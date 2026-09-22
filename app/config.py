"""运行期配置，全部来自环境变量（容器中由 docker-compose 注入）。"""
from __future__ import annotations

import os
from decimal import Decimal


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Settings:
    # SQLite 数据库文件路径
    database_path: str = os.environ.get("DATABASE_PATH", "/data/lineage.db")

    # 体积/金额保留的小数位数（提交体积累加得到，比例不受此限）
    decimal_scale: int = max(0, _get_int("DECIMAL_SCALE", 9))

    # 结果中比例输出的小数位数
    proportion_scale: int = max(0, _get_int("PROPORTION_SCALE", 6))

    # 除法等中间运算的 Decimal 精度
    decimal_precision: int = max(28, _get_int("DECIMAL_PRECISION", 50))


settings = Settings()

# 体积最小刻度：10**-SCALE
VOLUME_QUANTUM = Decimal(1).scaleb(-settings.decimal_scale)
