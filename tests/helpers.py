"""测试辅助函数。"""


def sample(client, sid, volume):
    r = client.post("/samples", json={"sample_id": sid, "volume": volume})
    assert r.status_code == 201, r.text
    return r


def container(client, cid):
    r = client.post("/containers", json={"container_id": cid})
    assert r.status_code == 201, r.text
    return r


def volume_of(client, cid):
    return client.get(f"/containers/{cid}").json()["volume"]
