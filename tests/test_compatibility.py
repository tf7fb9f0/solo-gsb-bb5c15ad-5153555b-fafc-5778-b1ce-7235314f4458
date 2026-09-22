"""祖先来源不相容规则端到端测试。

覆盖：
* PUT /compatibility/rules 原子全量替换：A-B/B-A 去重、规范化排序返回；
  自配对 422 self_pair；引用未登记为初始样品的 ID 422 unknown_sample；
  任一失败旧规则完全不变；空列表清空；规则变更不递增 version、不写审计；
* GET /compatibility/rules 返回规范化排序后的规则；
* 批量提交与预演复用同一顺序模拟：每一步分摊后检查目标容器的非零祖先集合，
  命中规则返回 422 incompatible_sources（条目序号、目标、排序后的全部冲突对），
  提交整批回滚（体积/版本/审计/幂等记录不变）、预演不写库；
* 同批前序混合产生的隐性祖先参与后续检查；0 份额来源不计入非零祖先集合；
* 规则只约束其后的转移，不回溯既有容器；
* 幂等重放仍返回首次成功结果（不重新校验规则）；失败请求不写幂等记录；
* 旧库启动时自动迁移 compatibility_rules 表与 containers.is_sample 列。
"""
import sqlite3

from helpers import container, sample, volume_of

from app.db import connect


def _put_rules(client, pairs):
    return client.put(
        "/compatibility/rules",
        json={"rules": [{"source_a": a, "source_b": b} for a, b in pairs]},
    )


def _get_rules(client):
    r = client.get("/compatibility/rules")
    assert r.status_code == 200, r.text
    return r.json()


def _version_of(client, cid):
    return client.get(f"/containers/{cid}").json()["version"]


def _audit_count(client):
    return client.get("/audit/records").json()["count"]


def _idempotency_count():
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# PUT /compatibility/rules：原子全量替换、规范化、校验
# --------------------------------------------------------------------------- #
def test_put_and_get_rules_normalized_and_sorted(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    sample(client, "S3", "10")

    # 乱序 + A-B/B-A 重复 + 两端顺序颠倒：PUT 响应即规范化排序结果
    r = client.put(
        "/compatibility/rules",
        json={
            "rules": [
                {"source_a": "S3", "source_b": "S1"},
                {"source_a": "S1", "source_b": "S2"},
                {"source_a": "S2", "source_b": "S1"},  # 与上一条重复
                {"source_a": "S1", "source_b": "S3"},  # 与第一条重复
            ]
        },
    )
    assert r.status_code == 200, r.text
    expected = {
        "rules": [
            {"source_a": "S1", "source_b": "S2"},
            {"source_a": "S1", "source_b": "S3"},
        ]
    }
    assert r.json() == expected
    # GET 返回同一规范化排序视图
    assert _get_rules(client) == expected


def test_put_rules_full_replacement_and_clear(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    sample(client, "S3", "10")

    r = _put_rules(client, [("S1", "S2")])
    assert r.status_code == 200, r.text

    # 全量替换：旧规则整体消失，只保留新规则
    r = _put_rules(client, [("S2", "S3")])
    assert r.status_code == 200
    assert r.json() == {"rules": [{"source_a": "S2", "source_b": "S3"}]}
    assert _get_rules(client) == {"rules": [{"source_a": "S2", "source_b": "S3"}]}

    # 空列表清空全部规则
    r = _put_rules(client, [])
    assert r.status_code == 200
    assert r.json() == {"rules": []}
    assert _get_rules(client) == {"rules": []}


def test_put_rules_self_pair_rejected_and_rules_unchanged(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    _put_rules(client, [("S1", "S2")])

    r = _put_rules(client, [("S2", "S2")])
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "self_pair"
    assert "第 1 条规则" in err["message"]
    assert "S2" in err["message"]
    # 拒绝自配对：旧规则完全不变（原子替换，失败不落库）
    assert _get_rules(client) == {"rules": [{"source_a": "S1", "source_b": "S2"}]}


def test_put_rules_unknown_sample_rejected(client):
    sample(client, "S1", "10")
    container(client, "V1")  # 空容器不是初始样品

    # 空容器 ID 与未知 ID 一并拒绝，details 列出全部未登记 ID（排序）
    r = _put_rules(client, [("S1", "V1"), ("S1", "GHOST")])
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "unknown_sample"
    assert err["details"]["unknown_samples"] == ["GHOST", "V1"]
    # 整批规则未替换：规则表仍为空
    assert _get_rules(client) == {"rules": []}


def test_rule_change_does_not_touch_versions_or_audit(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    container(client, "V1")
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "1"}]},
    )
    assert r.status_code == 200
    assert _audit_count(client) == 1

    # 规则变更不是批量转移：version 不变、不产生审计记录
    r = _put_rules(client, [("S1", "S2")])
    assert r.status_code == 200
    assert _version_of(client, "S1") == 1
    assert _version_of(client, "V1") == 1
    assert _version_of(client, "S2") == 0
    assert _audit_count(client) == 1


