"""批量转移的只追加审计链（append-only audit log）。

每个**成功提交**的批次（:func:`app.service._commit_batch`，与容器扣减、version
递增、幂等记录在**同一个 SQLite 事务**内）写入一条审计记录，包含：

* **批次摘要** ``summary``：转移条数、转移总量、受影响容器列表、幂等键（如有）；
* **逐步明细** ``transfers``：每条转移的源 / 目标 / 体积，以及按最大余额法从各
  祖先来源实际扣取的精确份额；
* **受影响容器** ``containers``：本批每个源 / 目标容器（去重）提交前后的
  version、体积与提交后来源构成；同时冗余写入 ``audit_record_containers``
  索引表，支持“按容器”反查；
* **结果版本** ``result_versions``：提交后每个受影响容器的 version。

哈希链
------
记录按 ``seq`` 从 1 开始连续编号。每条记录保存：

* ``prev_hash``：前一条记录的 ``entry_hash``；首条记录为
  :data:`GENESIS_PREV_HASH`（空字符串的 SHA-256，即 sha256("")）；
* ``entry_hash``：``SHA-256(prev_hash + "\\n" + seq + "\\n" + canonical_payload)``，
  其中 ``canonical_payload`` 是载荷的**规范化 JSON**
  （``sort_keys``、无空白、``ensure_ascii=False``，与库中 ``payload_json``
  逐字节一致）。

由此任何对既有记录（含其载荷）的篡改、删除或调换顺序，都会在
``GET /audit/verify`` 中被发现。

只追加
------
``audit_records`` 与 ``audit_record_containers`` 上各有一对 BEFORE UPDATE /
BEFORE DELETE 触发器，任何应用内连接都无法修改或删除既有记录（触发器在
:func:`app.db.init_db` 启动时幂等安装）。审计记录只允许 INSERT。

* 失败的批次（任何 4xx，事务回滚）**不产生**审计记录；
* 幂等重放不执行转移、直接返回首次响应，**不产生**新审计记录；
* ``/transfers/preview`` 只读，**不产生**审计记录。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from decimal import Decimal
from typing import Any

from .service import LineageError

# 首条记录（seq=1）的 prev_hash：空字符串的 SHA-256（小写 hex）
GENESIS_PREV_HASH = hashlib.sha256(b"").hexdigest()

# 列表查询的默认 / 最大页大小
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

# --------------------------------------------------------------------------- #
# 表结构（由 app.db.init_db 的 executescript 幂等执行）
# --------------------------------------------------------------------------- #
AUDIT_SCHEMA_SQL = """
-- 每个成功批次一条审计记录。只追加：应用侧不允许 UPDATE / DELETE
-- （由 audit_records_no_update / audit_records_no_delete 触发器强制）。
CREATE TABLE IF NOT EXISTS audit_records (
    -- 链上序号：从 1 开始、连续递增、不留空洞
    seq          INTEGER PRIMARY KEY,
    -- 前一条记录的 entry_hash；首条为 sha256("")（GENESIS_PREV_HASH）
    prev_hash    TEXT NOT NULL,
    -- 本条哈希：SHA-256(prev_hash || seq || canonical_payload)
    entry_hash   TEXT NOT NULL,
    -- 规范化 JSON 载荷原文（canonical JSON，逐字节参与哈希）
    payload_json TEXT NOT NULL,
    created      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 审计记录 <-> 受影响容器 的反查索引（seq 在容器查询中走 JOIN）。
-- 同样只追加；不设外键到 containers：审计是独立的只追加事实，
-- 不随容器生命周期变化。
CREATE TABLE IF NOT EXISTS audit_record_containers (
    audit_seq    INTEGER NOT NULL REFERENCES audit_records(seq),
    container_id TEXT NOT NULL,
    -- 容器在该记录中的稳定排序位置（按 ID 排序，便于逐行核对）
    position     INTEGER NOT NULL,
    PRIMARY KEY (audit_seq, container_id)
);

CREATE INDEX IF NOT EXISTS idx_audit_containers_container
    ON audit_record_containers(container_id, audit_seq);
"""

# 只追加触发器：BEFORE UPDATE / BEFORE DELETE 一律 RAISE。
# 名称固定，安装前先查 sqlite_master，保证在任意 SQLite 版本上都幂等。
_TRIGGER_DEFS: dict[str, str] = {
    "audit_records_no_update": (
        "audit_records",
        "UPDATE",
        "audit_records 是只追加审计表：禁止 UPDATE（如需纠错请追加新记录）",
    ),
    "audit_records_no_delete": (
        "audit_records",
        "DELETE",
        "audit_records 是只追加审计表：禁止 DELETE（如需纠错请追加新记录）",
    ),
    "audit_record_containers_no_update": (
        "audit_record_containers",
        "UPDATE",
        "audit_record_containers 是只追加审计索引：禁止 UPDATE",
    ),
    "audit_record_containers_no_delete": (
        "audit_record_containers",
        "DELETE",
        "audit_record_containers 是只追加审计索引：禁止 DELETE",
    ),
}


def install_audit_triggers(conn: sqlite3.Connection) -> None:
    """幂等安装四张只追加触发器（已存在则跳过），启动建库时调用。"""
    existing = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    for name, (table, event, message) in _TRIGGER_DEFS.items():
        if name in existing:
            continue
        conn.execute(
            f"CREATE TRIGGER {name} BEFORE {event} ON {table} "
            f"BEGIN SELECT RAISE(FAIL, '{message}'); END"
        )


# --------------------------------------------------------------------------- #
# 规范化序列化与哈希
# --------------------------------------------------------------------------- #
def canonical_json(payload: Any) -> str:
    """载荷的规范化 JSON：键排序、无多余空白、不转义非 ASCII。

    库中存储的 ``payload_json`` 与哈希输入均为本函数输出，因此
    “重新规范化存储的载荷”若不能复现原文，即说明载荷被外部篡改。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_entry_hash(seq: int, prev_hash: str, canonical_payload: str) -> str:
    """计算审计记录哈希：``SHA-256(prev_hash + "\\n" + seq + "\\n" + payload)``。"""
    h = hashlib.sha256()
    h.update(prev_hash.encode("utf-8"))
    h.update(b"\n")
    h.update(str(seq).encode("ascii"))
    h.update(b"\n")
    h.update(canonical_payload.encode("utf-8"))
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# 写入（仅在成功批次的写事务内调用）
# --------------------------------------------------------------------------- #
def _build_payload(
    *,
    seq: int,
    prepared: list[tuple[str, str, Decimal]],
    taken_steps: list[dict[str, Decimal]],
    touched: dict[str, dict[str, Decimal]],
    pre_states: dict[str, dict[str, Decimal]],
    prev_versions: dict[str, int],
    next_versions: dict[str, int],
    idempotency_key: str | None,
) -> dict:
    """构造一条审计记录的载荷（批次摘要 + 逐步明细 + 受影响容器 + 结果版本）。"""
    transfers: list[dict] = []
    total_volume = Decimal(0)
    for index, ((src, dst, volume), taken) in enumerate(
        zip(prepared, taken_steps), start=1
    ):
        total_volume += volume
        transfers.append(
            {
                "index": index,
                "source": src,
                "destination": dst,
                "volume": str(volume),
                "taken": [
                    {"source_id": sid, "amount": str(amount)}
                    for sid, amount in sorted(taken.items())
                ],
            }
        )

    containers: list[dict] = []
    for cid in sorted(touched):
        before = pre_states[cid]
        after = touched[cid]
        containers.append(
            {
                "container_id": cid,
                "version_before": prev_versions[cid],
                "version_after": next_versions[cid],
                "volume_before": str(sum(before.values(), Decimal(0))),
                "volume_after": str(sum(after.values(), Decimal(0))),
                "sources_after": [
                    {"source_id": sid, "amount": str(amount)}
                    for sid, amount in sorted(after.items())
                ],
            }
        )

    return {
        "seq": seq,
        "summary": {
            "transfer_count": len(prepared),
            "total_volume": str(total_volume),
            "containers": sorted(touched),
            "idempotency_key": idempotency_key,
        },
        "transfers": transfers,
        "containers": containers,
        "result_versions": {cid: next_versions[cid] for cid in sorted(next_versions)},
    }


def append_batch_audit(
    conn: sqlite3.Connection,
    *,
    prepared: list[tuple[str, str, Decimal]],
    taken_steps: list[dict[str, Decimal]],
    touched: dict[str, dict[str, Decimal]],
    pre_states: dict[str, dict[str, Decimal]],
    prev_versions: dict[str, int],
    next_versions: dict[str, int],
    idempotency_key: str | None = None,
) -> dict:
    """在当前（已开启的）写事务内追加一条批次审计记录。

    必须由 :func:`app.service._commit_batch` 在 ``_persist_states`` 之后、
    COMMIT 之前调用：与扣减 / version 递增 / 幂等行同事务，任何后续失败都会
    连同审计行一起回滚，保证“失败不留痕”。返回写入记录的字典视图。
    """
    head = conn.execute(
        "SELECT seq, entry_hash FROM audit_records WHERE seq = "
        "(SELECT COALESCE(max(seq), 0) FROM audit_records)"
    ).fetchone()
    # seq 由应用在写事务（BEGIN IMMEDIATE 串行化）内取 max+1 分配，无空洞
    seq = (head["seq"] + 1) if head is not None else 1
    prev_hash = head["entry_hash"] if head is not None else GENESIS_PREV_HASH

    payload = _build_payload(
        seq=seq,
        prepared=prepared,
        taken_steps=taken_steps,
        touched=touched,
        pre_states=pre_states,
        prev_versions=prev_versions,
        next_versions=next_versions,
        idempotency_key=idempotency_key,
    )
    canonical = canonical_json(payload)
    entry_hash = compute_entry_hash(seq, prev_hash, canonical)

    conn.execute(
        "INSERT INTO audit_records (seq, prev_hash, entry_hash, payload_json)"
        " VALUES (?, ?, ?, ?)",
        (seq, prev_hash, entry_hash, canonical),
    )
    conn.executemany(
        "INSERT INTO audit_record_containers (audit_seq, container_id, position)"
        " VALUES (?, ?, ?)",
        [(seq, cid, pos) for pos, cid in enumerate(sorted(touched))],
    )
    return _row_to_record(seq, prev_hash, entry_hash, None, payload)


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def _row_to_record(
    seq: int,
    prev_hash: str,
    entry_hash: str,
    created: str | None,
    payload: dict,
) -> dict:
    return {
        "seq": seq,
        "prev_hash": prev_hash,
        "entry_hash": entry_hash,
        "created": created,
        "summary": payload["summary"],
        "transfers": payload["transfers"],
        "containers": payload["containers"],
        "result_versions": payload["result_versions"],
    }


def get_audit_records(
    conn: sqlite3.Connection,
    *,
    container_id: str | None = None,
    start_seq: int | None = None,
    end_seq: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """按容器（可选）与序号范围（闭区间，可选）查询审计记录，按 seq 升序。

    * ``container_id`` 提供时返回该容器作为源 / 目标出现过的批次；
      容器不存在返回 404 ``unknown_container``（容器从未参与批次时返回空列表，
      不报错）；
    * ``start_seq`` / ``end_seq`` 为闭区间，必须 ≥1 且 start ≤ end，
      否则 422 ``invalid_seq_range``；
    * ``limit`` ∈ [1, 1000]，默认 100，否则 422 ``invalid_limit``。
    """
    if start_seq is not None and start_seq < 1:
        raise LineageError(
            422, "invalid_seq_range", "start_seq 必须是不小于 1 的正整数"
        )
    if end_seq is not None and end_seq < 1:
        raise LineageError(
            422, "invalid_seq_range", "end_seq 必须是不小于 1 的正整数"
        )
    if start_seq is not None and end_seq is not None and end_seq < start_seq:
        raise LineageError(
            422,
            "invalid_seq_range",
            f"序号范围非法：end_seq({end_seq}) 不能小于 start_seq({start_seq})",
        )
    if limit < 1 or limit > MAX_LIMIT:
        raise LineageError(
            422, "invalid_limit", f"limit 必须在 1 到 {MAX_LIMIT} 之间（默认 {DEFAULT_LIMIT}）"
        )
    if container_id is not None:
        exists = conn.execute(
            "SELECT 1 FROM containers WHERE id = ?", (container_id,)
        ).fetchone()
        if exists is None:
            raise LineageError(
                404, "unknown_container", f"容器 '{container_id}' 不存在"
            )

    where: list[str] = []
    params: list[Any] = []
    if container_id is not None:
        where.append("c.container_id = ?")
        params.append(container_id)
    if start_seq is not None:
        where.append("a.seq >= ?")
        params.append(start_seq)
    if end_seq is not None:
        where.append("a.seq <= ?")
        params.append(end_seq)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    join = (
        " JOIN audit_record_containers c ON c.audit_seq = a.seq"
        if container_id is not None
        else ""
    )
    distinct = "DISTINCT " if container_id is not None else ""

    rows = conn.execute(
        f"SELECT {distinct} a.seq, a.prev_hash, a.entry_hash, a.payload_json, a.created"
        f" FROM audit_records a{join}{where_sql} ORDER BY a.seq LIMIT ?",
        (*params, limit),
    ).fetchall()

    records = []
    for r in rows:
        records.append(
            _row_to_record(
                r["seq"],
                r["prev_hash"],
                r["entry_hash"],
                r["created"],
                json.loads(r["payload_json"]),
            )
        )
    return {
        "count": len(records),
        "limit": limit,
        "filters": {
            "container_id": container_id,
            "start_seq": start_seq,
            "end_seq": end_seq,
        },
        "records": records,
    }


# --------------------------------------------------------------------------- #
# 链完整性校验
# --------------------------------------------------------------------------- #
def _check_ok() -> dict:
    return {"ok": True, "detail": None}


def _check_bad(detail: str) -> dict:
    return {"ok": False, "detail": detail}


def verify_audit_chain(conn: sqlite3.Connection) -> dict:
    """从头至尾校验整条审计链，始终返回 200（结果在 ``valid`` 字段）。

    四项检查（各自独立给出 ok / detail）：

    1. ``sequence``：序号必须为从 1 开始的连续整数 1..N（无缺失、无重复）；
    2. ``payload_integrity``：存储的载荷重新规范化后必须与原文逐字节一致，
       且载荷内 ``seq`` 与行序号一致；
    3. ``hash_chain``：每行 ``prev_hash`` 必须等于前一行 ``entry_hash``
       （首行为 :data:`GENESIS_PREV_HASH`），且重算的哈希必须等于
       ``entry_hash``；
    4. ``container_index``：载荷中的受影响容器必须与反查索引表
       ``audit_record_containers`` 的行（按 position）完全一致。

    ``break`` 给出按 sequence → hash_chain → payload_integrity →
    container_index 优先级找到的第一处断裂（``seq`` / ``reason`` / ``detail``），
    无断裂时为 ``null``。
    """
    rows = conn.execute(
        "SELECT seq, prev_hash, entry_hash, payload_json FROM audit_records"
        " ORDER BY seq"
    ).fetchall()
    children: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT audit_seq, container_id FROM audit_record_containers"
        " ORDER BY audit_seq, position"
    ):
        children.setdefault(r["audit_seq"], []).append(r["container_id"])

    checks = {
        "sequence": _check_ok(),
        "payload_integrity": _check_ok(),
        "hash_chain": _check_ok(),
        "container_index": _check_ok(),
    }
    n = len(rows)

    # 1) 序号连续性
    expected_seqs = list(range(1, n + 1))
    actual_seqs = [r["seq"] for r in rows]
    if actual_seqs != expected_seqs:
        counts = Counter(actual_seqs)
        missing = sorted(set(expected_seqs) - counts.keys())
        dup = sorted(seq for seq, count in counts.items() if count > 1)
        detail = f"审计序号不是 1..{n} 的连续序列；缺失={missing or '无'}，重复={dup or '无'}"
        if not actual_seqs or actual_seqs[0] != 1:
            detail = f"审计链必须从 seq=1 开始；实际首条 seq={actual_seqs[0] if actual_seqs else '空'}"
        checks["sequence"] = _check_bad(detail)

    # 2) 载荷完整性（canonical roundtrip + 载荷内 seq）
    parsed: dict[int, dict] = {}
    first_payload_break: tuple[int, str] | None = None
    for r in rows:
        raw = r["payload_json"]
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            checks["payload_integrity"] = _check_bad(
                f"seq={r['seq']} 的载荷不是合法 JSON：{exc}"
            )
            if first_payload_break is None:
                first_payload_break = (r["seq"], "payload_not_json")
            continue
        if canonical_json(payload) != raw:
            checks["payload_integrity"] = _check_bad(
                f"seq={r['seq']} 的载荷与规范化序列化结果不一致（载荷被篡改）"
            )
            if first_payload_break is None:
                first_payload_break = (r["seq"], "payload_canonical_mismatch")
        elif payload.get("seq") != r["seq"]:
            checks["payload_integrity"] = _check_bad(
                f"seq={r['seq']} 载荷内 seq={payload.get('seq')} 与行序号不一致"
            )
            if first_payload_break is None:
                first_payload_break = (r["seq"], "seq_mismatch")
        parsed[r["seq"]] = payload

    # 3) 哈希链：prev_hash 链接 + 重算 entry_hash
    first_hash_break: tuple[int, str] | None = None
    prev_hash = GENESIS_PREV_HASH
    for r in rows:
        raw = r["payload_json"]
        if r["prev_hash"] != prev_hash:
            checks["hash_chain"] = _check_bad(
                f"seq={r['seq']} 的 prev_hash 与前一条记录的 entry_hash 不一致"
                "（链断裂，可能有记录被删除 / 插入 / 替换）"
            )
            if first_hash_break is None:
                first_hash_break = (r["seq"], "prev_hash_link_mismatch")
            break
        recomputed = compute_entry_hash(r["seq"], prev_hash, raw)
        if recomputed != r["entry_hash"]:
            checks["hash_chain"] = _check_bad(
                f"seq={r['seq']} 的 entry_hash 重算不匹配（记录或其载荷被篡改）"
            )
            if first_hash_break is None:
                first_hash_break = (r["seq"], "hash_mismatch")
            break
        prev_hash = r["entry_hash"]

    # 4) 容器反查索引一致
    first_index_break: tuple[int, str] | None = None
    for r in rows:
        payload = parsed.get(r["seq"])
        if payload is None or not isinstance(payload.get("containers"), list):
            # 载荷已损坏到无法核对容器索引：由 payload_integrity 检查负责报告
            continue
        expected_cids = [
            c["container_id"] for c in payload["containers"]
            if isinstance(c, dict) and "container_id" in c
        ]
        actual_cids = children.get(r["seq"], [])
        if actual_cids != expected_cids or len(expected_cids) != len(payload["containers"]):
            checks["container_index"] = _check_bad(
                f"seq={r['seq']} 受影响容器索引不一致："
                f"载荷={expected_cids}，索引表={actual_cids}"
            )
            if first_index_break is None:
                first_index_break = (r["seq"], "container_index_mismatch")
    indexed_seqs = set(children) - {r["seq"] for r in rows}
    if indexed_seqs and checks["container_index"]["ok"]:
        seq = sorted(indexed_seqs)[0]
        checks["container_index"] = _check_bad(
            f"索引表存在没有审计记录的容器行：audit_seq={seq}"
        )
        first_index_break = (seq, "orphan_container_index")

    valid = all(c["ok"] for c in checks.values())

    break_info: dict | None = None
    if not valid:
        # 按既定优先级报告第一处断裂
        if not checks["sequence"]["ok"]:
            first_bad_seq = actual_seqs[0] if actual_seqs else 1
            break_info = {
                "seq": first_bad_seq,
                "reason": "sequence_gap",
                "detail": checks["sequence"]["detail"],
            }
        elif first_hash_break is not None:
            seq, reason = first_hash_break
            break_info = {
                "seq": seq,
                "reason": reason,
                "detail": checks["hash_chain"]["detail"],
            }
        elif first_payload_break is not None:
            seq, reason = first_payload_break
            break_info = {
                "seq": seq,
                "reason": reason,
                "detail": checks["payload_integrity"]["detail"],
            }
        elif first_index_break is not None:
            seq, reason = first_index_break
            break_info = {
                "seq": seq,
                "reason": reason,
                "detail": checks["container_index"]["detail"],
            }

    return {
        "valid": valid,
        "records": n,
        "first_seq": rows[0]["seq"] if rows else None,
        "max_seq": rows[-1]["seq"] if rows else None,
        "break": break_info,
        "checks": checks,
    }


# --------------------------------------------------------------------------- #
# 测试辅助（生产代码不使用）
# --------------------------------------------------------------------------- #
def reset_for_tests(conn: sqlite3.Connection) -> None:
    """清空审计表并恢复只追加触发器（仅测试使用）。

    由于 BEFORE DELETE 触发器禁止删除审计行，测试夹具需要先临时摘除触发器、
    清空记录、再重新安装；生产路径永远不会调用本函数。
    """
    for name in _TRIGGER_DEFS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute("DELETE FROM audit_record_containers")
    conn.execute("DELETE FROM audit_records")
    install_audit_triggers(conn)
