"""容器容量约束端到端测试。

覆盖：
* PUT /containers/{id}/capacity 设置 / 修改 / 清除（null）容量上限；
  上限必须不小于当前体积（422 capacity_below_volume），参数非法 422 invalid_volume；
* 容器查询返回 capacity / remaining / utilization；
* 批量提交与预演在顺序模拟的每一步检查“目标当前体积 + 转入量”，
  任一目标超上限整批拒绝（422 capacity_exceeded，含条目序号 / 目标 / 预计超出量），
  失败时容器状态、版本、幂等记录与审计链均不变化；
* 同批先转出再转入按当时余量判断；恰好达到上限允许；
* 已成功请求的幂等重放保持原结果（不重新校验容量）；
* 旧库启动时幂等迁移 capacity 列（已有容器为 NULL = 不限制）。
"""
import sqlite3
from decimal import Decimal

from helpers import container, sample, volume_of

from app.db import connect


def _set_capacity(client, cid, capacity):
    return client.put(f"/containers/{cid}/capacity", json={"capacity": capacity})


def _status(client, cid):
    r = client.get(f"/containers/{cid}")
    assert r.status_code == 200, r.text
    return r.json()


def _version_of(client, cid):
    return _status(client, cid)["version"]


def _audit_count(client):
    return client.get("/audit/records").json()["count"]


def _idempotency_count():
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 设置 / 修改 / 清除容量上限
# --------------------------------------------------------------------------- #
def test_set_update_and_clear_capacity(client):
    sample(client, "S1", "100")
    container(client, "V1")

    # 未设置：不限制，capacity / remaining / utilization 均为 null
    body = _status(client, "V1")
    assert body["capacity"] is None
    assert body["remaining"] is None
    assert body["utilization"] is None

    # 设置容量 50：空容器剩余 50、利用率 0
    r = _set_capacity(client, "V1", "50")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["container_id"] == "V1"
    assert body["capacity"] == "50.000000000"
    assert body["remaining"] == "50.000000000"
    assert body["utilization"] == "0.000000"

    # 转入 30 后：剩余 20、利用率 0.6
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "30"}]},
    )
    assert r.status_code == 200, r.text
    body = _status(client, "V1")
    assert body["volume"] == "30.000000000"
    assert body["capacity"] == "50.000000000"
    assert body["remaining"] == "20.000000000"
    assert body["utilization"] == "0.600000"

    # 修改容量为 30（恰好等于当前体积）：剩余 0、利用率 1
    r = _set_capacity(client, "V1", "30")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["capacity"] == "30.000000000"
    assert body["remaining"] == "0.000000000"
    assert body["utilization"] == "1.000000"

    # 清除容量（null）：恢复不限制
    r = _set_capacity(client, "V1", None)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["capacity"] is None
    assert body["remaining"] is None
    assert body["utilization"] is None
    assert body["volume"] == "30.000000000"


def test_capacity_equal_to_current_volume_allowed(client):
    sample(client, "S1", "10")
    r = _set_capacity(client, "S1", "10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["capacity"] == "10.000000000"
    assert body["remaining"] == "0.000000000"
    assert body["utilization"] == "1.000000"


def test_capacity_zero_on_empty_container(client):
    container(client, "V1")
    r = _set_capacity(client, "V1", "0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["capacity"] == "0.000000000"
    assert body["remaining"] == "0.000000000"
    # 容量为 0 时利用率无定义（0/0）
    assert body["utilization"] is None

    # 容量为 0 的容器拒绝任何转入
    sample(client, "S1", "5")
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "1"}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "capacity_exceeded"


def test_capacity_below_current_volume_rejected(client):
    sample(client, "S1", "100")
    container(client, "V1")
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "40"}]},
    )
    assert r.status_code == 200

    # 上限 39.5 < 当前体积 40：拒绝，容量保持不限制
    r = _set_capacity(client, "V1", "39.5")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "capacity_below_volume"
    assert "V1" in err["message"]
    assert err["details"]["current_volume"] == "40.000000000"
    assert err["details"]["capacity"] == "39.500000000"
    assert _status(client, "V1")["capacity"] is None

    # 恰好 40 则允许
    r = _set_capacity(client, "V1", "40")
    assert r.status_code == 200


