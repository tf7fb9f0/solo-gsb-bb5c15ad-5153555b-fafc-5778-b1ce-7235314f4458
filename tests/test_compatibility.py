"""祖先来源不相容规则端到端测试。

覆盖：
* PUT /compatibility/rules 原子全量替换：无向去重（A-B 与 B-A）、规范化排序、
  拒绝自配对（422 incompatible_self_pair）、端点未登记或仅为空容器
  （422 source_not_initial_sample）；校验失败时旧规则保持不变；
* GET /compatibility/rules 返回规范化排序后的规则；
* 批量提交与预演复用同一顺序模拟：每一步分摊后检查目标容器的非零祖先集合，
  命中规则返回 422 incompatible_sources（条目序号、目标容器、排序后的全部冲突对）；
* 同批前序混合产生的隐性祖先参与后续检查；先转出清空后可合法混合；
  提交整批回滚（状态 / 版本 / 幂等记录 / 审计链均不变），预演不写库；
* 幂等重放返回首次成功结果（不重新校验规则）；
* 旧库启动时幂等迁移 compatibility_rules 表与 containers.is_initial_sample 列。
"""
from helpers import container, sample, volume_of

from app.db import connect


def _put_rules(client, rules):
    return client.put("/compatibility/rules", json={"rules": rules})


def _get_rules(client):
    r = client.get("/compatibility/rules")
    assert r.status_code == 200, r.text
    return r.json()


def _rules_body(pairs):
    return {"rules": [{"source_a": a, "source_b": b} for a, b in pairs]}


def _audit_count(client):
    return client.get("/audit/records").json()["count"]


def _idempotency_count():
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 规则配置：全量替换、去重、规范化排序
# --------------------------------------------------------------------------- #
def test_get_empty_rules_by_default(client):
    assert _get_rules(client) == {"count": 0, "rules": []}


