"""审计链模块测试：写入时机、哈希链、查询、篡改检测、只追加触发器。"""
import sqlite3

import pytest

from app import audit
from app.db import connect
from helpers import container, sample


def _batch(client, transfers, key=None):
    body = {"transfers": transfers}
    if key is not None:
        body["idempotency_key"] = key
    return client.post("/transfers/batch", json=body)


def _seed_two_samples_and_vials(client):
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    container(client, "V1")
    container(client, "V2")


# --------------------------------------------------------------------------- #
# 写入时机：每个成功批次一条，失败 / 预演 / 重放不写
# --------------------------------------------------------------------------- #
def test_successful_batch_writes_one_audit_record(client):
    _seed_two_samples_and_vials(client)
    r = _batch(
        client,
        [
            {"source": "S1", "destination": "V1", "volume": "40"},
            {"source": "S2", "destination": "V1", "volume": "60"},
        ],
    )
    assert r.status_code == 200
    body = client.get("/audit/records").json()
    assert body["count"] == 1
    rec = body["records"][0]
    assert rec["seq"] == 1
    assert rec["prev_hash"] == audit.GENESIS_PREV_HASH
    assert rec["summary"] == {
        "transfer_count": 2,
        "total_volume": "100.000000000",
        "containers": ["S1", "S2", "V1"],
        "idempotency_key": None,
    }
    assert rec["result_versions"] == {"S1": 1, "S2": 1, "V1": 1}


def test_failed_batch_writes_no_audit_record(client):
    _seed_two_samples_and_vials(client)
    # 源液不足：整批回滚，审计也不留痕
    r = _batch(
        client,
        [
            {"source": "S1", "destination": "V1", "volume": "5"},
            {"source": "S1", "destination": "V2", "volume": "9999"},
        ],
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "insufficient_volume"
    body = client.get("/audit/records").json()
    assert body["count"] == 0
    v = client.get("/audit/verify").json()
    assert v["valid"] is True
    assert v["records"] == 0


@pytest.mark.parametrize(
    "payload,error_code",
    [
        ({"transfers": [{"source": "S1", "destination": "S1", "volume": "1"}]},
         "self_transfer"),
        ({"transfers": [{"source": "GHOST", "destination": "V1", "volume": "1"}]},
         "unknown_container"),
        ({"transfers": [{"source": "S1", "destination": "V1", "volume": "0"}]},
         "invalid_volume"),
    ],
)
def test_all_failure_modes_write_no_audit_record(client, payload, error_code):
    sample(client, "S1", "10")
    container(client, "V1")
    r = client.post("/transfers/batch", json=payload)
    assert r.status_code in (404, 422)
    assert r.json()["error"]["code"] == error_code
    assert client.get("/audit/records").json()["count"] == 0


def test_preview_never_writes_audit_record(client):
    _seed_two_samples_and_vials(client)
    r = client.post(
        "/transfers/preview",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "40"},
                {"source": "V1", "destination": "V2", "volume": "10"},
            ]
        },
    )
    assert r.status_code == 200
    assert client.get("/audit/records").json()["count"] == 0

    # 预演失败也不留痕
    r = client.post(
        "/transfers/preview",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "9999"}]},
    )
    assert r.status_code == 422
    assert client.get("/audit/records").json()["count"] == 0


def test_idempotent_replay_does_not_add_audit_record(client):
    _seed_two_samples_and_vials(client)
    payload = {
        "transfers": [{"source": "S1", "destination": "V1", "volume": "10"}],
        "idempotency_key": "replay-key",
    }
    r1 = client.post("/transfers/batch", json=payload)
    assert r1.status_code == 200
    assert client.get("/audit/records").json()["count"] == 1

    r2 = client.post("/transfers/batch", json=payload)
    assert r2.status_code == 200
    assert r2.headers.get("idempotency-replayed") == "true"
    body = client.get("/audit/records").json()
    assert body["count"] == 1
    assert body["records"][0]["seq"] == 1


def test_idempotent_key_recorded_in_summary(client):
    _seed_two_samples_and_vials(client)
    _batch(
        client,
        [{"source": "S1", "destination": "V1", "volume": "10"}],
        key="key-abc",
    )
    rec = client.get("/audit/records").json()["records"][0]
    assert rec["summary"]["idempotency_key"] == "key-abc"