# --------------------------------------------------------------------------- #
# 批量提交：每一步分摊后检查目标非零祖先集合
# --------------------------------------------------------------------------- #
def test_batch_incompatible_sources_rejects_whole_batch(client):
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    container(client, "V1")
    container(client, "V2")
    _put_rules(client, [("S1", "S2")])

    # 第一条合法、第二条使 V1 同时含有 S1 与 S2：整批回滚，第一条也不留痕
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "5"},
                {"source": "S2", "destination": "V1", "volume": "5"},
                {"source": "S1", "destination": "V2", "volume": "3"},
            ]
        },
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "incompatible_sources"
    # 返回条目序号、目标容器与排序后的全部冲突对（消息与 details 双重呈现）
    assert "第 2 条转移" in err["message"]
    assert "V1" in err["message"]
    assert err["details"] == {
        "index": 2,
        "destination": "V1",
        "conflicts": [{"source_a": "S1", "source_b": "S2"}],
    }

    # 整批回滚：体积、版本、审计链均不变化
    assert volume_of(client, "S1") == "100.000000000"
    assert volume_of(client, "S2") == "100.000000000"
    assert volume_of(client, "V1") == "0"
    assert volume_of(client, "V2") == "0"
    for cid in ("S1", "S2", "V1", "V2"):
        assert _version_of(client, cid) == 0
    assert _audit_count(client) == 0


def test_batch_multiple_conflicts_all_reported_sorted(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    sample(client, "S3", "10")
    container(client, "V1")
    # V1 先混合 S1 + S2（规则尚未禁止 S1-S2）
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "1"},
                {"source": "S2", "destination": "V1", "volume": "1"},
            ]
        },
    )
    assert r.status_code == 200

    # 乱序写入两条规则；转入 S3 后 V1 同时命中两对：全部列出且按字典序排序
    _put_rules(client, [("S2", "S3"), ("S1", "S3")])
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S3", "destination": "V1", "volume": "1"}]},
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "incompatible_sources"
    assert err["details"]["index"] == 1
    assert err["details"]["destination"] == "V1"
    assert err["details"]["conflicts"] == [
        {"source_a": "S1", "source_b": "S3"},
        {"source_a": "S2", "source_b": "S3"},
    ]
    assert volume_of(client, "V1") == "2.000000000"


def test_implicit_ancestors_from_earlier_steps_are_checked(client):
    """同批前序混合产生的隐性祖先必须参与后续步骤的检查。"""
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    sample(client, "S3", "100")
    container(client, "V1")
    container(client, "V2")
    _put_rules(client, [("S1", "S3")])

    # 同批前序把 S1+S2 混入 V1；V1 -> V2 时 S1 作为隐性祖先随混合进入 V2，
    # 与 V2 中已有的 S3 构成禁配对（S1 并未直接转入 V2）
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "10"},
                {"source": "S2", "destination": "V1", "volume": "10"},
                {"source": "S3", "destination": "V2", "volume": "5"},
                {"source": "V1", "destination": "V2", "volume": "4"},
            ]
        },
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "incompatible_sources"
    assert err["details"] == {
        "index": 4,
        "destination": "V2",
        "conflicts": [{"source_a": "S1", "source_b": "S3"}],
    }
    # 整批回滚
    for cid in ("S1", "S2", "S3"):
        assert volume_of(client, cid) == "100.000000000"
    assert volume_of(client, "V1") == "0"
    assert volume_of(client, "V2") == "0"


