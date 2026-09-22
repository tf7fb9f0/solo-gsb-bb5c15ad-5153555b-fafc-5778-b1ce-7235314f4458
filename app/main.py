"""FastAPI 应用：实验室样品分装谱系 API。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import audit, service
from .db import get_db, init_db
from .schemas import (
    AuditListOut,
    AuditVerifyOut,
    BatchPreviewOut,
    BatchTransferIn,
    BatchTransferOut,
    CapacityIn,
    ContainerIn,
    ContainerOut,
    ContainerStatusOut,
    SampleIn,
    SampleOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 容器启动时建库（首次运行则创建文件与表）
    init_db()
    yield


app = FastAPI(
    title="实验室样品分装谱系 API",
    description=(
        "登记带体积的初始样品后，批量提交依次发生的容器间转移；"
        "每次按源容器当时的混合比例传播全部祖先来源（Decimal 精确计算）。"
        "容器可设置容量上限（空值不限制，且不得小于当前体积）；批量提交与预演"
        "在顺序模拟的每一步检查目标当前体积加转入量，任一目标超上限即整批拒绝"
        "（返回条目序号、目标与预计超出量），失败时状态、版本、幂等记录与审计链"
        "均不变化。"
        "支持 /transfers/preview 预演：与真实提交同一套校验与分摊，但不落库。"
        "/transfers/batch 支持可选 idempotency_key：成功请求的指纹与完整响应"
        "随转移同一事务持久化，同键同请求体重放、同键异请求体返回 409。"
        "每个成功批次在同一事务追加一条只追加审计记录（含批次摘要、受影响容器、"
        "结果版本），以前一条记录的哈希形成可校验哈希链；"
        "失败 / 重放 / 预演均不产生审计记录。"
    ),
    version="1.3.0",
    lifespan=lifespan,
)


@app.exception_handler(service.LineageError)
async def lineage_error_handler(request: Request, exc: service.LineageError) -> JSONResponse:
    error: dict = {"code": exc.code, "message": exc.message}
    # 结构化附加信息（如容量超限的条目序号 / 目标 / 预计超出量），无则不输出
    if exc.details is not None:
        error["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content={"error": error})


async def raw_request_body(request: Request) -> bytes:
    """读取未经解析的原始请求体字节。

    异步依赖在事件循环线程执行，不触碰线程池中的 sqlite 连接；
    Starlette 会缓存请求体，Pydantic 对 body 的解析复用同一份字节，
    因此这里的指纹恰好对应实际校验的请求。
    """
    return await request.body()


@app.get("/health", tags=["meta"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/samples",
    response_model=SampleOut,
    status_code=201,
    tags=["registry"],
    summary="登记带体积的初始样品",
)
def create_sample(body: SampleIn, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    volume = service.register_initial_sample(conn, body.sample_id, body.volume)
    return {"sample_id": body.sample_id, "volume": volume}


@app.post(
    "/containers",
    response_model=ContainerOut,
    status_code=201,
    tags=["registry"],
    summary="登记空容器",
)
def create_container(
    body: ContainerIn, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    service.register_empty_container(conn, body.container_id)
    return {"container_id": body.container_id}


@app.put(
    "/containers/{container_id}/capacity",
    response_model=ContainerStatusOut,
    tags=["registry"],
    summary="设置或修改容器容量上限（null 表示不限制）",
)
def set_container_capacity(
    container_id: str,
    body: CapacityIn,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    return service.set_container_capacity(conn, container_id, body.capacity)


@app.post(
    "/transfers/batch",
    response_model=BatchTransferOut,
    tags=["transfers"],
    summary="批量提交依次发生的转移（整批原子，支持可选幂等键）",
)
def batch_transfers(
    body: BatchTransferIn,
    raw_body: bytes = Depends(raw_request_body),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    triples = [(t.source, t.destination, t.volume) for t in body.transfers]
    if body.idempotency_key is None:
        # 不携带幂等键：完全保持旧版本行为（由 response_model 序列化）
        applied, versions = service.apply_transfers(
            conn, triples, body.expected_versions
        )
        return {
            "applied": applied,
            "message": f"已依次提交 {applied} 条转移",
            "versions": versions,
        }

    # 指纹基于未经解析的原始请求体字节：重放逐字节一致，异字节即 409
    result = service.apply_transfers_idempotent(
        conn,
        triples,
        body.expected_versions,
        idempotency_key=body.idempotency_key,
        request_sha256=service.fingerprint_request(raw_body),
    )
    # 首次与重放都返回事务中持久化/取回的原始 JSON，保证逐字节同一响应；
    # 重放额外给出 Idempotency-Replayed 头，体积与 version 均未再次变化。
    headers = {"Idempotency-Replayed": "true"} if result.replayed else None
    return Response(
        content=json.dumps(result.payload, ensure_ascii=False, separators=(",", ":")),
        status_code=result.status_code,
        media_type="application/json",
        headers=headers,
    )


@app.post(
    "/transfers/preview",
    response_model=BatchPreviewOut,
    tags=["transfers"],
    summary="预演批量转移（不落库、不写幂等记录）",
)
def preview_transfers(
    body: BatchTransferIn, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    triples = [(t.source, t.destination, t.volume) for t in body.transfers]
    # 预演只读：即使携带 idempotency_key 也一律忽略（不查重、不重放、不落记录）
    return service.preview_transfers(conn, triples, body.expected_versions)


@app.get(
    "/containers/{container_id}",
    response_model=ContainerStatusOut,
    tags=["query"],
    summary="查询容器当前体积、容量（上限/剩余/利用率）及来源占比",
)
def container_status(
    container_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    return service.get_container_status(conn, container_id)


# --------------------------------------------------------------------------- #
# 审计链查询与校验
# --------------------------------------------------------------------------- #
@app.get(
    "/audit/verify",
    response_model=AuditVerifyOut,
    tags=["audit"],
    summary="校验整条审计链的完整性（序号、哈希链、载荷、容器索引）",
)
def verify_audit_chain(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    # 链本身是否有效属于正常查询结果，始终返回 200，结果在 valid / break 中
    return audit.verify_audit_chain(conn)


@app.get(
    "/audit/records",
    response_model=AuditListOut,
    tags=["audit"],
    summary="按容器与序号范围查询审计记录（按 seq 升序）",
)
def list_audit_records(
    container_id: str | None = None,
    start_seq: int | None = None,
    end_seq: int | None = None,
    limit: int = audit.DEFAULT_LIMIT,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    return audit.get_audit_records(
        conn,
        container_id=container_id.strip() if container_id else None,
        start_seq=start_seq,
        end_seq=end_seq,
        limit=limit,
    )