def test_put_rules_dedup_undirected_and_normalizes_order(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    sample(client, "S3", "10")

    # B-A 与 A-B 是同一条无向对；重复条目去重；返回按 (source_a, source_b) 排序
    r = _put_rules(
        client,
        [
            {"source_a": "S3", "source_b": "S1"},
            {"source_a": "S1", "source_b": "S2"},
            {"source_a": "S1", "source_b": "S3"},  # 与首条反向重复
        ],
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "count": 2,
        "rules": [
            {"source_a": "S1", "source_b": "S2"},
            {"source_a": "S1", "source_b": "S3"},
        ],
    }
    # GET 返回同样的规范化结果
    assert _get_rules(client) == r.json()

    # 全量替换：只传一条则旧规则中另一条被删除
    r = _put_rules(client, [{"source_a": "S2", "source_b": "S3"}])
    assert r.status_code == 200
    assert r.json() == {"count": 1, "rules": [{"source_a": "S2", "source_b": "S3"}]}


def test_put_empty_list_clears_all_rules(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    assert _put_rules(client, [{"source_a": "S1", "source_b": "S2"}]).status_code == 200
    assert _get_rules(client)["count"] == 1

    r = client.put("/compatibility/rules", json={"rules": []})
    assert r.status_code == 200, r.text
    assert r.json() == {"count": 0, "rules": []}
    assert _get_rules(client)["count"] == 0


def test_put_rules_self_pair_rejected(client):
    sample(client, "S1", "10")
    r = _put_rules(client, [{"source_a": "S1", "source_b": "S1"}])
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "incompatible_self_pair"
    assert "第 1 条规则" in err["message"]
    assert err["details"] == {"index": 1, "source_id": "S1"}
    # 拒绝后规则表仍为空
    assert _get_rules(client)["count"] == 0

    # 第二条才是自配对：序号按请求数组位置给出
    sample(client, "S2", "10")
    r = _put_rules(
        client,
        [
            {"source_a": "S1", "source_b": "S2"},
            {"source_a": "S2", "source_b": "S2"},
        ],
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"] == {"index": 2, "source_id": "S2"}
    assert _get_rules(client)["count"] == 0


def test_put_rules_unknown_and_non_sample_endpoints_rejected(client):
    sample(client, "S1", "10")
    container(client, "V1")  # 已登记但只是空容器，不是初始样品

    for endpoint in ("GHOST", "V1"):
        r = _put_rules(client, [{"source_a": "S1", "source_b": endpoint}])
        assert r.status_code == 422, endpoint
        err = r.json()["error"]
        assert err["code"] == "source_not_initial_sample", endpoint
        assert err["details"] == {"source_id": endpoint}, endpoint
        assert _get_rules(client)["count"] == 0


def test_put_rules_atomic_replace_keeps_old_rules_on_failure(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    assert _put_rules(client, [{"source_a": "S1", "source_b": "S2"}]).status_code == 200

    # 新列表含非法端点：整批拒绝，旧规则原样保留
    r = _put_rules(
        client,
        [
            {"source_a": "S1", "source_b": "S2"},
            {"source_a": "S1", "source_b": "GHOST"},
        ],
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "source_not_initial_sample"
    assert _get_rules(client) == {
        "count": 1,
        "rules": [{"source_a": "S1", "source_b": "S2"}],
    }


def test_put_rules_does_not_touch_versions_or_audit(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    assert _put_rules(client, [{"source_a": "S1", "source_b": "S2"}]).status_code == 200
    # 规则配置不是批量转移：容器版本与审计链都不变
    assert client.get("/containers/S1").json()["version"] == 0
    assert _audit_count(client) == 0


# --------------------------------------------------------------------------- #
# 批量提交：祖先来源不相容
# --------------------------------------------------------------------------- #
def _register_three_samples(client):
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    sample(client, "S3", "100")
    container(client, "V1")


def test_batch_incompatible_sources_rejects_and_rolls_back(client):
    _register_three_samples(client)
    assert _put_rules(client, [{"source_a": "S1", "source_b": "S2"}]).status_code == 200

    # 第一条合法（V1 只有 S1），第二条把 S2 混入 V1：命中规则，整批回滚
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "10"},
                {"source": "S2", "destination": "V1", "volume": "10"},
            ]
        },
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "incompatible_sources"
    assert "第 2 条转移" in err["message"]
    assert err["details"] == {
        "index": 2,
        "destination": "V1",
        "conflicts": [{"source_a": "S1", "source_b": "S2"}],
    }

    # 整批回滚：体积 / 版本 / 审计 / 幂等均不变
    assert volume_of(client, "S1") == "100.000000000"
    assert volume_of(client, "S2") == "100.000000000"
    assert volume_of(client, "V1") == "0"
    assert client.get("/containers/V1").json()["version"] == 0
    assert _audit_count(client) == 0
    assert _idempotency_count() == 0


def test_batch_reports_all_conflicting_pairs_sorted(client):
    """目标一次混入多个来源时，details 给出排序后的全部冲突对。"""
    _register_three_samples(client)
    # S1-S2、S1-S3、S2-S3 两两禁配；S1、S2 已在 V1 时 S3 转入会同时命中两对
    assert _put_rules(
        client,
        [
            {"source_a": "S1", "source_b": "S3"},
            {"source_a": "S2", "source_b": "S3"},
        ],
    ).status_code == 200

    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "5"},
                {"source": "S2", "destination": "V1", "volume": "5"},
                {"source": "S3", "destination": "V1", "volume": "5"},
            ]
        },
    )
    assert r.status_code == 422
    details = r.json()["error"]["details"]
    assert details["index"] == 3
    assert details["destination"] == "V1"
    assert details["conflicts"] == [
        {"source_a": "S1", "source_b": "S3"},
        {"source_a": "S2", "source_b": "S3"},
    ]
    # 整批回滚
    assert volume_of(client, "V1") == "0"


def test_rule_direction_is_undirected(client):
    """规则存成 B-A，A 转入 B 所在容器与 B 转入 A 所在容器同样被拒。"""
    _register_three_samples(client)
    # 故意反向提交
    assert _put_rules(client, [{"source_a": "S2", "source_b": "S1"}]).status_code == 200
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S2", "destination": "V1", "volume": "1"},
                {"source": "S1", "destination": "V1", "volume": "1"},
            ]
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "incompatible_sources"
    assert volume_of(client, "V1") == "0"


def test_implicit_ancestors_from_prior_mixing_participate_in_later_checks(client):
    """同批前序混合产生的隐性祖先必须参与后续步骤的检查。"""
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    sample(client, "S3", "100")
    container(client, "V1")
    container(client, "V2")
    # S1-S3 禁配。批内：V1 先混入 S1+S2（合法），再把 V1 的混合液转入 V2，
    # 最后向 V2 加入 S3 —— V2 里的 S1 是同批前序传播来的隐性祖先，仍须命中。
    assert _put_rules(client, [{"source_a": "S1", "source_b": "S3"}]).status_code == 200

    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "10"},
                {"source": "S2", "destination": "V1", "volume": "10"},
                {"source": "V1", "destination": "V2", "volume": "10"},
                {"source": "S3", "destination": "V2", "volume": "5"},
            ]
        },
    )
    assert r.status_code == 422
    details = r.json()["error"]["details"]
    assert details["index"] == 4
    assert details["destination"] == "V2"
    assert details["conflicts"] == [{"source_a": "S1", "source_b": "S3"}]
    # 整批回滚：四步全部不留痕
    assert volume_of(client, "S1") == "100.000000000"
    assert volume_of(client, "V1") == "0"
    assert volume_of(client, "V2") == "0"