# --------------------------------------------------------------------------- #
# 哈希链
# --------------------------------------------------------------------------- #
def test_hash_chain_links_to_previous_record(client):
    _seed_two_samples_and_vials(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "10"}])
    _batch(client, [{"source": "S2", "destination": "V1", "volume": "20"}])
    _batch(client, [{"source": "V1", "destination": "V2", "volume": "5"}])

    body = client.get("/audit/records").json()
    records = body["records"]
    assert [r["seq"] for r in records] == [1, 2, 3]
    assert records[0]["prev_hash"] == audit.GENESIS_PREV_HASH
    assert records[1]["prev_hash"] == records[0]["entry_hash"]
    assert records[2]["prev_hash"] == records[1]["entry_hash"]

    # entry_hash 可独立按公式复算
    for r in records:
        from app.audit import canonical_json, compute_entry_hash

        payload = {k: r[k] for k in ("summary", "transfers", "containers", "result_versions")}
        payload["seq"] = r["seq"]
        recomputed = compute_entry_hash(r["seq"], r["prev_hash"], canonical_json(payload))
        assert recomputed == r["entry_hash"]

    v = client.get("/audit/verify").json()
    assert v["valid"] is True
    assert v["records"] == 3
    assert v["break"] is None
    assert all(check["ok"] for check in v["checks"].values())


def test_verify_empty_chain_is_valid(client):
    v = client.get("/audit/verify").json()
    assert v == {
        "valid": True,
        "records": 0,
        "first_seq": None,
        "max_seq": None,
        "break": None,
        "checks": {
            "sequence": {"ok": True, "detail": None},
            "payload_integrity": {"ok": True, "detail": None},
            "hash_chain": {"ok": True, "detail": None},
            "container_index": {"ok": True, "detail": None},
        },
    }


# --------------------------------------------------------------------------- #
# 受影响容器与结果版本
# --------------------------------------------------------------------------- #
def test_record_captures_before_after_versions_and_volumes(client):
    _seed_two_samples_and_vials(client)
    # 第一批：S1 -> V1 40，S2 -> V1 60
    _batch(
        client,
        [
            {"source": "S1", "destination": "V1", "volume": "40"},
            {"source": "S2", "destination": "V1", "volume": "60"},
        ],
    )
    # 第二批：链式 V1 -> V2 25（S1 10 / S2 15）
    _batch(client, [{"source": "V1", "destination": "V2", "volume": "25"}])

    rec = client.get(
        "/audit/records", params={"start_seq": 2, "end_seq": 2}
    ).json()["records"][0]
    by_id = {c["container_id"]: c for c in rec["containers"]}

    assert by_id["V1"]["version_before"] == 1
    assert by_id["V1"]["version_after"] == 2
    assert by_id["V1"]["volume_before"] == "100.000000000"
    assert by_id["V1"]["volume_after"] == "75.000000000"
    assert {(s["source_id"], s["amount"]) for s in by_id["V1"]["sources_after"]} == {
        ("S1", "30.000000000"),
        ("S2", "45.000000000"),
    }
    assert by_id["V2"]["volume_before"] == "0"
    assert by_id["V2"]["volume_after"] == "25.000000000"
    # V2 首批未参与（version 仍为 0），本批第一次被触碰：0 -> 1
    assert rec["result_versions"] == {"V1": 2, "V2": 1}

    # 逐步明细保留最大余额法扣取份额
    step = rec["transfers"][0]
    assert step["taken"] == [
        {"source_id": "S1", "amount": "10.000000000"},
        {"source_id": "S2", "amount": "15.000000000"},
    ]


def test_container_appearing_multiple_times_in_batch_versioned_once(client):
    sample(client, "S1", "10")
    container(client, "V1")
    container(client, "V2")
    _batch(
        client,
        [
            {"source": "S1", "destination": "V1", "volume": "3"},
            {"source": "V1", "destination": "V2", "volume": "2"},
        ],
    )
    rec = client.get("/audit/records").json()["records"][0]
    by_id = {c["container_id"]: c for c in rec["containers"]}
    # V1 同批先做目标后做源：version_before=0、version_after=1，只 +1 一次
    assert by_id["V1"]["version_before"] == 0
    assert by_id["V1"]["version_after"] == 1
    assert by_id["V1"]["volume_before"] == "0"
    assert by_id["V1"]["volume_after"] == "1.000000000"


