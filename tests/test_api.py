"""API 端到端测试。"""
from decimal import Decimal

from helpers import container, sample, volume_of

SCALE = 9  # 与 conftest 中 DECIMAL_SCALE 一致


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_register_sample_and_query(client):
    r = client.post("/samples", json={"sample_id": "S1", "volume": "100.5"})
    assert r.status_code == 201
    # 体积按配置精度规范化
    assert r.json() == {"sample_id": "S1", "volume": "100.500000000"}

    r = client.get("/containers/S1")
    assert r.status_code == 200
    body = r.json()
    assert body["volume"] == "100.500000000"
    assert body["sources"] == [
        {"source_id": "S1", "amount": "100.500000000", "proportion": "1.000000"}
    ]


def test_register_empty_container_shows_zero_volume(client):
    container(client, "V1")
    body = client.get("/containers/V1").json()
    assert body["volume"] == "0"
    assert body["sources"] == []


def test_duplicate_registration_conflicts(client):
    sample(client, "S1", "10")
    r = client.post("/samples", json={"sample_id": "S1", "volume": "5"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "container_exists"

    container(client, "V1")
    r = client.post("/containers", json={"container_id": "V1"})
    assert r.status_code == 409


def test_query_unknown_container_404(client):
    r = client.get("/containers/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_container"


def test_lineage_propagates_all_ancestors(client):
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    container(client, "V1")
    container(client, "V2")

    # S1 -> V1 40，S2 -> V1 60，V1 混合比为 40/60
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "40"},
                {"source": "S2", "destination": "V1", "volume": "60"},
            ]
        },
    )
    assert r.status_code == 200
    assert r.json()["applied"] == 2

    # 从 V1 取 25：S1 得 25*0.4=10，S2 得 25*0.6=15
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "V1", "destination": "V2", "volume": "25"}]},
    )
    assert r.status_code == 200

    v2 = client.get("/containers/V2").json()
    assert v2["volume"] == "25.000000000"
    assert {(s["source_id"], s["amount"]) for s in v2["sources"]} == {
        ("S1", "10.000000000"),
        ("S2", "15.000000000"),
    }
    probs = {s["source_id"]: s["proportion"] for s in v2["sources"]}
    assert probs == {"S1": "0.400000", "S2": "0.600000"}

    v1 = client.get("/containers/V1").json()
    assert v1["volume"] == "75.000000000"
    assert {(s["source_id"], s["amount"]) for s in v1["sources"]} == {
        ("S1", "30.000000000"),
        ("S2", "45.000000000"),
    }

    # 源容器体积同步扣减
    assert volume_of(client, "S1") == "60.000000000"
    assert volume_of(client, "S2") == "40.000000000"


def test_batch_insufficient_volume_rejects_whole_batch(client):
    sample(client, "S1", "10")
    container(client, "V1")
    container(client, "V2")

    # 第一条合法、第二条源液不足：整批回滚，第一条也不留痕
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "5"},
                {"source": "S1", "destination": "V2", "volume": "100"},
            ]
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "insufficient_volume"

    assert volume_of(client, "S1") == "10.000000000"
    assert volume_of(client, "V1") == "0"
    assert volume_of(client, "V2") == "0"


def test_batch_unknown_container_rejects_whole_batch(client):
    sample(client, "S1", "10")
    container(client, "V1")
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "1"},
                {"source": "S1", "destination": "GHOST", "volume": "1"},
            ]
        },
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_container"
    assert volume_of(client, "S1") == "10.000000000"
    assert volume_of(client, "V1") == "0"


def test_batch_self_transfer_rejected(client):
    sample(client, "S1", "10")
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "S1", "volume": "1"}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "self_transfer"
    assert volume_of(client, "S1") == "10.000000000"


def test_batch_transfers_are_sequential(client):
    sample(client, "S1", "10")
    container(client, "V1")
    container(client, "V2")

    # 第二条依赖第一条的结果：S1 -> V1 3，再 V1 -> V2 2
    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "3"},
                {"source": "V1", "destination": "V2", "volume": "2"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert volume_of(client, "V2") == "2.000000000"
    assert volume_of(client, "V1") == "1.000000000"
    assert volume_of(client, "S1") == "7.000000000"


def test_volume_conservation_in_chained_mixes(client):
    sample(client, "A", "3.333333333")
    sample(client, "B", "6.666666666")
    container(client, "M")
    container(client, "N")

    r = client.post(
        "/transfers/batch",
        json={
            "transfers": [
                {"source": "A", "destination": "M", "volume": "3.333333333"},
                {"source": "B", "destination": "M", "volume": "6.666666666"},
                {"source": "M", "destination": "N", "volume": "5.555555555"},
            ]
        },
    )
    assert r.status_code == 200, r.text

    total = sum(
        (Decimal(volume_of(client, cid)) for cid in ("A", "B", "M", "N")),
        Decimal(0),
    )
    # 总体积守恒（初始总量）
    assert total == Decimal("9.999999999")


def test_input_more_decimals_than_scale_rejected(client):
    r = client.post("/samples", json={"sample_id": "X", "volume": "1.0000000001"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_volume"

    sample(client, "S1", "1")
    container(client, "V1")
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "0.0000000001"}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_volume"


def test_transfer_into_existing_mix_adds_proportions(client):
    sample(client, "A", "10")
    sample(client, "B", "10")
    container(client, "M")
    client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "A", "destination": "M", "volume": "10"}]},
    )
    client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "B", "destination": "M", "volume": "10"}]},
    )
    body = client.get("/containers/M").json()
    assert body["volume"] == "20.000000000"
    probs = {s["source_id"]: s["proportion"] for s in body["sources"]}
    assert probs == {"A": "0.500000", "B": "0.500000"}


def test_zero_volume_transfer_rejected(client):
    sample(client, "S1", "10")
    container(client, "V1")
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "0"}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_volume"
    assert volume_of(client, "S1") == "10.000000000"
