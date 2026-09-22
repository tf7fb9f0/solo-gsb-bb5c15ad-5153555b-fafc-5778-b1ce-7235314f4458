#!/bin/sh
# 容器启动时：1) 安装依赖  2) 建库  3) 启动服务
set -e

echo "[entrypoint] 安装依赖..."
pip install --no-cache-dir -r /app/requirements.txt

echo "[entrypoint] 初始化数据库..."
python -c "from app.db import init_db; init_db(); print('database ready at', __import__('app.config', fromlist=['settings']).settings.database_path)"
echo "[entrypoint] 数据库就绪（旧库会自动补齐 containers.version / containers.capacity 列、idempotency_keys 表与审计链表 audit_records / audit_record_containers 及只追加触发器，幂等迁移，无需手工操作）"

echo "[entrypoint] 启动 uvicorn (0.0.0.0:${PORT:-8000})..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