# --------------------------------------------------------------------------- #
# 查询：按容器、按序号范围、分页、参数校验
# --------------------------------------------------------------------------- #
def test_query_by_container(client):
    _seed_two_samples_and_vials(client)
    _batch(
        client,
        [
            {"source": "S1", "destination": "V1", "volume": "40"},
            {"source": "S2", "destination": "V1", "volume": "60"},
        ],
    )  # seq 1: S1, S2, V1
    _batch(client, [{"source": "V1", "destination": "V2", "volume": "25"}])  # seq 2: V1, V2
    _batch(client, [{"source": "S2", "destination": "V2", "volume": "5"}])  # seq 3: S2, V2

    body = client.get("/audit/records", params={"container_id": "V2"}).json()
    assert [r["seq"] for r in body["records"]] == [2, 3]
    assert body["filters"] == {"container_id": "V2", "start_seq": None, "end_seq": None}

    body = client.get("/audit/records", params={"container_id": "S1"}).json()
    assert [r["seq"] for r in body["records"]] == [1]

    # 容器存在但从未参与任何批次：空结果而非报错
    container(client, "UNUSED")
    body = client.get("/audit/records", params={"container_id": "UNUSED"}).json()
    assert body["records"] == []
    assert body["count"] == 0


def test_query_unknown_container_returns_404(client):
    r = client.get("/audit/records", params={"container_id": "GHOST"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_container"


def test_query_by_seq_range(client):
    _seed_two_samples_and_vials(client)
    for i in range(1, 4):
        _batch(client, [{"source": "S1", "destination": "V1", "volume": str(i)}])

    body = client.get(
        "/audit/records", params={"start_seq": 2, "end_seq": 3}
    ).json()
    assert [r["seq"] for r in body["records"]] == [2, 3]

    body = client.get("/audit/records", params={"start_seq": 3}).json()
    assert [r["seq"] for r in body["records"]] == [3]

    body = client.get("/audit/records", params={"end_seq": 1}).json()
    assert [r["seq"] for r in body["records"]] == [1]

    # 容器 + 范围组合
    body = client.get(
        "/audit/records",
        params={"container_id": "V1", "start_seq": 2, "end_seq": 2},
    ).json()
    assert [r["seq"] for r in body["records"]] == [2]


def test_invalid_seq_range_rejected(client):
    r = client.get("/audit/records", params={"start_seq": 0})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_seq_range"

    r = client.get("/audit/records", params={"start_seq": 5, "end_seq": 2})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_seq_range"

    r = client.get("/audit/records", params={"end_seq": -1})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_seq_range"


def test_limit_validation_and_pagination(client):
    _seed_two_samples_and_vials(client)
    for _ in range(3):
        _batch(client, [{"source": "S1", "destination": "V1", "volume": "1"}])

    body = client.get("/audit/records", params={"limit": 2}).json()
    assert body["limit"] == 2
    assert [r["seq"] for r in body["records"]] == [1, 2]
    body = client.get(
        "/audit/records", params={"start_seq": 3, "limit": 2}
    ).json()
    assert [r["seq"] for r in body["records"]] == [3]

    r = client.get("/audit/records", params={"limit": 0})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_limit"

    r = client.get("/audit/records", params={"limit": audit.MAX_LIMIT + 1})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_limit"


# --------------------------------------------------------------------------- #
# 只追加触发器
# --------------------------------------------------------------------------- #
def test_audit_records_are_append_only_via_triggers(client):
    _seed_two_samples_and_vials(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "10"}])

    conn = connect()
    try:
        with pytest.raises(sqlite3.DatabaseError) as exc:
            conn.execute("UPDATE audit_records SET payload_json = 'x' WHERE seq = 1")
        assert "只追加" in str(exc.value)

        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM audit_records WHERE seq = 1")

        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE audit_record_containers SET container_id = 'x' WHERE audit_seq = 1"
            )

        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM audit_record_containers WHERE audit_seq = 1")
    finally:
        conn.close()

    # 被拒绝的修改不影响数据，链仍然有效
    assert client.get("/audit/verify").json()["valid"] is True


# --------------------------------------------------------------------------- #
# 篡改检测（绕过触发器模拟外部直接改库文件）
# --------------------------------------------------------------------------- #
def _drop_audit_triggers():
    conn = connect()
    try:
        for name in audit._TRIGGER_DEFS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    finally:
        conn.close()


