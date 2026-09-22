"""Pydantic 模型。

Decimal 类型同时接受数字与字符串（推荐字符串，避免 JSON 数字经 float 解析），
输出统一为字符串以保留精度。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, PlainSerializer, field_validator

# 输入：数字或字符串均可；输出：字符串
DecimalStr = Annotated[
    Decimal,
    PlainSerializer(lambda d: format(d, "f"), return_type=str, when_used="json"),
]


def _check_id(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("容器 ID 不能为空")
    return v


class SampleIn(BaseModel):
    sample_id: str = Field(..., min_length=1, description="初始样品（容器）ID")
    volume: DecimalStr = Field(..., description="初始体积，非负且小数位受限")

    @field_validator("sample_id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v)


class ContainerIn(BaseModel):
    container_id: str = Field(..., min_length=1, description="空容器 ID")

    @field_validator("container_id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v)


class CapacityIn(BaseModel):
    """设置 / 修改容器容量上限的请求体。"""

    capacity: DecimalStr | None = Field(
        ...,
        description=(
            "容量上限：非负、小数位受 DECIMAL_SCALE 限制，且不得小于容器当前体积；"
            "null 表示不限制容量。字段必须显式提供（数值或 null）。"
        ),
    )


class TransferIn(BaseModel):
    source: str = Field(..., min_length=1, description="源容器 ID")
    destination: str = Field(..., min_length=1, description="目标容器 ID")
    volume: DecimalStr = Field(..., description="转移体积（> 0，不超过源容器当前体积）")

    @field_validator("source", "destination")
    @classmethod
    def _ids(cls, v: str) -> str:
        return _check_id(v)


class BatchTransferIn(BaseModel):
    transfers: list[TransferIn] = Field(
        ..., min_length=1, description="按数组顺序依次执行的转移"
    )
    expected_versions: dict[str, int] | None = Field(
        default=None,
        description=(
            "可选的乐观并发保护：容器 ID -> 客户端预期的 version。"
            "提供时必须恰好覆盖本批全部源与目标容器（不多不少），"
            "覆盖不符返回 422 invalid_expected_versions；"
            "任一容器当前版本与预期不符返回 409 stale_container，整批不落库。"
            "不提供时保持无版本校验的兼容行为。"
        ),
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=255,
        description=(
            "仅对 POST /transfers/batch 生效的可选幂等键。"
            "携带时：成功请求的请求体指纹（原始请求体的 SHA-256）与完整响应与"
            "转移写入同一事务持久化；同键同请求体重放首次结果（不重复扣减体积、"
            "不重复递增 version），同键但请求体不同返回 409 idempotency_conflict。"
            "预演接口不写幂等记录（携带也被忽略）；不携带时行为与旧版本完全一致。"
        ),
    )

    @field_validator("idempotency_key")
    @classmethod
    def _non_blank_idempotency_key(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("idempotency_key 不能为空字符串")
        return v

    @field_validator("expected_versions", mode="before")
    @classmethod
    def _reject_bool_versions(cls, v):
        # 在 lax 强转（bool -> int、"0" -> 0）发生前拦截布尔值
        if isinstance(v, dict) and any(isinstance(ver, bool) for ver in v.values()):
            raise ValueError("expected_versions 中的版本必须是非负整数，不能为布尔值")
        return v

    @field_validator("expected_versions")
    @classmethod
    def _non_negative_versions(cls, v: dict[str, int] | None) -> dict[str, int] | None:
        if v is not None:
            bad = [cid for cid, ver in v.items() if ver < 0]
            if bad:
                raise ValueError(f"expected_versions 中的版本不能为负：{bad}")
        return v


class SourceOut(BaseModel):
    source_id: str
    amount: DecimalStr = Field(..., description="该祖先来源在容器中的绝对体积")
    proportion: DecimalStr = Field(..., description="占当前体积的比例")


class ContainerStatusOut(BaseModel):
    container_id: str
    version: int = Field(
        ..., description="容器乐观并发版本：创建时为 0，每次被成功批次触碰后加 1"
    )
    volume: DecimalStr
    capacity: DecimalStr | None = Field(
        ..., description="容量上限；null 表示不限制"
    )
    remaining: DecimalStr | None = Field(
        ..., description="剩余可转入空间（容量 - 当前体积）；不限容量时为 null"
    )
    utilization: DecimalStr | None = Field(
        ...,
        description=(
            "利用率（当前体积 / 容量），按 PROPORTION_SCALE 四舍五入；"
            "不限容量或容量为 0 时为 null"
        ),
    )
    sources: list[SourceOut]
    proportion_scale: int


class SampleOut(BaseModel):
    sample_id: str
    volume: DecimalStr


class ContainerOut(BaseModel):
    container_id: str


class BatchTransferOut(BaseModel):
    applied: int
    message: str
    versions: dict[str, int] = Field(
        ...,
        description=(
            "本批每个被触碰容器提交后的 version（同批多次出现也只加 1）。"
            "未提供 expected_versions 时同样返回。"
        ),
    )


class TakenOut(BaseModel):
    source_id: str = Field(..., description="祖先初始样品 ID")
    amount: DecimalStr = Field(..., description="本步从该祖先来源实际扣取的体积")


class TransferStepOut(BaseModel):
    index: int = Field(..., description="转移在请求数组中的序号（从 1 开始）")
    source: str = Field(..., description="源容器 ID")
    destination: str = Field(..., description="目标容器 ID")
    volume: DecimalStr = Field(..., description="本步转移体积（规范化后）")
    taken: list[TakenOut] = Field(
        ..., description="本步从各祖先来源实际扣取的精确份额（之和等于 volume）"
    )


class BatchPreviewOut(BaseModel):
    steps: list[TransferStepOut] = Field(
        ..., description="按数组顺序逐步模拟的扣取明细"
    )
    containers: list[ContainerStatusOut] = Field(
        ..., description="全部受影响容器在预演结束时的最终体积与来源构成"
    )


# --------------------------------------------------------------------------- #
# 审计链（app/audit.py）
# --------------------------------------------------------------------------- #
class AuditSummaryOut(BaseModel):
    transfer_count: int = Field(..., description="本批转移条数")
    total_volume: DecimalStr = Field(..., description="本批转移体积之和（提交后汇总）")
    containers: list[str] = Field(..., description="受影响容器 ID（去重，按 ID 排序）")
    idempotency_key: str | None = Field(..., description="本批携带的幂等键，未携带为 null")


class AuditContainerOut(BaseModel):
    container_id: str
    version_before: int = Field(..., description="该容器在本批之前的 version")
    version_after: int = Field(..., description="提交后的 version（恰好 +1）")
    volume_before: DecimalStr = Field(..., description="该容器在本批之前的体积")
    volume_after: DecimalStr = Field(..., description="提交后的体积")
    sources_after: list[TakenOut] = Field(
        ..., description="提交后该容器各祖先来源的绝对体积份额"
    )


class AuditRecordOut(BaseModel):
    seq: int = Field(..., description="链上序号，从 1 开始连续递增")
    prev_hash: str = Field(..., description="前一条记录的 entry_hash；首条为 sha256(\"\")")
    entry_hash: str = Field(
        ..., description="SHA-256(prev_hash + seq + 规范化载荷)，见 /audit/verify"
    )
    created: str | None = Field(..., description="记录创建时间（UTC，ISO-8601）")
    summary: AuditSummaryOut
    transfers: list[TransferStepOut]
    containers: list[AuditContainerOut]
    result_versions: dict[str, int] = Field(
        ..., description="提交后各受影响容器的 version（与批量响应 versions 一致）"
    )


class AuditFiltersOut(BaseModel):
    container_id: str | None
    start_seq: int | None
    end_seq: int | None


class AuditListOut(BaseModel):
    count: int
    limit: int
    filters: AuditFiltersOut
    records: list[AuditRecordOut]


class AuditCheckOut(BaseModel):
    ok: bool
    detail: str | None


class AuditBreakInfo(BaseModel):
    seq: int = Field(..., description="断裂所在（或起首）的记录序号")
    reason: str = Field(..., description="断裂原因代码")
    detail: str = Field(..., description="断裂的人类可读说明")


class AuditVerifyOut(BaseModel):
    valid: bool = Field(..., description="整条审计链是否完整可校验")
    records: int = Field(..., description="审计记录总数")
    first_seq: int | None
    max_seq: int | None
    # break 是 Python 关键字：字段名用 break_，JSON 中仍输出为 "break"
    break_: AuditBreakInfo | None = Field(default=None, alias="break")
    checks: dict[str, AuditCheckOut] = Field(
        ...,
        description=(
            "四项检查结果：sequence（序号连续）、hash_chain（prev_hash 链接与"
            " entry_hash 重算）、payload_integrity（规范化载荷一致）、"
            "container_index（受影响容器反查索引一致）"
        ),
    )

    model_config = {"populate_by_name": True}