def test_capacity_change_does_not_touch_version_or_audit(client):
    sample(client, "S1", "10")
    container(client, "V1")
    assert _audit_count(client) == 0

    r = _set_capacity(client, "V1", "5")
    assert r.status_code == 200
    # 容量修改不是批量转移：version 不变、不产生审计记录
    assert _version_of(client, "V1") == 0
    assert _version_of(client, "S1") == 0
    assert _audit_count(client) == 0


def test_set_capacity_unknown_container_404(client):
    r = _set_capacity(client, "GHOST", "10")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_container"


def test_set_capacity_invalid_values_rejected(client):
    container(client, "V1")
    # 负数 / 超出精度：业务校验 422 invalid_volume
    for bad in ("-1", "1.0000000001"):
        r = _set_capacity(client, "V1", bad)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"]["code"] == "invalid_volume", bad
    # 非有限数：与 /samples 的 volume 一样在请求模型层被拦截（框架级 422）
    for bad in ("NaN", "Infinity"):
        r = _set_capacity(client, "V1", bad)
        assert r.status_code == 422, (bad, r.text)
    # 全部失败：容量保持不限制
    assert _status(client, "V1")["capacity"] is None


# --------------------------------------------------------------------------- #
# 批量提交：目标容量超限整批拒绝
# --------------------------------------------------------------------------- #
def test_batch_capacity_exceeded_rejects_whole_batch(client):
    sample(client, "S1", "100")
    container(client, "V1")
    container(client, "V2")
    _set_capacity(client, "V2", "10")

    # 第一条合法、第二条使 V2 超限：整批回滚，第一条也不留痕
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "5"},
                {"source": "S1", "destination": "V2", "volume": "20"},
            ]
        },
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "capacity_exceeded"
    # 返回条目序号、目标与预计超出量（消息与结构化 details 双重呈现）
    assert "第 2 条转移" in err["message"]
    assert "V2" in err["message"]
    assert "10.000000000" in err["message"]
    assert err["details"] == {
        "index": 2,
        "destination": "V2",
        "current_volume": "0",
        "incoming": "20.000000000",
        "projected_volume": "20.000000000",
        "capacity": "10.000000000",
        "excess": "10.000000000",
    }

    # 整批回滚：体积、版本、审计链均不变化
    assert volume_of(client, "S1") == "100.000000000"
    assert volume_of(client, "V1") == "0"
    assert volume_of(client, "V2") == "0"
    assert _version_of(client, "S1") == 0
    assert _version_of(client, "V1") == 0
    assert _version_of(client, "V2") == 0
    assert _audit_count(client) == 0


def test_batch_capacity_exceeded_reports_current_volume_at_step(client):
    """同批前序转入计入目标的“当前体积”，超出量按当时余量计算。"""
    sample(client, "S1", "100")
    container(client, "V1")
    _set_capacity(client, "V1", "10")

    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "8"},
                # 此时 V1 已有 8，再转入 5 -> 13，超出 3
                {"source": "S1", "destination": "V1", "volume": "5"},
            ]
        },
    )
    assert r.status_code == 422
    details = r.json()["error"]["details"]
    assert details["index"] == 2
    assert details["destination"] == "V1"
    assert details["current_volume"] == "8.000000000"
    assert details["projected_volume"] == "13.000000000"
    assert details["excess"] == "3.000000000"
    # 整批回滚
    assert volume_of(client, "V1") == "0"
    assert volume_of(client, "S1") == "100.000000000"


def test_batch_exactly_at_capacity_allowed(client):
    sample(client, "S1", "100")
    container(client, "V1")
    _set_capacity(client, "V1", "10")

    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "10"}]},
    )
    assert r.status_code == 200, r.text
    body = _status(client, "V1")
    assert body["volume"] == "10.000000000"
    assert body["remaining"] == "0.000000000"
    assert body["utilization"] == "1.000000"

    # 已满：再转入任意正体积都超限
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "0.000000001"}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "capacity_exceeded"
    assert r.json()["error"]["details"]["excess"] == "0.000000001"


