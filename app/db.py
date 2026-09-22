"""SQLite 连接管理与建库。

容器启动时调用 init_db() 建库建表；应用通过 FastAPI 依赖拿到连接。
为支持事务回滚，连接使用显式 BEGIN/COMMIT/ROLLBACK（autocommit 隔离级别）。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .audit import AUDIT_SCHEMA_SQL, install_audit_triggers
from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS containers (
    id      TEXT PRIMARY KEY,
    -- 乐观并发版本：创建时为 0，每次成功的批量转移触碰后加 1（同批多次触碰只加 1）
    version INTEGER NOT NULL DEFAULT 0,
    -- 容量上限：NULL 表示不限制；非 NULL 为 Decimal 字符串，必须 >= 当前体积
    capacity TEXT,
    created INTEGER NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 容器中来自每个祖先初始样品的“绝对体积份额”。
-- 初始样品登记后占 1 行（source_id = 自身）；空容器为 0 行。
-- Decimal 以字符串存储，杜绝浮点误差。
CREATE TABLE IF NOT EXISTS components (
    container_id TEXT NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
    source_id    TEXT NOT NULL,
    amount       TEXT NOT NULL,
    PRIMARY KEY (container_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_components_source ON components(source_id);

-- POST /transfers/batch 的幂等记录：仅在批次成功提交时，与容器扣减/版本递增
-- 写入同一个事务。重放（同键同请求体）直接返回 response_body，不再扣减体积。
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    -- 首次成功请求的原始请求体指纹（SHA-256 hex）；同键但指纹不同 -> 409
    request_sha256  TEXT NOT NULL,
    -- 首次响应的 HTTP 状态码与完整响应体（compact JSON 原文，逐字节重放）
    status_code     INTEGER NOT NULL,
    response_body   TEXT NOT NULL,
    created INTEGER NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
""" + AUDIT_SCHEMA_SQL


def _migrate(conn: sqlite3.Connection) -> None:
    """对已有数据库做幂等升级（旧库没有 containers.version / capacity 列）。

    ``idempotency_keys`` 与审计链相关的表（``audit_records``、
    ``audit_record_containers``）由建表脚本中的 ``CREATE TABLE IF NOT EXISTS``
    负责补齐，旧库升级时自动创建，无需手工迁移；只追加审计触发器在此幂等安装。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(containers)")}
    if columns and "version" not in columns:
        # 旧库中的容器版本统一视为 0（DEFAULT 0 会回填到每一行）
        conn.execute(
            "ALTER TABLE containers ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
        )
    if columns and "capacity" not in columns:
        # 旧库中的容器一律不限制容量（NULL）
        conn.execute("ALTER TABLE containers ADD COLUMN capacity TEXT")
    # 审计表的只追加触发器（BEFORE UPDATE/DELETE -> RAISE），已存在则跳过
    install_audit_triggers(conn)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.database_path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # 显式事务
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    """创建数据目录与全部表（幂等，容器启动时执行），并兼容升级旧库。"""
    directory = os.path.dirname(os.path.abspath(settings.database_path))
    os.makedirs(directory, exist_ok=True)
    conn = connect()
    try:
        # executescript 自带事务语义；各语句均为 IF NOT EXISTS，幂等
        conn.executescript(_SCHEMA)
        # 旧版本数据库缺少 version 列时补齐（ALTER TABLE 幂等由本函数判断）
        _migrate(conn)
    finally:
        conn.close()


@contextmanager
def get_conn_context() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 请求依赖。"""
    with get_conn_context() as conn:
        yield conn