def test_zero_amount_ancestors_do_not_trigger_rule(client):
    """最大余额法可能给极小份额分出 0 个刻度：0 份额不计入非零祖先集合。"""
    sample(client, "A", "0.5")
    sample(client, "B", "0.000000001")
    container(client, "M")
    container(client, "V1")

    # 先构造混合（规则尚不存在）：V1 含 1 个刻度的 A；M = {A, B: 1 个刻度}
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "A", "destination": "V1", "volume": "0.000000001"},
                {"source": "A", "destination": "M", "volume": "0.499999998"},
                {"source": "B", "destination": "M", "volume": "0.000000001"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    _put_rules(client, [("A", "B")])

    # M = {A: 0.499999998, B: 0.000000001}；转入 1 个刻度时最大余额法把唯一的
    # 刻度分给 A，B 实际移动 0：V1 的非零祖先集合仍只有 A，不命中规则
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "M", "destination": "V1", "volume": "0.000000001"}]},
    )
    assert r.status_code == 200, r.text
    assert volume_of(client, "V1") == "0.000000002"

    # 转入足够大时 B 真实移动 1 个刻度：命中规则
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "M", "destination": "V1", "volume": "0.499999998"}]},
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "incompatible_sources"
    assert err["details"]["conflicts"] == [{"source_a": "A", "source_b": "B"}]


# --------------------------------------------------------------------------- #
# 预演：同样的不相容校验，且不落库
# --------------------------------------------------------------------------- #
def test_preview_incompatible_identical_to_batch_and_persists_nothing(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    container(client, "V1")
    _put_rules(client, [("S1", "S2")])
    payload = {
        "transfers": [
            {"source": "S1", "destination": "V1", "volume": "1"},
            {"source": "S2", "destination": "V1", "volume": "1"},
        ]
    }

    preview_resp = client.post("/transfers/preview", json=payload)
    batch_resp = client.post("/transfers/batch", json=payload)
    # 状态码、错误码、条目序号与 details 与真实提交完全一致
    assert preview_resp.status_code == batch_resp.status_code == 422
    assert preview_resp.json() == batch_resp.json()
    assert preview_resp.json()["error"]["code"] == "incompatible_sources"
    assert preview_resp.json()["error"]["details"]["index"] == 2
    # 失败的预演与提交都不落库
    assert volume_of(client, "V1") == "0"
    assert _version_of(client, "V1") == 0
    assert _audit_count(client) == 0


def test_preview_success_within_rules_persists_nothing(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    container(client, "V1")
    _put_rules(client, [("S1", "S2")])

    # 只含 S1 的转入不命中规则：预演成功且不落库
    r = client.post(
        "/transfers/preview",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "1"}]},
    )
    assert r.status_code == 200, r.text
    assert volume_of(client, "V1") == "0"
    assert _version_of(client, "V1") == 0


# --------------------------------------------------------------------------- #
# 规则的生命周期：只约束其后的转移，不回溯既有容器
# --------------------------------------------------------------------------- #
def test_rules_replacement_allows_previously_forbidden_batch(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    container(client, "V1")
    _put_rules(client, [("S1", "S2")])
    payload = {
        "transfers": [
            {"source": "S1", "destination": "V1", "volume": "1"},
            {"source": "S2", "destination": "V1", "volume": "1"},
        ]
    }
    assert client.post("/transfers/batch", json=payload).status_code == 422

    # 全量替换为空规则后，同一批成功
    _put_rules(client, [])
    r = client.post("/transfers/batch", json=payload)
    assert r.status_code == 200, r.text
    assert volume_of(client, "V1") == "2.000000000"


def test_rules_do_not_retroactively_block_existing_mixtures(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    container(client, "V1")
    container(client, "V2")
    # 先混合（规则尚不存在）
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "2"},
                {"source": "S2", "destination": "V1", "volume": "2"},
            ]
        },
    )
    assert r.status_code == 200

    # 事后新增规则：既有混合不回溯，查询照常
    _put_rules(client, [("S1", "S2")])
    body = client.get("/containers/V1").json()
    assert body["volume"] == "4.000000000"

    # 但规则约束其后的转移：V1 已含 S1+S2，任何使其仍含该对的转入都被拒
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "1"}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "incompatible_sources"
    assert r.json()["error"]["details"]["destination"] == "V1"

    # V1 的混合液转入空容器会把禁配对带入目标：同样被拒
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "V1", "destination": "V2", "volume": "1"}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["destination"] == "V2"

    # 只含单一来源的转入不命中规则：S1 -> V2 合法
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V2", "volume": "1"}]},
    )
    assert r.status_code == 200, r.text
    assert volume_of(client, "V2") == "1.000000000"


