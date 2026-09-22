"""POST /transfers/batch 的 idempotency_key 端到端测试。

覆盖：
* 同键同请求体 -> 重放首次响应：不重复扣减体积、不重复递增 version；
* 成功请求的指纹与完整响应与转移写入同一 SQLite 事务（idempotency_keys 表）；
* 同键异请求体 -> 409 idempotency_conflict，整批不落库；
* 失败请求不写幂等记录，同键可重试；
* /transfers/preview 不写幂等记录（携带键也被忽略）；
* 不携带键完全兼容；指纹以原始请求体字节为准。
"""
import hashlib
import json
import sqlite3

from helpers import container, sample, volume_of

from app.db import connect
from app.service import fingerprint_request


def _setup(client):
    sample(client, "S1", "10")
    container(client, "V1")
    container(client, "V2")


def _batch_raw(client, raw_body):
    """用逐字指定的原始请求体发请求（绕过客户端的 JSON 重排）。"""
    return client.post(
        "/transfers/batch",
        content=raw_body,
        headers={"Content-Type": "application/json"},
    )


def _batch(client, transfers, key=None, expected_versions=None):
    body = {"transfers": transfers}
    if key is not None:
        body["idempotency_key"] = key
    if expected_versions is not None:
        body["expected_versions"] = expected_versions
    return client.post("/transfers/batch", json=body)


def _version_of(client, cid):
    return client.get(f"/containers/{cid}").json()["version"]


def _idempotency_rows(key):
    conn = connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT idempotency_key, request_sha256, status_code, response_body"
                " FROM idempotency_keys WHERE idempotency_key = ?",
                (key,),
            )
        ]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 同键同请求体：重放首次结果
# --------------------------------------------------------------------------- #
def test_same_key_same_body_replays_without_deduction_or_version_increment(client):
    _setup(client)

    r1 = _batch(client, [{"source": "S1", "destination": "V1", "volume": "4"}], key="k1")
    assert r1.status_code == 200, r1.text
    assert r1.headers.get("Idempotency-Replayed") is None
    assert r1.json() == {
        "applied": 1,
        "message": "已依次提交 1 条转移",
        "versions": {"S1": 1, "V1": 1},
    }

    # 同键同请求体再来两次：逐字节重放
    for _ in range(2):
        r = _batch(client, [{"source": "S1", "destination": "V1", "volume": "4"}], key="k1")
        assert r.status_code == 200
        assert r.headers.get("Idempotency-Replayed") == "true"
        assert r.json() == r1.json()

    # 体积只扣过一次（10 - 4 = 6，而非 10 - 4*3）
    assert volume_of(client, "S1") == "6.000000000"
    assert volume_of(client, "V1") == "4.000000000"
    assert volume_of(client, "V2") == "0"
    # version 只递增过一次
    assert _version_of(client, "S1") == 1
    assert _version_of(client, "V1") == 1
    assert _version_of(client, "V2") == 0

    # 幂等记录只有一行
    rows = _idempotency_rows("k1")
    assert len(rows) == 1
    assert rows[0]["status_code"] == 200
    assert json.loads(rows[0]["response_body"]) == r1.json()


def test_replay_returns_byte_identical_first_response(client):
    _setup(client)
    raw = json.dumps(
        {
            "transfers": [{"source": "S1", "destination": "V1", "volume": "4"}],
            "idempotency_key": "byte-key",
        }
    )
    r1 = _batch_raw(client, raw)
    r2 = _batch_raw(client, raw)
    assert r1.status_code == r2.status_code == 200
    # 持久化的原始 JSON 与首次、重放响应体逐字节一致
    assert r1.content == r2.content
    assert _idempotency_rows("byte-key")[0]["response_body"] == r1.text
    assert r2.headers.get("Idempotency-Replayed") == "true"


