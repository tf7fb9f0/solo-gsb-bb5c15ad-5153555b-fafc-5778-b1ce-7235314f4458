"""样品分装谱系的核心逻辑：登记容器/初始样品、批量转移、转移预演、查询状态。

约定
----
* 体积一律使用 :class:`decimal.Decimal`，数据库中以字符串存储，全程无浮点。
* 容器状态表示为 ``{祖先初始样品ID: 绝对体积份额}``，某容器内所有份额之和
  即当前体积。转移时按源容器当时的混合比例传播 *全部* 祖先来源。
* 每个容器持久化一个整数 ``version``（创建时为 0）。每次成功的批量转移后，
  被本批触碰的容器 version 恰好加 1（同批中多次出现也只加 1）。批量接口可通过
  ``expected_versions`` 做乐观并发校验：提供时必须恰好覆盖本批全部源/目标，
  任一容器版本与预期不符则在同一写事务内拒绝（409 stale_container），整批不落库。
* 一批转移严格“依次”发生（后面的转移能看到前面转移的结果）。
* 任一转移失败（未知容器 / 自转移 / 源液不足 / 目标容量超限 / 参数非法），
  整批拒绝，数据库状态完全不变（单个事务回滚）。
* 容器可设置**容量上限**（``containers.capacity``，``NULL`` 表示不限制；见
  :func:`set_container_capacity`，上限必须不小于容器当前体积）。批量提交与
  预演在顺序模拟的每一步检查“目标当前体积 + 本步转入量”是否超过目标的容量
  上限：任一目标超限即整批拒绝（422 capacity_exceeded，消息与 ``details``
  中含条目序号、目标容器与预计超出量）；同批先转出再转入按当时余量判断。
  容量校验与转移在同一事务内：失败时容器状态、版本、幂等记录与审计链均不
  变化；已成功请求的幂等重放直接返回首次结果，不重新校验容量。
* 预演（preview）与真实提交共享同一份校验与分摊实现（见
  :func:`_simulate_transfers`），错误码与条目序号完全一致；
  预演只读不写（不递增 version），无论成功失败都不改变数据库，但返回中给出
  若真实提交后各容器将变成的 version（当前 version + 1）。
* ``POST /transfers/batch`` 可选携带 ``idempotency_key``：携带时首次成功请求的
  请求体指纹（原始请求体字节的 SHA-256）与**完整响应**与容器扣减/版本递增写入
  **同一个写事务**（见 :func:`apply_transfers_idempotent`）。同键同请求体的
  后续请求直接重放首次响应，不再扣减体积、不再递增 version；同键但请求体不同
  返回 409 idempotency_conflict。失败的请求（4xx）不落任何记录，可用同键重试。
  预演接口不写幂等记录；不携带幂等键时行为完全保持兼容。
* 每个成功提交的批次还会在同一事务内追加一条**只追加审计记录**（见
  :mod:`app.audit`）：包含批次摘要、逐步扣取明细、受影响容器提交前后版本/体积
  与提交后来源构成、结果版本，并把前一条记录的哈希纳入当前记录的 entry_hash，
  形成可校验哈希链。审计记录由数据库触发器禁止 UPDATE/DELETE；失败批次、
  幂等重放与预演均不产生审计记录。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext
from typing import NamedTuple

from .config import VOLUME_QUANTUM, settings

# 组成：source_id（祖先初始样品）-> 绝对体积份额
State = dict[str, Decimal]

# 容器 ID -> 乐观并发版本
Versions = dict[str, int]


class LineageError(Exception):
    """业务错误，映射为 HTTP 4xx。

    ``details`` 为可选的结构化附加信息（如容量超限时的条目序号、目标与
    预计超出量），非空时随错误响应一并输出。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