def test_draining_incompatible_source_out_allows_mixing(client):
    """目标先把不相容祖先完全转出（非零集合不再包含它），随后混合合法。"""
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    container(client, "V1")
    container(client, "V2")
    assert _put_rules(client, [{"source_a": "S1", "source_b": "S2"}]).status_code == 200

    # V1 装入 10 的 S1，随后整笔转到 V2；再向空的 V1 装入 S2：合法
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "10"},
                {"source": "V1", "destination": "V2", "volume": "10"},
                {"source": "S2", "destination": "V1", "volume": "10"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert volume_of(client, "V1") == "10.000000000"
    assert client.get("/containers/V1").json()["sources"] == [
        {"source_id": "S2", "amount": "10.000000000", "proportion": "1.000000"}
    ]
    assert volume_of(client, "V2") == "10.000000000"


def test_no_rule_means_no_restriction(client):
    """规则表为空时行为与旧版本完全一致。"""
    _register_three_samples(client)
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "10"},
                {"source": "S2", "destination": "V1", "volume": "10"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert volume_of(client, "V1") == "20.000000000"


def test_rule_removal_lifts_restriction(client):
    """全量替换删掉规则后，原先被拒的混合立即合法。"""
    _register_three_samples(client)
    assert _put_rules(client, [{"source_a": "S1", "source_b": "S2"}]).status_code == 200
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "1"},
                {"source": "S2", "destination": "V1", "volume": "1"},
            ]
        },
    )
    assert r.status_code == 422

    assert client.put("/compatibility/rules", json={"rules": []}).status_code == 200
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "1"},
                {"source": "S2", "destination": "V1", "volume": "1"},
            ]
        },
    )
    assert r.status_code == 200, r.text


def test_compatible_mixing_unaffected(client):
    """只禁 S1-S2 时，S1 与 S3、S2 与 S3 混合均合法。"""
    _register_three_samples(client)
    assert _put_rules(client, [{"source_a": "S1", "source_b": "S2"}]).status_code == 200
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "10"},
                {"source": "S3", "destination": "V1", "volume": "10"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert volume_of(client, "V1") == "20.000000000"


# --------------------------------------------------------------------------- #
# 预演：同一套检查、不落库
# --------------------------------------------------------------------------- #
def test_preview_enforces_compatibility_and_persists_nothing(client):
    _register_three_samples(client)
    assert _put_rules(client, [{"source_a": "S1", "source_b": "S2"}]).status_code == 200

    payload = {
        "transfers": [
            {"source": "S1", "destination": "V1", "volume": "10"},
            {"source": "S2", "destination": "V1", "volume": "10"},
        ]
    }
    r = client.post("/transfers/preview", json=payload)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "incompatible_sources"
    assert err["details"]["index"] == 2
    assert err["details"]["destination"] == "V1"
    assert err["details"]["conflicts"] == [{"source_a": "S1", "source_b": "S2"}]
    # 预演失败不写库
    assert volume_of(client, "V1") == "0"

    # 兼容的预演成功，同样不落库
    ok_payload = {
        "transfers": [
            {"source": "S1", "destination": "V1", "volume": "10"},
            {"source": "S3", "destination": "V1", "volume": "10"},
        ]
    }
    r = client.post("/transfers/preview", json=ok_payload)
    assert r.status_code == 200, r.text
    assert volume_of(client, "V1") == "0"
    assert client.get("/containers/V1").json()["version"] == 0


def test_preview_compatibility_error_identical_to_batch(client):
    _register_three_samples(client)
    assert _put_rules(client, [{"source_a": "S1", "source_b": "S2"}]).status_code == 200
    payload = {
        "transfers": [
            {"source": "S1", "destination": "V1", "volume": "3"},
            {"source": "S2", "destination": "V1", "volume": "4"},
        ]
    }
    preview_resp = client.post("/transfers/preview", json=payload)
    batch_resp = client.post("/transfers/batch", json=payload)
    assert preview_resp.status_code == batch_resp.status_code == 422
    assert preview_resp.json() == batch_resp.json()
    assert volume_of(client, "V1") == "0"


def test_preview_implicit_ancestors_checked(client):
    """预演同样对同批前序混合出的隐性祖先做检查。"""
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    container(client, "V1")
    container(client, "V2")
    assert _put_rules(client, [{"source_a": "S1", "source_b": "S2"}]).status_code == 200

    r = client.post(
        "/transfers/preview",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "10"},
                {"source": "V1", "destination": "V2", "volume": "10"},
                {"source": "S2", "destination": "V2", "volume": "5"},
            ]
        },
    )
    assert r.status_code == 422
    details = r.json()["error"]["details"]
    assert details["index"] == 3
    assert details["destination"] == "V2"
    assert volume_of(client, "V2") == "0"


