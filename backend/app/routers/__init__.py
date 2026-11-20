"""业务路由汇总：实体来源索引接口先注册，通用资源路由兜底 /api/{module}。"""
from __future__ import annotations

from app.routers import resource, source_index

ROUTERS = [source_index, resource]
