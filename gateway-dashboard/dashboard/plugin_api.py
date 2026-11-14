"""gateway-dashboard dashboard-API —— 挂 /api/plugins/gateway-dashboard/（spike3 最小面）。

只读、无副作用；handler 全量 try/except（§7-2 崩溃隔离：装坏只坏本命名空间）。
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter

logger = logging.getLogger("gateway-dashboard.api")

router = APIRouter()


@router.get("/ping")
async def ping():
    """spike3 挂载探针：返回固定结构即证明发现/信任闸/import/mount 全通。"""
    return {"ok": True, "plugin": "gateway-dashboard", "ts": time.time()}