# --------------------------------------------------------------------------- #
# 幂等：失败不留痕；重放返回首次结果
# --------------------------------------------------------------------------- #
def test_incompatible_failure_with_key_writes_no_idempotency_record(client):
    _register_three_samples(client)
    assert _put_rules(client, [{"source_a": "S1", "source_b": "S2"}]).status_code == 200

    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "1"},
                {"source": "S2", "destination": "V1", "volume": "1"},
            ],
            "idempotency_key": "compat-key",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "incompatible_sources"
    assert _idempotency_count() == 0
    assert _audit_count(client) == 0

    # 同键改成兼容的请求后可以成功
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "1"},
                {"source": "S3", "destination": "V1", "volume": "1"},
            ],
            "idempotency_key": "compat-key",
        },
    )
    assert r.status_code == 200, r.text
    assert _idempotency_count() == 1


def test_idempotent_replay_keeps_original_result_after_rule_added(client):
    """首次成功后再新增命中的规则，同键重放仍返回首次结果（不重新校验规则）。"""
    _register_three_samples(client)
    payload = {
        "transfers": [
            {"source": "S1", "destination": "V1", "volume": "10"},
            {"source": "S2", "destination": "V1", "volume": "10"},
        ],
        "idempotency_key": "replay-compat",
    }
    r1 = client.post("/transfers/batch", json=payload)
    assert r1.status_code == 200, r1.text
    assert volume_of(client, "V1") == "20.000000000"

    # 事后新增 S1-S2 禁配：若重新执行必然 422，但重放必须返回首次结果
    assert _put_rules(client, [{"source_a": "S1", "source_b": "S2"}]).status_code == 200
    r2 = client.post("/transfers/batch", json=payload)
    assert r2.status_code == 200
    assert r2.headers.get("Idempotency-Replayed") == "true"
    assert r2.json() == r1.json()
    # 不重复扣减、不重复递增版本、不产生新审计
    assert volume_of(client, "V1") == "20.000000000"
    assert client.get("/containers/V1").json()["version"] == 1
    assert _audit_count(client) == 1


# --------------------------------------------------------------------------- #
# 旧库迁移
# --------------------------------------------------------------------------- #
def test_init_db_migrates_legacy_database_with_rules_support(tmp_path, monkeypatch):
    import sqlite3

    legacy_path = tmp_path / "legacy.db"

    # 旧 schema：containers 没有 is_initial_sample 列，也没有 compatibility_rules 表
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
            -- 初始样品：自祖先行 (S1, S1)
            INSERT INTO containers (id) VALUES ('S1');
            INSERT INTO components (container_id, source_id, amount)
                VALUES ('S1', 'S1', '10.000000000');
            -- 空容器：无 components 行
            INSERT INTO containers (id) VALUES ('V1');
            -- 混合容器：components 中没有 (id, id) 行，不应被误判为初始样品
            INSERT INTO containers (id) VALUES ('M1');
            INSERT INTO components (container_id, source_id, amount)
                VALUES ('M1', 'S1', '10.000000000');
            """
        )
        raw.commit()
    finally:
        raw.close()

    monkeypatch.setattr("app.config.settings.database_path", str(legacy_path))

    from app.db import connect as db_connect, init_db

    init_db()  # 执行迁移
    init_db()  # 再次执行必须幂等

    conn = db_connect()
    try:
        flags = {
            r["id"]: r["is_initial_sample"]
            for r in conn.execute("SELECT id, is_initial_sample FROM containers")
        }
        assert flags == {"S1": 1, "V1": 0, "M1": 0}

        # compatibility_rules 表已建好：自配对会被 CHECK 拒绝，
        # 正常的规范对可以写入
        import pytest

        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO compatibility_rules VALUES ('S1', 'S1')")
        conn.execute("ROLLBACK")

        from app import service
        from decimal import Decimal

        # 迁移识别出的初始样品可作为规则端点；空容器 V1 仍不行
        with pytest.raises(service.LineageError) as exc:
            service.replace_compatibility_rules(conn, [("S1", "V1")])
        assert exc.value.code == "source_not_initial_sample"

        # 登记一个新初始样品后规则与转移校验全部可用
        service.register_initial_sample(conn, "S2", Decimal("10"))
        service.register_empty_container(conn, "V2")
        service.replace_compatibility_rules(conn, [("S1", "S2")])
        with pytest.raises(service.LineageError) as exc:
            service.apply_transfers(
                conn,
                [
                    ("S1", "V2", Decimal("1")),
                    ("S2", "V2", Decimal("1")),
                ],
            )
        assert exc.value.code == "incompatible_sources"
        assert exc.value.details["index"] == 2
    finally:
        conn.close()