def test_verify_detects_payload_tampering(client):
    _seed_two_samples_and_vials(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "10"}])
    _batch(client, [{"source": "S2", "destination": "V1", "volume": "20"}])

    _drop_audit_triggers()
    conn = connect()
    try:
        conn.execute("UPDATE audit_records SET payload_json = '{}' WHERE seq = 1")
        conn.commit()
    finally:
        conn.close()

    v = client.get("/audit/verify").json()
    assert v["valid"] is False
    assert v["records"] == 2
    assert v["break"]["seq"] == 1
    assert v["break"]["reason"] == "hash_mismatch"
    assert v["checks"]["hash_chain"]["ok"] is False
    assert v["checks"]["payload_integrity"]["ok"] is False
    assert v["checks"]["sequence"]["ok"] is True


def test_verify_detects_middle_record_deletion(client):
    _seed_two_samples_and_vials(client)
    for i in range(1, 4):
        _batch(client, [{"source": "S1", "destination": "V1", "volume": str(i)}])

    _drop_audit_triggers()
    conn = connect()
    try:
        # 外键要求先删索引表行，再删审计记录
        conn.execute("DELETE FROM audit_record_containers WHERE audit_seq = 2")
        conn.execute("DELETE FROM audit_records WHERE seq = 2")
        conn.commit()
    finally:
        conn.close()

    v = client.get("/audit/verify").json()
    assert v["valid"] is False
    assert v["records"] == 2
    # 序号不连续（1,3）与 prev_hash 断链都应暴露
    assert v["checks"]["sequence"]["ok"] is False
    assert v["checks"]["hash_chain"]["ok"] is False
    assert v["break"]["reason"] == "sequence_gap"


def test_verify_detects_recomputed_hash_after_tamper(client):
    """攻击者同时改载荷并重算自身哈希：由于 prev_hash 链在下一条处断裂仍会被发现。"""
    _seed_two_samples_and_vials(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "10"}])
    _batch(client, [{"source": "S2", "destination": "V1", "volume": "20"}])

    _drop_audit_triggers()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT seq, prev_hash, entry_hash, payload_json FROM audit_records WHERE seq = 1"
        ).fetchone()
        tampered = row["payload_json"].replace("10.000000000", "11.000000000")
        new_hash = audit.compute_entry_hash(1, row["prev_hash"], tampered)
        conn.execute(
            "UPDATE audit_records SET payload_json = ?, entry_hash = ? WHERE seq = 1",
            (tampered, new_hash),
        )
        conn.commit()
    finally:
        conn.close()

    v = client.get("/audit/verify").json()
    assert v["valid"] is False
    assert v["break"]["seq"] == 2
    assert v["break"]["reason"] == "prev_hash_link_mismatch"


def test_verify_detects_container_index_tampering(client):
    _seed_two_samples_and_vials(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "10"}])

    _drop_audit_triggers()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO audit_record_containers (audit_seq, container_id, position)"
            " VALUES (1, 'FAKE', 9)"
        )
        conn.commit()
    finally:
        conn.close()

    v = client.get("/audit/verify").json()
    assert v["valid"] is False
    assert v["break"]["reason"] == "container_index_mismatch"
    assert v["checks"]["container_index"]["ok"] is False
    # 按容器查询会把伪造索引查出来——verify 暴露后管理员可据此排查
    r = client.get("/audit/records", params={"container_id": "FAKE"})
    assert r.status_code == 404  # FAKE 不是真实容器，仍按 unknown_container 拒绝


def test_verify_detects_seq_payload_mismatch(client):
    _seed_two_samples_and_vials(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "10"}])

    _drop_audit_triggers()
    conn = connect()
    try:
        import json as _json

        row = conn.execute(
            "SELECT prev_hash, payload_json FROM audit_records WHERE seq = 1"
        ).fetchone()
        payload = _json.loads(row["payload_json"])
        payload["seq"] = 99
        canon = audit.canonical_json(payload)
        new_hash = audit.compute_entry_hash(1, row["prev_hash"], canon)
        conn.execute(
            "UPDATE audit_records SET payload_json = ?, entry_hash = ? WHERE seq = 1",
            (canon, new_hash),
        )
        conn.commit()
    finally:
        conn.close()

    v = client.get("/audit/verify").json()
    assert v["valid"] is False
    assert v["break"]["reason"] == "seq_mismatch"
