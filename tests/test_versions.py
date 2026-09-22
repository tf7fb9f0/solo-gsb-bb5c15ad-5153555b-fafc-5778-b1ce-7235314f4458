"""乐观并发保护（持久化 version / expected_versions）端到端测试。"""
import sqlite3
from decimal import Decimal

from helpers import container, sample, volume_of


def _batch(client, transfers, expected_versions=None):
    body = {"transfers": transfers}
    if expected_versions is not None:
        body["expected_versions"] = expected_versions
    return client.post("/transfers/batch", json=body)


def _preview(client, transfers, expected_versions=None):
    body = {"transfers": transfers}
    if expected_versions is not None:
        body["expected_versions"] = expected_versions
    return client.post("/transfers/preview", json=body)


def _version_of(client, cid):
    return client.get(f"/containers/{cid}").json()["version"]


def _setup_chain(client):
    sample(client, "S1", "10")
    container(client, "V1")
    container(client, "V2")


# --------------------------------------------------------------------------- #
# 初始版本
# --------------------------------------------------------------------------- #
def test_new_containers_start_at_version_0(client):
    sample(client, "S1", "10")
    container(client, "V1")
    assert _version_of(client, "S1") == 0
    assert _version_of(client, "V1") == 0
    # GET 输出中包含 version 字段
    body = client.get("/containers/S1").json()
    assert body["version"] == 0


# --------------------------------------------------------------------------- #
# 成功提交：每个被触碰容器 version 恰好 +1（同批多次出现也只 +1）
# --------------------------------------------------------------------------- #
def test_successful_batch_increments_each_touched_container_once(client):
    _setup_chain(client)

    # V1 在同批中先作为目标、再作为源（链式），只应 +1
    r = _batch(
        client,
        [
            {"source": "S1", "destination": "V1", "volume": "3"},
            {"source": "V1", "destination": "V2", "volume": "2"},
        ],
    )
    assert r.status_code == 200, r.text
    assert r.json()["versions"] == {"S1": 1, "V1": 1, "V2": 1}
    assert _version_of(client, "S1") == 1
    assert _version_of(client, "V1") == 1
    assert _version_of(client, "V2") == 1

    # 再来一批，只触碰 S1/V1：版本各自再加 1，V2 保持 1
    r = _batch(client, [{"source": "S1", "destination": "V1", "volume": "1"}])
    assert r.status_code == 200, r.text
    assert r.json()["versions"] == {"S1": 2, "V1": 2}
    assert _version_of(client, "S1") == 2
    assert _version_of(client, "V1") == 2
    assert _version_of(client, "V2") == 1


def test_versions_returned_even_without_expected_versions(client):
    _setup_chain(client)
    r = _batch(client, [{"source": "S1", "destination": "V1", "volume": "1"}])
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == 1
    assert body["versions"] == {"S1": 1, "V1": 1}


# --------------------------------------------------------------------------- #
# expected_versions：匹配时成功
# --------------------------------------------------------------------------- #
def test_batch_with_correct_expected_versions_succeeds(client):
    _setup_chain(client)
    r = _batch(
        client,
        [{"source": "S1", "destination": "V1", "volume": "1"}],
        expected_versions={"S1": 0, "V1": 0},
    )
    assert r.status_code == 200, r.text
    assert r.json()["versions"] == {"S1": 1, "V1": 1}