# --------------------------------------------------------------------------- #
# 幂等：失败不写记录；已成功请求的重放保持原结果（不重新校验规则）
# --------------------------------------------------------------------------- #
def test_idempotent_replay_keeps_first_result_after_rule_change(client):
    """首次成功后新增禁配规则，同键重放仍返回首次结果（不重新校验规则）。"""
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    container(client, "V1")
    payload = {
        "transfers": [
            {"source": "S1", "destination": "V1", "volume": "1"},
            {"source": "S2", "destination": "V1", "volume": "1"},
        ],
        "idempotency_key": "compat-key",
    }
    r1 = client.post("/transfers/batch", json=payload)
    assert r1.status_code == 200, r1.text

    # 新增规则后，同样的转移不带键执行必然 422
    _put_rules(client, [("S1", "S2")])
    fresh = {"transfers": payload["transfers"]}
    r = client.post("/transfers/batch", json=fresh)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "incompatible_sources"

    # 幂等重放：返回首次结果，不重复扣减、不重复递增版本、不产生新审计
    r2 = client.post("/transfers/batch", json=payload)
    assert r2.status_code == 200
    assert r2.headers.get("Idempotency-Replayed") == "true"
    assert r2.json() == r1.json()
    assert volume_of(client, "V1") == "2.000000000"
    assert _version_of(client, "V1") == 1
    assert _audit_count(client) == 1


def test_incompatible_failure_writes_no_idempotency_record_and_key_can_retry(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    container(client, "V1")
    _put_rules(client, [("S1", "S2")])
    payload = {
        "transfers": [
            {"source": "S1", "destination": "V1", "volume": "1"},
            {"source": "S2", "destination": "V1", "volume": "1"},
        ],
        "idempotency_key": "k-compat",
    }

    # 带幂等键的不相容失败：不落幂等记录、不留审计
    r = client.post("/transfers/batch", json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "incompatible_sources"
    assert _idempotency_count() == 0
    assert _audit_count(client) == 0

    # 清空规则后同键重试成功
    _put_rules(client, [])
    r = client.post("/transfers/batch", json=payload)
    assert r.status_code == 200, r.text
    assert _idempotency_count() == 1
    assert volume_of(client, "V1") == "2.000000000"


# --------------------------------------------------------------------------- #
# 旧库迁移：init_db 自动补齐 compatibility_rules 表与 is_sample 列
# --------------------------------------------------------------------------- #
def test_init_db_migrates_legacy_database_with_compatibility(tmp_path, monkeypatch):
    legacy_path = tmp_path / "legacy.db"

    # 旧 schema：containers 有 version/capacity，但没有 is_sample 列，
    # 也没有 compatibility_rules 表
    raw = sqlite3.connect(legacy_path)
    try:
        raw.executescript(
            """
            CREATE TABLE containers (
                id      TEXT PRIMARY KEY,
                version INTEGER NOT NULL DEFAULT 0,
                capacity TEXT,
                created INTEGER NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE components (
                container_id TEXT NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
                source_id    TEXT NOT NULL,
                amount       TEXT NOT NULL,
                PRIMARY KEY (container_id, source_id)
            );
            INSERT INTO containers (id) VALUES ('OLDS'), ('OLDC');
            INSERT INTO components (container_id, source_id, amount)
                VALUES ('OLDS', 'OLDS', '5.500000000');
            """
        )
        raw.commit()
    finally:
        raw.close()

    monkeypatch.setattr("app.config.settings.database_path", str(legacy_path))

    from app import service
    from app.db import connect as db_connect, init_db
    from app.service import LineageError

    init_db()  # 执行迁移
    init_db()  # 再次执行必须幂等，不报错、不改变数据

    conn = db_connect()
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "compatibility_rules" in tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info(containers)")}
        assert "is_sample" in cols

        # 回填：仍持有自身来源份额的旧容器识别为初始样品；空容器不是
        flags = dict(conn.execute("SELECT id, is_sample FROM containers"))
        assert flags == {"OLDS": 1, "OLDC": 0}
        # 旧数据完好
        assert service.get_container_status(conn, "OLDS")["volume"] == "5.500000000"

        # 迁移后规则接口可用：新登记的样品与回填样品可组成规则
        from decimal import Decimal

        service.register_initial_sample(conn, "NEWS", Decimal("1"))
        result = service.replace_compatibility_rules(conn, [("NEWS", "OLDS")])
        assert result == {"rules": [{"source_a": "NEWS", "source_b": "OLDS"}]}
        assert service.get_compatibility_rules(conn) == result

        # 空容器（OLDC）不是初始样品：引用被拒
        import pytest

        with pytest.raises(LineageError) as exc:
            service.replace_compatibility_rules(conn, [("OLDS", "OLDC")])
        assert exc.value.status_code == 422
        assert exc.value.code == "unknown_sample"
        # 失败不替换：旧规则仍在
        assert service.get_compatibility_rules(conn) == result
    finally:
        conn.close()
