FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/data/lineage.db

WORKDIR /app

# requirements 单独拷贝以利用构建缓存；依赖也会在容器启动时由 entrypoint 再安装一次
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh && mkdir -p /data

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
