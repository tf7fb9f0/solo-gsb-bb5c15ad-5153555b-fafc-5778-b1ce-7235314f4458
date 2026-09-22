"""POST /transfers/preview 端到端测试：逐步扣取明细、最终状态、不落库。"""
from decimal import Decimal

from helpers import container, sample, volume_of


def _preview(client, transfers):
    return client.post("/transfers/preview", json={"transfers": transfers})


def test_preview_returns_steps_and_final_states_without_persisting(client):
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    container(client, "V1")
    container(client, "V2")

    r = _preview(
        client,
        [
            {"source": "S1", "destination": "V1", "volume": "40"},
            {"source": "S2", "destination": "V1", "volume": "60"},
            {"source": "V1", "destination": "V2", "volume": "25"},
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # 每一步从各祖先来源实际扣取的精确份额（条目序号从 1 开始）
    assert [s["index"] for s in body["steps"]] == [1, 2, 3]
    assert body["steps"][0] == {
        "index": 1,
        "source": "S1",
        "destination": "V1",
        "volume": "40.000000000",
        "taken": [{"source_id": "S1", "amount": "40.000000000"}],
    }
    assert body["steps"][1]["taken"] == [
        {"source_id": "S2", "amount": "60.000000000"}
    ]
    # 链式步骤使用本批前序模拟结果：V1 此时已是 40/60 混合
    assert body["steps"][2]["taken"] == [
        {"source_id": "S1", "amount": "10.000000000"},
        {"source_id": "S2", "amount": "15.000000000"},
    ]

    # 全部受影响容器的最终体积与来源构成
    finals = {c["container_id"]: c for c in body["containers"]}
    assert set(finals) == {"S1", "S2", "V1", "V2"}
    assert finals["S1"]["volume"] == "60.000000000"
    assert finals["S2"]["volume"] == "40.000000000"
    assert finals["V1"]["volume"] == "75.000000000"
    assert {(s["source_id"], s["amount"]) for s in finals["V1"]["sources"]} == {
        ("S1", "30.000000000"),
        ("S2", "45.000000000"),
    }
    assert finals["V2"]["volume"] == "25.000000000"
    assert {(s["source_id"], s["amount"]) for s in finals["V2"]["sources"]} == {
        ("S1", "10.000000000"),
        ("S2", "15.000000000"),
    }
    probs = {s["source_id"]: s["proportion"] for s in finals["V2"]["sources"]}
    assert probs == {"S1": "0.400000", "S2": "0.600000"}

    # 预演不改变数据库：所有容器保持初始状态
    assert volume_of(client, "S1") == "100.000000000"
    assert volume_of(client, "S2") == "100.000000000"
    assert volume_of(client, "V1") == "0"
    assert volume_of(client, "V2") == "0"


def test_preview_matches_subsequent_committed_batch(client):
    """同一批转移：预演的最终状态与真实提交后的数据库状态逐一相同。"""
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    container(client, "V1")
    container(client, "V2")
    transfers = [
        {"source": "S1", "destination": "V1", "volume": "40"},
        {"source": "S2", "destination": "V1", "volume": "60"},
        {"source": "V1", "destination": "V2", "volume": "25"},
    ]

    preview = _preview(client, transfers).json()
    r = client.post("/transfers/batch", json={"transfers": transfers})
    assert r.status_code == 200

    for expected in preview["containers"]:
        actual = client.get(f"/containers/{expected['container_id']}").json()
        assert actual == expected


def test_preview_error_identical_to_batch_and_leaves_state(client):
    sample(client, "S1", "10")
    container(client, "V1")
    container(client, "V2")
    transfers = [
        {"source": "S1", "destination": "V1", "volume": "5"},
        {"source": "S1", "destination": "V2", "volume": "100"},
    ]

    preview_resp = _preview(client, transfers)
    batch_resp = client.post("/transfers/batch", json={"transfers": transfers})

    # 状态码、错误码、条目序号（消息中的“第 N 条”）与真实提交完全一致
    assert preview_resp.status_code == batch_resp.status_code == 422
    assert preview_resp.json() == batch_resp.json()
    assert preview_resp.json()["error"]["code"] == "insufficient_volume"
    assert "第 2 条转移" in preview_resp.json()["error"]["message"]

    # 失败的预演同样不落库
    assert volume_of(client, "S1") == "10.000000000"
    assert volume_of(client, "V1") == "0"
    assert volume_of(client, "V2") == "0"


def test_preview_unknown_container_404(client):
    sample(client, "S1", "10")
    r = _preview(client, [{"source": "S1", "destination": "GHOST", "volume": "1"}])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_container"
    assert "第 1 条转移" in r.json()["error"]["message"]
    assert volume_of(client, "S1") == "10.000000000"


def test_preview_self_transfer_and_invalid_volume_rejected(client):
    sample(client, "S1", "10")
    container(client, "V1")

    r = _preview(client, [{"source": "S1", "destination": "S1", "volume": "1"}])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "self_transfer"

    r = _preview(client, [{"source": "S1", "destination": "V1", "volume": "0"}])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_volume"

    r = _preview(
        client,
        [{"source": "S1", "destination": "V1", "volume": "0.0000000001"}],
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_volume"

    assert volume_of(client, "S1") == "10.000000000"
    assert volume_of(client, "V1") == "0"


def test_preview_uses_same_largest_remainder_apportionment(client):
    # 与真实提交相同的最大余额分摊：1/3 三等分混合中取 1.000000001
    sample(client, "A", "1")
    sample(client, "B", "1")
    sample(client, "C", "1")
    container(client, "M")
    container(client, "N")
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "A", "destination": "M", "volume": "1"},
                {"source": "B", "destination": "M", "volume": "1"},
                {"source": "C", "destination": "M", "volume": "1"},
            ]
        },
    )
    assert r.status_code == 200

    r = _preview(
        client, [{"source": "M", "destination": "N", "volume": "1.000000001"}]
    )
    assert r.status_code == 200, r.text
    taken = r.json()["steps"][0]["taken"]
    amounts = sorted(Decimal(t["amount"]) for t in taken)
    assert amounts == [
        Decimal("0.333333333"),
        Decimal("0.333333334"),
        Decimal("0.333333334"),
    ]
    assert sum(amounts, Decimal(0)) == Decimal("1.000000001")

    # 预演后目标容器仍为空
    assert volume_of(client, "N") == "0"


def test_preview_repeated_calls_are_idempotent(client):
    sample(client, "S1", "10")
    container(client, "V1")
    transfers = [{"source": "S1", "destination": "V1", "volume": "4"}]

    first = _preview(client, transfers)
    second = _preview(client, transfers)
    assert first.status_code == second.status_code == 200
    # 两次预演基于同一数据库状态，结果完全一致
    assert first.json() == second.json()
    assert volume_of(client, "S1") == "10.000000000"
    assert volume_of(client, "V1") == "0"