def test_batch_transfer_out_then_in_uses_headroom_at_that_moment(client):
    """同批先转出再转入按当时余量判断：腾出空间后的转入合法。"""
    sample(client, "S1", "100")
    container(client, "V1")
    container(client, "V2")
    _set_capacity(client, "V1", "10")
    # V1 先装 8
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "8"}]},
    )
    assert r.status_code == 200

    # 同批：V1 转出 5（余 3），再转入 6 -> 9 <= 10，合法
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "V1", "destination": "V2", "volume": "5"},
                {"source": "S1", "destination": "V1", "volume": "6"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert volume_of(client, "V1") == "9.000000000"

    # 若顺序反过来（先转入 6：8+6=14 > 10），同一批量也会被拒
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "6"},
                {"source": "V1", "destination": "V2", "volume": "5"},
            ]
        },
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "capacity_exceeded"
    assert err["details"]["index"] == 1
    assert err["details"]["current_volume"] == "9.000000000"
    assert err["details"]["excess"] == "5.000000000"
    # 整批回滚
    assert volume_of(client, "V1") == "9.000000000"


def test_capacity_check_only_constrains_destination(client):
    """容量只约束转入目标：源容器即使体积远超其容量上限也可正常转出。"""
    sample(client, "S1", "100")
    container(client, "V1")
    # 给 S1 设置一个比当前体积小的上限会被拒；先设 100 再转入不受限场景
    _set_capacity(client, "S1", "100")
    # S1 作为源转出不受容量影响（容量只检查目标）
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "60"}]},
    )
    assert r.status_code == 200, r.text
    assert volume_of(client, "S1") == "40.000000000"
    # 转出后 S1 体积 40 < 容量 100，可把上限收紧到 40
    r = _set_capacity(client, "S1", "40")
    assert r.status_code == 200


def test_unlimited_capacity_by_default(client):
    """不设置容量时行为与旧版本一致：任意体积转入都不受容量限制。"""
    sample(client, "S1", "1000000")
    container(client, "V1")
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "999999"}]},
    )
    assert r.status_code == 200, r.text
    assert volume_of(client, "V1") == "999999.000000000"


# --------------------------------------------------------------------------- #
# 预演：同样的容量校验，且不落库
# --------------------------------------------------------------------------- #
def test_preview_enforces_capacity_and_persists_nothing(client):
    sample(client, "S1", "100")
    container(client, "V1")
    _set_capacity(client, "V1", "10")

    r = client.post(
        "/transfers/preview",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "11"}]},
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "capacity_exceeded"
    assert err["details"]["index"] == 1
    assert err["details"]["destination"] == "V1"
    assert err["details"]["excess"] == "1.000000000"
    # 预演失败不落库
    assert volume_of(client, "S1") == "100.000000000"
    assert volume_of(client, "V1") == "0"
    assert _version_of(client, "V1") == 0

    # 上限内的预演成功，且返回容量信息（remaining 按预演结束时的体积计算）
    r = client.post(
        "/transfers/preview",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "4"}]},
    )
    assert r.status_code == 200, r.text
    finals = {c["container_id"]: c for c in r.json()["containers"]}
    assert finals["V1"]["capacity"] == "10.000000000"
    assert finals["V1"]["remaining"] == "6.000000000"
    assert finals["V1"]["utilization"] == "0.400000"
    # 成功的预演同样不落库
    assert volume_of(client, "V1") == "0"
    assert _version_of(client, "V1") == 0


def test_preview_capacity_error_identical_to_batch(client):
    sample(client, "S1", "50")
    container(client, "V1")
    _set_capacity(client, "V1", "10")
    payload = {"transfers": [{"source": "S1", "destination": "V1", "volume": "20"}]}

    preview_resp = client.post("/transfers/preview", json=payload)
    batch_resp = client.post("/transfers/batch", json=payload)
    assert preview_resp.status_code == batch_resp.status_code == 422
    assert preview_resp.json() == batch_resp.json()
    assert preview_resp.json()["error"]["code"] == "capacity_exceeded"
    assert volume_of(client, "V1") == "0"