# --------------------------------------------------------------------------- #
# Decimal 辅助
# --------------------------------------------------------------------------- #
def parse_volume(raw: Decimal, field: str) -> Decimal:
    """校验入参体积：有限、非负、且不超过配置精度。"""
    if not raw.is_finite():
        raise LineageError(422, "invalid_volume", f"{field} 必须是有限数")
    if raw < 0:
        raise LineageError(422, "invalid_volume", f"{field} 不能为负")
    if raw != raw.quantize(VOLUME_QUANTUM, rounding=ROUND_HALF_EVEN):
        raise LineageError(
            422,
            "invalid_volume",
            f"{field} 的小数位不能超过 {settings.decimal_scale} 位",
        )
    return raw.quantize(VOLUME_QUANTUM, rounding=ROUND_HALF_EVEN)


def apportion(total: Decimal, shares: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """按份额（混合比例）把待取 ``total`` 切分给各祖先来源。

    采用最大余额法（Hare quota + Largest remainder）：

    1. 每份先向下取整到体积刻度；
    2. 差额（不超过来源个数个刻度）按“被舍掉的余额”从大到小补 1 个刻度。

    因此结果之和 **精确等于** ``total``，每份满足
    ``0 <= 取出额 <= 该来源在源容器中的份额``，且比例与真实混合比例之差
    不超过 1 个体积刻度。源容器为空时调用方应先拦截。
    """
    sources = list(shares.keys())
    pool = sum(shares.values(), Decimal(0))

    # 每份先向下取整到体积刻度（ROUND_FLOOR 保证余数非负，且余数步数 < 来源数）
    raw = {
        s: (total * shares[s] / pool).quantize(VOLUME_QUANTUM, rounding=ROUND_FLOOR)
        for s in sources
    }
    allocated = sum(raw.values(), Decimal(0))
    remainder_steps = int((total - allocated) / VOLUME_QUANTUM)

    # 被舍掉的余额（按它从大到小补发），余额相同按来源 ID 打破平局，保证确定性
    leftovers = {s: total * shares[s] / pool - raw[s] for s in sources}
    order = sorted(sources, key=lambda s: (-leftovers[s], s))
    result = dict(raw)
    for i in range(remainder_steps):
        result[order[i % len(order)]] += VOLUME_QUANTUM
    return result


# --------------------------------------------------------------------------- #
# 状态读写
# --------------------------------------------------------------------------- #
def _load_state(conn: sqlite3.Connection, cid: str) -> tuple[State, int, Decimal | None] | None:
    """读取容器状态、当前乐观版本与容量上限；容器不存在返回 ``None``。

    容量上限为 ``None`` 表示不限制。
    """
    row = conn.execute(
        "SELECT version, capacity FROM containers WHERE id = ?", (cid,)
    ).fetchone()
    if row is None:
        return None
    state: State = {}
    for r in conn.execute(
        "SELECT source_id, amount FROM components WHERE container_id = ?", (cid,)
    ):
        state[r["source_id"]] = Decimal(r["amount"])
    capacity = Decimal(row["capacity"]) if row["capacity"] is not None else None
    return state, int(row["version"]), capacity


def _persist_states(
    conn: sqlite3.Connection,
    states: Mapping[str, State],
    versions: Mapping[str, int],
) -> None:
    """把本批被触碰过的容器整体写回（version 增 1、components 删后重插，同一事务内）。"""
    for cid, state in states.items():
        conn.execute(
            "UPDATE containers SET version = ? WHERE id = ?",
            (versions[cid], cid),
        )
        conn.execute("DELETE FROM components WHERE container_id = ?", (cid,))
        conn.executemany(
            "INSERT INTO components (container_id, source_id, amount) VALUES (?, ?, ?)",
            [(cid, sid, str(amount)) for sid, amount in sorted(state.items())],
        )


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #
def register_initial_sample(
    conn: sqlite3.Connection, sample_id: str, volume: Decimal
) -> Decimal:
    """登记一个带体积的初始样品：它是自身唯一的祖先来源。

    返回按配置精度规范化后的体积。
    """
    volume = parse_volume(volume, "volume")
    if volume == 0:
        raise LineageError(422, "invalid_volume", "初始样品体积必须大于 0")
    conn.execute("BEGIN")
    try:
        try:
            conn.execute("INSERT INTO containers (id) VALUES (?)", (sample_id,))
        except sqlite3.IntegrityError:
            raise LineageError(
                409, "container_exists", f"容器/样品 '{sample_id}' 已存在"
            )
        conn.execute(
            "INSERT INTO components (container_id, source_id, amount) VALUES (?, ?, ?)",
            (sample_id, sample_id, str(volume)),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return volume


def register_empty_container(conn: sqlite3.Connection, cid: str) -> None:
    """登记一个空容器（之后可作为转移目标）。"""
    conn.execute("BEGIN")
    try:
        try:
            conn.execute("INSERT INTO containers (id) VALUES (?)", (cid,))
        except sqlite3.IntegrityError:
            raise LineageError(
                409, "container_exists", f"容器/样品 '{cid}' 已存在"
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def set_container_capacity(
    conn: sqlite3.Connection, cid: str, raw_capacity: Decimal | None
) -> dict:
    """设置或修改容器的容量上限；``None`` 表示取消限制（不限制容量）。

    上限必须是非负、符合配置精度的有限数，且**不小于容器当前体积**
    （等于当前体积合法，此后任何转入都会被拒绝）。容器不存在返回
    404 ``unknown_container``；上限小于当前体积返回
    422 ``capacity_below_volume``。容量修改不是批量转移：不递增
    ``version``、不产生审计记录。返回修改后的容器状态（含容量、
    剩余空间与利用率）。
    """
    # 纯参数校验在事务外完成（与数据库状态无关）
    capacity = None
    if raw_capacity is not None:
        capacity = parse_volume(raw_capacity, "capacity")

    # IMMEDIATE：读取当前体积与写入容量在同一写事务内原子完成
    conn.execute("BEGIN IMMEDIATE")
    try:
        loaded = _load_state(conn, cid)
        if loaded is None:
            raise LineageError(404, "unknown_container", f"容器 '{cid}' 不存在")
        state, version, _old_capacity = loaded
        current = sum(state.values(), Decimal(0))
        if capacity is not None and capacity < current:
            raise LineageError(
                422,
                "capacity_below_volume",
                (
                    f"容量上限 {capacity} 小于容器 '{cid}' 当前体积 {current}，"
                    "拒绝设置"
                ),
                details={
                    "container_id": cid,
                    "capacity": format(capacity, "f"),
                    "current_volume": format(current, "f"),
                },
            )
        conn.execute(
            "UPDATE containers SET capacity = ? WHERE id = ?",
            (str(capacity) if capacity is not None else None, cid),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _format_container_status(cid, state, version, capacity)


# --------------------------------------------------------------------------- #
# 批量转移：共享的校验与模拟核心
# --------------------------------------------------------------------------- #
def _prepare_transfers(
    transfers: Iterable[tuple[str, str, Decimal]]
) -> list[tuple[str, str, Decimal]]:
    """预先校验、量化每一条转移的体积与容器参数。

    任何失败都发生在事务开启与状态读取之前，数据库必然不变。
    """
    prepared: list[tuple[str, str, Decimal]] = []
    for index, (src, dst, raw_volume) in enumerate(transfers):
        volume = parse_volume(raw_volume, f"第 {index + 1} 条转移的 volume")
        if not src:
            raise LineageError(422, "invalid_container", f"第 {index + 1} 条转移缺少源容器")
        if not dst:
            raise LineageError(422, "invalid_container", f"第 {index + 1} 条转移缺少目标容器")
        if src == dst:
            raise LineageError(
                422,
                "self_transfer",
                f"第 {index + 1} 条转移为自转移：'{src}' -> '{src}'，整批拒绝",
            )
        prepared.append((src, dst, volume))
    return prepared


def _validate_expected_coverage(
    prepared: Iterable[tuple[str, str, Decimal]],
    expected_versions: Mapping[str, int] | None,
) -> None:
    """校验 expected_versions（若提供）恰好覆盖本批全部源/目标容器。

    覆盖规则：键集合与“本批所有 source∪destination（去重）”完全一致，
    多一个或少一个都拒绝。该检查发生在事务开启前，属纯请求合法性错误。
    """
    if expected_versions is None:
        return
    required: set[str] = set()
    for src, dst, _volume in prepared:
        required.add(src)
        required.add(dst)
    provided = set(expected_versions)
    if provided != required:
        missing = sorted(required - provided)
        extra = sorted(provided - required)
        details: list[str] = []
        if missing:
            details.append(f"缺少: {missing}")
        if extra:
            details.append(f"多余: {extra}")
        raise LineageError(
            422,
            "invalid_expected_versions",
            "expected_versions 必须恰好覆盖本批全部源和目标容器（"
            + "；".join(details)
            + "），整批拒绝",
        )


def _simulate_transfers(
    conn: sqlite3.Connection,
    prepared: list[tuple[str, str, Decimal]],
    expected_versions: Mapping[str, int] | None = None,
) -> tuple[
    dict[str, State],
    Versions,
    list[dict[str, Decimal]],
    dict[str, State],
    dict[str, Decimal | None],
]:
    """按数组顺序在内存中依次模拟一批转移（链式转移看到本批前序结果）。

    只从数据库读取、在内存状态上演算，**不写库**；返回
    ``(被触碰容器的最终状态, 被触碰容器的当前 version, 每一步从各祖先来源
    实际扣取的份额, 被触碰容器在本批之前的初始状态快照, 被触碰容器的容量上限)``。
    第四个返回值是各容器**触碰前**的深拷贝快照，供审计记录记载
    version_before / volume_before。提交与预演共用本函数，校验规则、错误码
    与条目序号因此完全一致。

    每一步在源液不足检查之后、分摊之前做**容量校验**：目标容器当时体积 +
    本步转入量超过其容量上限（``NULL`` 表示不限制）即抛
    422 ``capacity_exceeded``，``details`` 含条目序号、目标与预计超出量；
    同批先转出再转入按模拟到本步时的当时余量判断。

    若提供 ``expected_versions``，则在事务内对每个被触碰容器读取到的当前版本做
    乐观并发校验：任一不符即抛 409 stale_container（调用方负责回滚，整批不落库）。
    """
    touched: dict[str, State] = {}
    pre_states: dict[str, State] = {}
    versions: Versions = {}
    capacities: dict[str, Decimal | None] = {}
    taken_steps: list[dict[str, Decimal]] = []

    for index, (src, dst, volume) in enumerate(prepared, start=1):
        # 未知容器（初始样品与空容器都在 containers 表中）
        for cid in (src, dst):
            if cid not in touched:
                loaded = _load_state(conn, cid)
                if loaded is None:
                    raise LineageError(
                        404,
                        "unknown_container",
                        f"第 {index} 条转移中的容器 '{cid}' 不存在，整批拒绝",
                    )
                state, current_version, capacity = loaded
                # 乐观并发版本校验在同一写事务内完成（预演时为只读事务，同样不写库）
                if expected_versions is not None:
                    expected = expected_versions[cid]
                    if current_version != expected:
                        raise LineageError(
                            409,
                            "stale_container",
                            (
                                f"容器 '{cid}' 版本已过期：expected_version={expected}，"
                                f"当前 version={current_version}，整批拒绝且不落库"
                            ),
                        )
                # 保留触碰前快照供审计记载 before 值。Decimal 不可变，模拟中对
                # touched 的原地操作只会整体替换/删除键值，不会原地修改 Decimal，
                # 故浅拷贝 dict 即可完整隔离快照与模拟状态。
                pre_states[cid] = dict(state)
                touched[cid] = state
                versions[cid] = current_version
                capacities[cid] = capacity

        src_state = touched[src]
        dst_state = touched[dst]
        src_total = sum(src_state.values(), Decimal(0))

        if volume == 0:
            raise LineageError(
                422,
                "invalid_volume",
                f"第 {index} 条转移体积必须大于 0，整批拒绝",
            )
        if volume > src_total:
            raise LineageError(
                422,
                "insufficient_volume",
                (
                    f"第 {index} 条转移源液不足：'{src}' 当前 {src_total}，"
                    f"请求 {volume}，整批拒绝"
                ),
            )

        # 容量约束：目标当时体积 + 本步转入量不得超过其容量上限。
        # dst_state 反映本批前序步骤的结果，故同批“先转出再转入”按当时余量判断。
        capacity = capacities[dst]
        if capacity is not None:
            dst_total = sum(dst_state.values(), Decimal(0))
            projected = dst_total + volume
            if projected > capacity:
                excess = projected - capacity
                raise LineageError(
                    422,
                    "capacity_exceeded",
                    (
                        f"第 {index} 条转移目标容量超限：'{dst}' 当前 {dst_total}，"
                        f"转入 {volume} 后预计 {projected}，超过容量上限 {capacity}，"
                        f"预计超出 {excess}，整批拒绝"
                    ),
                    # details 为机器可读字段：定点字符串，与响应中体积格式一致
                    details={
                        "index": index,
                        "destination": dst,
                        "current_volume": format(dst_total, "f"),
                        "incoming": format(volume, "f"),
                        "projected_volume": format(projected, "f"),
                        "capacity": format(capacity, "f"),
                        "excess": format(excess, "f"),
                    },
                )

        # 按源容器“当时”的混合比例取出全部祖先来源
        with localcontext() as ctx:
            ctx.prec = settings.decimal_precision
            taken = apportion(volume, src_state)

        for sid, amount in taken.items():
            src_state[sid] -= amount
            dst_state[sid] = dst_state.get(sid, Decimal(0)) + amount
            if src_state[sid] == 0:
                del src_state[sid]
        taken_steps.append(taken)

    return touched, versions, taken_steps, pre_states, capacities


def build_batch_response(applied: int, versions: Versions) -> dict:
    """构造批量提交的响应体（首次写入与 API 返回共用，保证重放内容一致）。"""
    return {
        "applied": applied,
        "message": f"已依次提交 {applied} 条转移",
        "versions": versions,
    }


def fingerprint_request(raw_body: bytes) -> str:
    """计算原始请求体字节的 SHA-256 指纹（小写 hex）。

    以**未经解析的原始请求体**为准：语义相同但字节不同的两个请求体
    （如 JSON 键顺序、空白、数字写法差异）视为不同请求，同键提交时返回 409。
    """
    return hashlib.sha256(raw_body).hexdigest()


class BatchResult(NamedTuple):
    """一批转移的提交结果。

    ``replayed=True`` 时为幂等重放：``payload`` 是首次请求持久化的完整响应，
    数据库未被再次修改（体积不重复扣减、version 不重复递增）。
    """

    applied: int
    versions: Versions
    replayed: bool
    status_code: int
    payload: dict


def _commit_batch(
    conn: sqlite3.Connection,
    prepared: list[tuple[str, str, Decimal]],
    expected_versions: Mapping[str, int] | None,
    idempotency_key: str | None = None,
    request_sha256: str | None = None,
) -> BatchResult:
    """在单个写事务内完成一批转移的全部读写（幂等逻辑亦在此事务内）。

    调用前必须已完成与数据库状态无关的参数/覆盖校验。
    IMMEDIATE 保证“幂等查重 + 状态校验 + 扣减写入 + 幂等记录写入 +
    审计链追加”原子发生：

    * 携带 ``idempotency_key`` 且键已存在：指纹一致 -> 回滚后重放首次响应
      （不重复扣减、不重复递增 version、**不追加审计记录**）；指纹不一致 ->
      回滚后抛 409 idempotency_conflict；
    * 键不存在：正常模拟、持久化，在**同一事务**插入请求指纹与完整响应，
      并追加一条含 prev_hash 的只追加审计记录（见 :mod:`app.audit`）；
    * 不携带幂等键：其余行为与旧版本完全一致，审计记录同样写入。
    """
    # IMMEDIATE：拿到写锁后再读版本与状态，保证“校验+写入”在同一写事务内原子
    conn.execute("BEGIN IMMEDIATE")
    try:
        if idempotency_key is not None:
            row = conn.execute(
                "SELECT request_sha256, status_code, response_body FROM idempotency_keys"
                " WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                if row["request_sha256"] != request_sha256:
                    raise LineageError(
                        409,
                        "idempotency_conflict",
                        (
                            f"幂等键 '{idempotency_key}' 已用于另一个请求体，"
                            "整批拒绝且不落库"
                        ),
                    )
                # 同键同请求体：回滚只读事务，逐字节重放首次结果
                conn.execute("ROLLBACK")
                payload = json.loads(row["response_body"])
                return BatchResult(
                    applied=payload["applied"],
                    versions=dict(payload["versions"]),
                    replayed=True,
                    status_code=row["status_code"],
                    payload=payload,
                )

        touched, current_versions, taken_steps, pre_states, _capacities = (
            _simulate_transfers(conn, prepared, expected_versions)
        )
        next_versions = {cid: ver + 1 for cid, ver in current_versions.items()}
        _persist_states(conn, touched, next_versions)

        # 审计记录与容器扣减、version 递增、幂等行写入同一事务：
        # 之后任何异常都会连同审计行一起回滚，失败 / 预演绝不留痕。
        # 延迟导入避免 service <-> audit 导入环（audit 依赖本模块的 LineageError）。
        from . import audit

        audit.append_batch_audit(
            conn,
            prepared=prepared,
            taken_steps=taken_steps,
            touched=touched,
            pre_states=pre_states,
            prev_versions=current_versions,
            next_versions=next_versions,
            idempotency_key=idempotency_key,
        )

        payload = build_batch_response(len(prepared), next_versions)
        if idempotency_key is not None:
            # 与转移写入同一事务持久化指纹与完整响应；重复键由主键兜底为整批失败
            conn.execute(
                "INSERT INTO idempotency_keys"
                " (idempotency_key, request_sha256, status_code, response_body)"
                " VALUES (?, ?, ?, ?)",
                (
                    idempotency_key,
                    request_sha256,
                    200,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return BatchResult(
        applied=len(prepared),
        versions=next_versions,
        replayed=False,
        status_code=200,
        payload=payload,
    )


def apply_transfers(
    conn: sqlite3.Connection,
    transfers: Iterable[tuple[str, str, Decimal]],
    expected_versions: Mapping[str, int] | None = None,
) -> tuple[int, Versions]:
    """依次执行一批转移，全部成功才提交，任一失败整批回滚。

    提供 ``expected_versions`` 时在同一写事务内做乐观并发校验，任一容器版本
    不符即抛 409 stale_container 并回滚。成功后每个被触碰容器（无论在本批中
    出现多少次）version 恰好加 1。

    返回 ``(执行的转移条数, 各被触碰容器提交后的 version)``。
    不带幂等键的兼容入口；幂等提交见 :func:`apply_transfers_idempotent`。
    """
    result = apply_transfers_idempotent(
        conn, transfers, expected_versions, idempotency_key=None
    )
    return result.applied, result.versions


def apply_transfers_idempotent(
    conn: sqlite3.Connection,
    transfers: Iterable[tuple[str, str, Decimal]],
    expected_versions: Mapping[str, int] | None = None,
    *,
    idempotency_key: str | None,
    request_sha256: str | None = None,
) -> BatchResult:
    """带幂等键的批量提交，返回 :class:`BatchResult`（含是否重放与完整响应）。

    ``idempotency_key`` 为 ``None`` 时与 :func:`apply_transfers` 完全等价
    （不查、不写 ``idempotency_keys``）。提供幂等键时必须同时提供
    ``request_sha256``（由 :func:`fingerprint_request` 对原始请求体计算）：

    * 同键同指纹 -> 重放首次结果，``replayed=True``，不扣减、不递增 version；
    * 同键异指纹 -> 409 idempotency_conflict，整批回滚；
    * 新键 -> 正常提交，指纹与完整响应与转移写入同一事务。
    """
    prepared = _prepare_transfers(transfers)
    _validate_expected_coverage(prepared, expected_versions)
    return _commit_batch(
        conn, prepared, expected_versions, idempotency_key, request_sha256
    )


def preview_transfers(
    conn: sqlite3.Connection,
    transfers: Iterable[tuple[str, str, Decimal]],
    expected_versions: Mapping[str, int] | None = None,
) -> dict:
    """在数据库当前状态上预演一批转移，绝不写入数据库。

    与 :func:`apply_transfers` 共享全部校验、版本校验与最大余额分摊逻辑
    （错误码、条目序号一致）；模拟在事务内进行，结束即回滚，成功或失败
    数据库都完全不变，version 也不递增。返回的容器状态中给出若真实提交后
    将变成的 version（当前 version + 1，每个被触碰容器只加 1 次）。

    返回每一步从各祖先来源实际扣取的精确份额，以及全部受影响
    （被本批触碰的）容器在预演结束时的最终体积、来源构成与预测版本。
    """
    prepared = _prepare_transfers(transfers)
    _validate_expected_coverage(prepared, expected_versions)

    conn.execute("BEGIN")
    try:
        touched, current_versions, taken_steps, _pre_states, capacities = (
            _simulate_transfers(conn, prepared, expected_versions)
        )
        predicted_versions = {cid: ver + 1 for cid, ver in current_versions.items()}
    finally:
        # 预演只读不落库：无论成功失败都回滚，数据库状态必然不变（也不写审计）
        conn.execute("ROLLBACK")

    steps: list[dict] = []
    for index, ((src, dst, volume), taken) in enumerate(
        zip(prepared, taken_steps), start=1
    ):
        steps.append(
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

    return {
        "steps": steps,
        "containers": [
            _format_container_status(
                cid, touched[cid], predicted_versions[cid], capacities[cid]
            )
            for cid in sorted(touched)
        ],
    }


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def _format_container_status(
    cid: str, state: State, version: int, capacity: Decimal | None
) -> dict:
    """把内存中的容器状态格式化为查询/预演共用的输出结构。

    除体积与来源占比外，一并给出容量上限（``None`` 表示不限制）、
    剩余空间（容量 - 当前体积；不限容量时为 ``None``）与利用率
    （体积 / 容量；不限容量或容量为 0 时为 ``None``）。
    """
    total = sum(state.values(), Decimal(0))
    q = Decimal(1).scaleb(-settings.proportion_scale)
    sources: list[dict] = []

    with localcontext() as ctx:
        ctx.prec = settings.decimal_precision
        for sid, amount in sorted(state.items()):
            proportion = (
                (amount / total).quantize(q, rounding=ROUND_HALF_UP)
                if total > 0
                else Decimal(0).quantize(q)
            )
            sources.append(
                {
                    "source_id": sid,
                    "amount": str(amount),
                    "proportion": str(proportion),
                }
            )
        # 利用率与 proportion 同精度、同舍入；容量为 0 时 0/0 无定义
        utilization = (
            (total / capacity).quantize(q, rounding=ROUND_HALF_UP)
            if capacity is not None and capacity > 0
            else None
        )

    remaining = (capacity - total) if capacity is not None else None
    return {
        "container_id": cid,
        "version": version,
        "volume": str(total),
        "capacity": str(capacity) if capacity is not None else None,
        "remaining": str(remaining) if remaining is not None else None,
        "utilization": str(utilization) if utilization is not None else None,
        "sources": sources,
        "proportion_scale": settings.proportion_scale,
    }


def get_container_status(conn: sqlite3.Connection, cid: str) -> dict:
    """返回容器当前乐观版本、体积、容量信息与各祖先来源占比。"""
    loaded = _load_state(conn, cid)
    if loaded is None:
        raise LineageError(404, "unknown_container", f"容器 '{cid}' 不存在")
    state, version, capacity = loaded
    return _format_container_status(cid, state, version, capacity)