# --------------------------------------------------------------------------- #
# expected_versions：覆盖错误 -> 422 invalid_expected_versions（不落库）
# --------------------------------------------------------------------------- #
def test_expected_versions_missing_key_422(client):
    _setup_chain(client)
    r = _batch(
        client,
        [{"source": "S1", "destination": "V1", "volume": "1"}],
        expected_versions={"S1": 0},  # 缺 V1
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_expected_versions"
    assert volume_of(client, "S1") == "10.000000000"
    assert _version_of(client, "S1") == 0


def test_expected_versions_extra_key_422(client):
    _setup_chain(client)
    r = _batch(
        client,
        [{"source": "S1", "destination": "V1", "volume": "1"}],
        expected_versions={"S1": 0, "V1": 0, "V2": 0},  # 多 V2
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_expected_versions"
    assert _version_of(client, "S1") == 0


def test_expected_versions_empty_dict_422(client):
    _setup_chain(client)
    r = _batch(
        client,
        [{"source": "S1", "destination": "V1", "volume": "1"}],
        expected_versions={},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_expected_versions"


def test_expected_versions_coverage_dedupes_repeated_containers(client):
    # V1 在两条转移中分别作目标和源：覆盖集仍是 {S1, V1, V2}
    _setup_chain(client)
    transfers = [
        {"source": "S1", "destination": "V1", "volume": "3"},
        {"source": "V1", "destination": "V2", "volume": "2"},
    ]
    r = _batch(client, transfers, expected_versions={"S1": 0, "V1": 0})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_expected_versions"

    r = _batch(
        client,
        transfers,
        expected_versions={"S1": 0, "V1": 0, "V2": 0},
    )
    assert r.status_code == 200, r.text
    assert r.json()["versions"] == {"S1": 1, "V1": 1, "V2": 1}


# --------------------------------------------------------------------------- #
# expected_versions：版本不符 -> 409 stale_container（整批不落库）
# --------------------------------------------------------------------------- #
def test_stale_expected_version_409_and_whole_batch_not_persisted(client):
    _setup_chain(client)
    # 先真实提交一次：S1/V1 版本变为 1
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "1"}])

    # 第二条转移引用的 V2 版本预期错误：整批拒绝
    r = _batch(
        client,
        [
            {"source": "S1", "destination": "V1", "volume": "1"},
            {"source": "S1", "destination": "V2", "volume": "1"},
        ],
        expected_versions={"S1": 1, "V1": 1, "V2": 99},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "stale_container"
    assert "V2" in body["error"]["message"]

    # 整批不落库：体积与版本都停留在上一批之后的状态
    assert volume_of(client, "S1") == "9.000000000"
    assert volume_of(client, "V1") == "1.000000000"
    assert volume_of(client, "V2") == "0"
    assert _version_of(client, "S1") == 1
    assert _version_of(client, "V1") == 1
    assert _version_of(client, "V2") == 0


def test_stale_version_on_first_appearance_order(client):
    _setup_chain(client)
    r = _batch(
        client,
        [{"source": "S1", "destination": "V1", "volume": "1"}],
        expected_versions={"S1": 7, "V1": 0},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_container"
    assert "S1" in r.json()["error"]["message"]
    assert _version_of(client, "S1") == 0


def test_successful_commit_requires_refreshed_versions(client):
    _setup_chain(client)
    transfers = [{"source": "S1", "destination": "V1", "volume": "1"}]

    r = _batch(client, transfers, expected_versions={"S1": 0, "V1": 0})
    assert r.status_code == 200

    # 客户端仍持有旧版本 0 -> 409
    r = _batch(client, transfers, expected_versions={"S1": 0, "V1": 0})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_container"

    # 用 GET 刷新版本后重试 -> 成功
    versions = {
        cid: _version_of(client, cid) for cid in ("S1", "V1")
    }
    r = _batch(client, transfers, expected_versions=versions)
    assert r.status_code == 200
    assert r.json()["versions"] == {"S1": 2, "V1": 2}


def test_unknown_container_with_valid_coverage_still_404(client):
    sample(client, "S1", "10")
    r = _batch(
        client,
        [{"source": "S1", "destination": "GHOST", "volume": "1"}],
        expected_versions={"S1": 0, "GHOST": 0},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_container"


# --------------------------------------------------------------------------- #
# preview：复用覆盖与版本校验，但不递增、不写库，返回预测版本
# --------------------------------------------------------------------------- #
def test_preview_returns_predicted_versions_without_increment(client):
    _setup_chain(client)
    transfers = [
        {"source": "S1", "destination": "V1", "volume": "3"},
        {"source": "V1", "destination": "V2", "volume": "2"},
    ]
    r = _preview(client, transfers)
    assert r.status_code == 200, r.text
    predicted = {c["container_id"]: c["version"] for c in r.json()["containers"]}
    # 每个被触碰容器预测提交后 +1（V1 同批出现两次也只 +1）
    assert predicted == {"S1": 1, "V1": 1, "V2": 1}

    # 预演不递增、不写库
    assert _version_of(client, "S1") == 0
    assert _version_of(client, "V1") == 0
    assert _version_of(client, "V2") == 0
    assert volume_of(client, "S1") == "10.000000000"
    assert volume_of(client, "V2") == "0"

    # 可安全重复调用，结果一致
    assert _preview(client, transfers).json() == r.json()


def test_preview_predicted_versions_match_subsequent_commit(client):
    _setup_chain(client)
    transfers = [{"source": "S1", "destination": "V1", "volume": "1"}]

    preview = _preview(client, transfers).json()
    commit = _batch(client, transfers)
    assert commit.status_code == 200
    commit_versions = commit.json()["versions"]

    for c in preview["containers"]:
        actual = client.get(f"/containers/{c['container_id']}").json()
        assert actual == c  # 含 version：预测版本恰好等于真实提交后的版本
        assert commit_versions[c["container_id"]] == c["version"]


def test_preview_with_expected_versions_matches_checks(client):
    _setup_chain(client)
    transfers = [{"source": "S1", "destination": "V1", "volume": "1"}]

    # 覆盖错误：422
    r = _preview(client, transfers, expected_versions={"S1": 0})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_expected_versions"

    # 版本不符：409（预演同样不写库）
    r = _preview(client, transfers, expected_versions={"S1": 0, "V1": 42})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_container"
    assert _version_of(client, "V1") == 0
    assert volume_of(client, "S1") == "10.000000000"

    # 版本匹配：200，预测版本 +1
    r = _preview(client, transfers, expected_versions={"S1": 0, "V1": 0})
    assert r.status_code == 200, r.text
    predicted = {c["container_id"]: c["version"] for c in r.json()["containers"]}
    assert predicted == {"S1": 1, "V1": 1}


def test_preview_with_expected_versions_error_identical_to_batch(client):
    _setup_chain(client)
    transfers = [{"source": "S1", "destination": "V1", "volume": "1"}]
    expected = {"S1": 0, "V1": 42}

    preview_resp = _preview(client, transfers, expected_versions=expected)
    batch_resp = _batch(client, transfers, expected_versions=expected)
    assert preview_resp.status_code == batch_resp.status_code == 409
    assert preview_resp.json() == batch_resp.json()
    # 两种调用都不改变数据库
    assert volume_of(client, "S1") == "10.000000000"
    assert _version_of(client, "S1") == 0


# --------------------------------------------------------------------------- #
# 旧库迁移：init_db 兼容没有 version 列的已有数据
# --------------------------------------------------------------------------- #
def test_init_db_migrates_legacy_database_without_version(tmp_path, monkeypatch):
    legacy_path = tmp_path / "legacy.db"

    # 直接按旧 schema 造一个已有数据库（containers 无 version 列）
    raw = sqlite3.connect(legacy_path)
    try:
        raw.executescript(
            """
            CREATE TABLE containers (
                id      TEXT PRIMARY KEY,
                created INTEGER NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE components (
                container_id TEXT NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
                source_id    TEXT NOT NULL,
                amount       TEXT NOT NULL,
                PRIMARY KEY (container_id, source_id)
            );
            INSERT INTO containers (id) VALUES ('OLD');
            INSERT INTO components (container_id, source_id, amount)
                VALUES ('OLD', 'OLD', '5.500000000');
            INSERT INTO containers (id) VALUES ('EMPTY');
            """
        )
        raw.commit()
    finally:
        raw.close()

    monkeypatch.setattr("app.config.settings.database_path", str(legacy_path))

    from app.db import connect, init_db
    from app import service

    init_db()  # 执行迁移
    init_db()  # 再次执行必须幂等，不报错、不改变版本

    conn = connect()
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(containers)")}
        assert "version" in columns
        rows = dict(
            conn.execute("SELECT id, version FROM containers ORDER BY id").fetchall()
        )
        assert rows == {"EMPTY": 0, "OLD": 0}

        # 迁移后服务可正常读取旧数据
        status = service.get_container_status(conn, "OLD")
        assert status["version"] == 0
        assert status["volume"] == "5.500000000"
        assert service.get_container_status(conn, "EMPTY")["version"] == 0

        # 迁移后的旧容器可参与转移，版本正常从 0 增到 1
        service.register_empty_container(conn, "NEW")
        applied, versions = service.apply_transfers(
            conn,
            [("OLD", "EMPTY", Decimal("1"))],
        )
        assert applied == 1
        assert versions == {"OLD": 1, "EMPTY": 1}
    finally:
        conn.close()