# --------------------------------------------------------------------------- #
# 幂等：失败不写记录；已成功请求的重放保持原结果
# --------------------------------------------------------------------------- #
def test_capacity_failure_with_key_writes_no_idempotency_record(client):
    sample(client, "S1", "100")
    container(client, "V1")
    _set_capacity(client, "V1", "10")

    # 带幂等键的容量超限失败：不落幂等记录，同键可修正后重试
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [{"source": "S1", "destination": "V1", "volume": "20"}],
            "idempotency_key": "cap-key",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "capacity_exceeded"
    assert _idempotency_count() == 0
    assert _audit_count(client) == 0

    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [{"source": "S1", "destination": "V1", "volume": "5"}],
            "idempotency_key": "cap-key",
        },
    )
    assert r.status_code == 200, r.text
    assert _idempotency_count() == 1
    assert volume_of(client, "V1") == "5.000000000"


def test_idempotent_replay_keeps_original_result_after_capacity_change(client):
    """首次成功后把容量改小，同键重放仍返回首次结果（不重新校验容量）。"""
    sample(client, "S1", "100")
    container(client, "V1")
    _set_capacity(client, "V1", "10")
    payload = {
        "transfers": [{"source": "S1", "destination": "V1", "volume": "4"}],
        "idempotency_key": "replay-cap",
    }
    r1 = client.post("/transfers/batch", json=payload)
    assert r1.status_code == 200, r1.text

    # 收紧容量到 4（等于当前体积，合法）：同样的请求体现在执行必然超限
    r = _set_capacity(client, "V1", "4")
    assert r.status_code == 200

    # 重放：返回首次结果，不重复扣减、不重复递增版本、不产生新审计
    r2 = client.post("/transfers/batch", json=payload)
    assert r2.status_code == 200
    assert r2.headers.get("Idempotency-Replayed") == "true"
    assert r2.json() == r1.json()
    assert volume_of(client, "V1") == "4.000000000"
    assert _version_of(client, "V1") == 1
    assert _audit_count(client) == 1


# --------------------------------------------------------------------------- #
# 旧库迁移：启动时幂等补齐 capacity 列
# --------------------------------------------------------------------------- #
def test_init_db_migrates_legacy_database_without_capacity(tmp_path, monkeypatch):
    legacy_path = tmp_path / "legacy.db"

    # 旧 schema：containers 有 version 但没有 capacity 列
    raw = sqlite3.connect(legacy_path)
    try:
        raw.executescript(
            """
            CREATE TABLE containers (
                id      TEXT PRIMARY KEY,
                version INTEGER NOT NULL DEFAULT 0,
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

    from app.db import connect as db_connect, init_db
    from app import service

    init_db()  # 执行迁移
    init_db()  # 再次执行必须幂等，不报错、不改变数据

    conn = db_connect()
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(containers)")}
        assert "capacity" in columns
        # 旧数据完好，容量一律回填为 NULL（不限制）
        assert conn.execute(
            "SELECT capacity FROM containers WHERE id = 'OLD'"
        ).fetchone()[0] is None

        # 迁移后旧容器查询返回容量字段（不限制）
        status = service.get_container_status(conn, "OLD")
        assert status["capacity"] is None
        assert status["remaining"] is None
        assert status["utilization"] is None
        assert status["volume"] == "5.500000000"

        # 迁移后的旧容器可正常设置容量并参与容量校验
        service.set_container_capacity(conn, "OLD", Decimal("6"))
        assert service.get_container_status(conn, "OLD")["capacity"] == "6.000000000"
        service.register_empty_container(conn, "NEW")
        service.set_container_capacity(conn, "NEW", Decimal("1"))
        import pytest
        from app.service import LineageError

        with pytest.raises(LineageError) as exc:
            service.apply_transfers(conn, [("OLD", "NEW", Decimal("2"))])
        assert exc.value.code == "capacity_exceeded"
        assert exc.value.details["excess"] == "1.000000000"
    finally:
        conn.close()