def test_persisted_fingerprint_is_sha256_of_raw_body(client):
    _setup(client)
    raw = json.dumps(
        {
            "transfers": [{"source": "S1", "destination": "V1", "volume": "4"}],
            "idempotency_key": "fp-key",
        }
    )
    r = _batch_raw(client, raw)
    assert r.status_code == 200
    row = _idempotency_rows("fp-key")[0]
    assert row["request_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert fingerprint_request(raw.encode("utf-8")) == row["request_sha256"]


def test_replay_ignores_current_state_and_stale_expected_versions(client):
    _setup(client)
    transfers = [{"source": "S1", "destination": "V1", "volume": "1"}]

    # 首次成功：version 变为 1
    r1 = _batch(
        client, transfers, key="k-ev", expected_versions={"S1": 0, "V1": 0}
    )
    assert r1.status_code == 200
    assert r1.json()["versions"] == {"S1": 1, "V1": 1}

    # 同键重放：即使携带的 expected_versions 已过期，也直接返回首次结果，
    # 不再执行任何业务校验、扣减或版本递增
    r2 = _batch(
        client, transfers, key="k-ev", expected_versions={"S1": 0, "V1": 0}
    )
    assert r2.status_code == 200
    assert r2.headers.get("Idempotency-Replayed") == "true"
    assert r2.json() == r1.json()
    assert _version_of(client, "S1") == 1
    assert _version_of(client, "V1") == 1
    assert volume_of(client, "S1") == "9.000000000"


# --------------------------------------------------------------------------- #
# 同键异请求体：409 idempotency_conflict
# --------------------------------------------------------------------------- #
def test_same_key_different_body_409_and_not_persisted(client):
    _setup(client)
    r1 = _batch(client, [{"source": "S1", "destination": "V1", "volume": "4"}], key="k2")
    assert r1.status_code == 200

    # 同键但请求体不同（体积不同）
    r2 = _batch(client, [{"source": "S1", "destination": "V1", "volume": "5"}], key="k2")
    assert r2.status_code == 409
    err = r2.json()["error"]
    assert err["code"] == "idempotency_conflict"
    assert "k2" in err["message"]

    # 冲突请求不落库：体积与版本保持首次请求后的状态
    assert volume_of(client, "S1") == "6.000000000"
    assert volume_of(client, "V1") == "4.000000000"
    assert _version_of(client, "S1") == 1
    assert _version_of(client, "V1") == 1
    # 仍只有首次那一行记录，且重放仍可用
    assert len(_idempotency_rows("k2")) == 1
    r3 = _batch(client, [{"source": "S1", "destination": "V1", "volume": "4"}], key="k2")
    assert r3.status_code == 200
    assert r3.headers.get("Idempotency-Replayed") == "true"
    assert r3.json() == r1.json()


def test_same_key_different_expected_versions_is_conflict(client):
    _setup(client)
    transfers = [{"source": "S1", "destination": "V1", "volume": "1"}]

    r1 = _batch(client, transfers, key="k3", expected_versions={"S1": 0, "V1": 0})
    assert r1.status_code == 200

    # 同样的转移、同样的键，但 expected_versions 不同 -> 原始请求体不同 -> 409
    r2 = _batch(client, transfers, key="k3", expected_versions={"S1": 0, "V1": 1})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency_conflict"


def test_conflict_takes_precedence_over_business_errors(client):
    _setup(client)
    # 首次占用键 k4
    r1 = _batch(client, [{"source": "S1", "destination": "V1", "volume": "4"}], key="k4")
    assert r1.status_code == 200

    # 同键 + 不同请求体，且新请求本身业务上也不可能成功（源液不足）：
    # 幂等冲突在写事务内最先判定，应返回 409 而非 422
    r2 = _batch(client, [{"source": "S1", "destination": "V1", "volume": "999"}], key="k4")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency_conflict"
    assert volume_of(client, "S1") == "6.000000000"


# --------------------------------------------------------------------------- #
# 指纹以原始请求体字节为准
# --------------------------------------------------------------------------- #
def test_semantically_equal_but_different_bytes_are_conflict(client):
    _setup(client)
    canonical = (
        '{"transfers":[{"source":"S1","destination":"V1","volume":4}],'
        '"idempotency_key":"raw-key"}'
    )
    r1 = _batch_raw(client, canonical)
    assert r1.status_code == 200

    # 仅空白/排版不同 -> 语义相同但原始字节不同 -> 指纹不同 -> 409
    pretty = json.dumps(
        {
            "transfers": [{"source": "S1", "destination": "V1", "volume": 4}],
            "idempotency_key": "raw-key",
        },
        indent=2,
        ensure_ascii=False,
    )
    r2 = _batch_raw(client, pretty)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency_conflict"

    # 数字 4（JSON number）vs 字符串 "4"：同样字节不同 -> 409
    string_volume = canonical.replace('"volume":4', '"volume":"4"')
    assert string_volume != canonical
    r3 = _batch_raw(client, string_volume)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "idempotency_conflict"

    # 字节完全一致的重放仍然成功
    r4 = _batch_raw(client, canonical)
    assert r4.status_code == 200
    assert r4.headers.get("Idempotency-Replayed") == "true"
    assert r4.content == r1.content
    # 冲突尝试没有造成额外扣减
    assert volume_of(client, "S1") == "6.000000000"


# --------------------------------------------------------------------------- #
# 失败请求不写幂等记录：同键可重试
# --------------------------------------------------------------------------- #
def test_failed_request_writes_no_idempotency_record_and_key_can_retry(client):
    _setup(client)
    # 源液不足 -> 422 整批回滚
    r1 = _batch(client, [{"source": "S1", "destination": "V1", "volume": "999"}], key="k5")
    assert r1.status_code == 422
    assert r1.json()["error"]["code"] == "insufficient_volume"
    assert _idempotency_rows("k5") == []
    assert volume_of(client, "S1") == "10.000000000"

    # 同键（同体或换体）重试合法请求：正常成功
    r2 = _batch(client, [{"source": "S1", "destination": "V1", "volume": "3"}], key="k5")
    assert r2.status_code == 200
    assert r2.headers.get("Idempotency-Replayed") is None
    assert r2.json()["versions"] == {"S1": 1, "V1": 1}
    assert volume_of(client, "S1") == "7.000000000"
    assert volume_of(client, "V1") == "3.000000000"

    # 未知容器 / 自转移等失败同样不落记录
    r3 = _batch(
        client,
        [{"source": "S1", "destination": "GHOST", "volume": "1"}],
        key="k6",
    )
    assert r3.status_code == 404
    assert _idempotency_rows("k6") == []


def test_stale_expected_versions_failure_writes_no_record(client):
    _setup(client)
    transfers = [{"source": "S1", "destination": "V1", "volume": "1"}]
    r1 = _batch(client, transfers, key="k7", expected_versions={"S1": 9, "V1": 0})
    assert r1.status_code == 409
    assert r1.json()["error"]["code"] == "stale_container"
    assert _idempotency_rows("k7") == []
    # 用正确版本以同键重试成功
    r2 = _batch(client, transfers, key="k7", expected_versions={"S1": 0, "V1": 0})
    assert r2.status_code == 200
    assert _version_of(client, "S1") == 1


# --------------------------------------------------------------------------- #
# 不同键互不干扰；无键请求完全兼容
# --------------------------------------------------------------------------- #
def test_distinct_keys_are_independent(client):
    _setup(client)
    body_a = [{"source": "S1", "destination": "V1", "volume": "1"}]
    body_b = [{"source": "S1", "destination": "V2", "volume": "2"}]

    r1 = _batch(client, body_a, key="a")
    r2 = _batch(client, body_b, key="b")
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["versions"] == {"S1": 1, "V1": 1}
    assert r2.json()["versions"] == {"S1": 2, "V2": 1}

    # 同体不同键不视为重放：会再执行一次（这里是合法的第二次转移）
    r3 = _batch(client, body_a, key="c")
    assert r3.status_code == 200
    assert r3.headers.get("Idempotency-Replayed") is None
    assert r3.json()["versions"] == {"S1": 3, "V1": 2}

    # 各键重放各自的首次响应
    assert _batch(client, body_a, key="a").json() == r1.json()
    assert _batch(client, body_b, key="b").json() == r2.json()
    assert volume_of(client, "S1") == "6.000000000"  # 10-1-2-1


def test_request_without_key_is_fully_backward_compatible(client):
    _setup(client)
    body = [{"source": "S1", "destination": "V1", "volume": "1"}]

    r1 = _batch(client, body)
    r2 = _batch(client, body)
    assert r1.status_code == r2.status_code == 200
    assert r1.headers.get("Idempotency-Replayed") is None
    assert r2.headers.get("Idempotency-Replayed") is None
    # 无键：每次都真实执行，版本照常递增
    assert r1.json()["versions"] == {"S1": 1, "V1": 1}
    assert r2.json()["versions"] == {"S1": 2, "V1": 2}
    assert _idempotency_rows("nonexistent") == []
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 入参校验
# --------------------------------------------------------------------------- #
def test_blank_idempotency_key_rejected_422(client):
    _setup(client)
    for bad in ("", "   "):
        r = _batch(
            client,
            [{"source": "S1", "destination": "V1", "volume": "1"}],
            key=bad,
        )
        assert r.status_code == 422, r.text
    # 键两侧空白会被 strip 后作为存储/查重键；指纹仍是原始请求体字节，
    # 因此必须用完全相同的原始请求体（含空白）才能重放
    raw = (
        '{"transfers":[{"source":"S1","destination":"V1","volume":"1"}],'
        '"idempotency_key":"  trim-key  "}'
    )
    r = _batch_raw(client, raw)
    assert r.status_code == 200
    r2 = _batch_raw(client, raw)
    assert r2.status_code == 200
    assert r2.headers.get("Idempotency-Replayed") == "true"
    assert r2.json() == r.json()
    # 存储的键是 strip 后的
    assert len(_idempotency_rows("trim-key")) == 1


def test_too_long_idempotency_key_rejected_422(client):
    _setup(client)
    r = _batch(
        client,
        [{"source": "S1", "destination": "V1", "volume": "1"}],
        key="x" * 256,
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# 预演：不写幂等记录、不重放
# --------------------------------------------------------------------------- #
def test_preview_ignores_idempotency_key(client):
    _setup(client)
    transfers = [{"source": "S1", "destination": "V1", "volume": "4"}]

    # 预演携带键：不查重、不重放、不落任何记录
    p1 = client.post(
        "/transfers/preview",
        json={"transfers": transfers, "idempotency_key": "pk"},
    )
    assert p1.status_code == 200
    assert "Idempotency-Replayed" not in p1.headers
    assert _idempotency_rows("pk") == []
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
    finally:
        conn.close()
    # 数据库未被预演改变
    assert volume_of(client, "S1") == "10.000000000"
    assert _version_of(client, "S1") == 0

    # 预演不会“消费”幂等键：随后同键提交仍是首次成功而非重放
    b = _batch(client, transfers, key="pk")
    assert b.status_code == 200
    assert b.headers.get("Idempotency-Replayed") is None
    assert b.json()["versions"] == {"S1": 1, "V1": 1}

    # 再次预演同键：不触发重放，仍正常按当前状态模拟（此时已提交过一次）
    p2 = client.post(
        "/transfers/preview",
        json={"transfers": transfers, "idempotency_key": "pk"},
    )
    assert p2.status_code == 200
    assert "Idempotency-Replayed" not in p2.headers
    assert p2.json()["containers"] != p1.json()["containers"]
    assert volume_of(client, "S1") == "6.000000000"  # 预演未再次扣减
    assert _version_of(client, "S1") == 1


# --------------------------------------------------------------------------- #
# 持久化原子性：转移写入与幂等记录在同一事务（服务层直查）
# --------------------------------------------------------------------------- #
def test_idempotency_record_persisted_alongside_transfer(client):
    _setup(client)
    r = _batch(client, [{"source": "S1", "destination": "V1", "volume": "4"}], key="atomic")
    assert r.status_code == 200

    conn = connect()
    try:
        # 行存在且与转移结果一致
        row = conn.execute(
            "SELECT request_sha256, status_code, response_body FROM idempotency_keys"
            " WHERE idempotency_key = ?",
            ("atomic",),
        ).fetchone()
        assert row is not None
        assert row["status_code"] == 200
        payload = json.loads(row["response_body"])
        assert payload["applied"] == 1
        assert payload["versions"] == {"S1": 1, "V1": 1}

        # 容器扣减与版本递增同批可见
        versions = dict(
            conn.execute("SELECT id, version FROM containers WHERE id IN ('S1','V1')")
        )
        assert versions == {"S1": 1, "V1": 1}
        amount = conn.execute(
            "SELECT amount FROM components WHERE container_id = 'S1'"
        ).fetchone()["amount"]
        assert amount == "6.000000000"
    finally:
        conn.close()


def test_service_replay_does_not_increment_versions(client):
    from app import service
    from decimal import Decimal

    _setup(client)
    # 在 setup 提交之后再打开连接，避免 WAL 读事务快照停留在旧状态
    conn = connect()
    try:
        r1 = service.apply_transfers_idempotent(
            conn,
            [("S1", "V1", Decimal("4"))],
            idempotency_key="svc",
            request_sha256="a" * 64,
        )
        assert r1.replayed is False
        assert r1.applied == 1
        assert r1.versions == {"S1": 1, "V1": 1}

        r2 = service.apply_transfers_idempotent(
            conn,
            [("S1", "V1", Decimal("4"))],
            idempotency_key="svc",
            request_sha256="a" * 64,
        )
        assert r2.replayed is True
        assert r2.payload == r1.payload
        assert r2.applied == 1
        assert r2.versions == {"S1": 1, "V1": 1}

        # 同键异指纹 -> 409
        import pytest
        from app.service import LineageError

        with pytest.raises(LineageError) as exc:
            service.apply_transfers_idempotent(
                conn,
                [("S1", "V1", Decimal("5"))],
                idempotency_key="svc",
                request_sha256="b" * 64,
            )
        assert exc.value.status_code == 409
        assert exc.value.code == "idempotency_conflict"
    finally:
        conn.close()

    assert volume_of(client, "S1") == "6.000000000"
    assert _version_of(client, "S1") == 1


# --------------------------------------------------------------------------- #
# 旧库迁移：init_db 自动为最老的库补齐 idempotency_keys 表
# --------------------------------------------------------------------------- #
def test_init_db_creates_idempotency_table_on_legacy_database(tmp_path, monkeypatch):
    legacy_path = tmp_path / "legacy.db"

    # 最老的 schema：既没有 containers.version，也没有 idempotency_keys 表
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
            """
        )
        raw.commit()
    finally:
        raw.close()

    monkeypatch.setattr("app.config.settings.database_path", str(legacy_path))

    from app.db import connect, init_db

    init_db()  # 一次启动：同时补齐 version 列与幂等表
    init_db()  # 再次启动必须幂等

    conn = connect()
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "idempotency_keys" in tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info(idempotency_keys)")}
        assert cols == {
            "idempotency_key",
            "request_sha256",
            "status_code",
            "response_body",
            "created",
        }
        # 旧数据完好，版本列已回填为 0
        assert conn.execute(
            "SELECT version FROM containers WHERE id = 'OLD'"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
    finally:
        conn.close()
