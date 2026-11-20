"""Web语义抽取与内容理解中心 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.services import source_index
from app.store import store

app = FastAPI(title="Web语义抽取与内容理解中心", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()


@app.on_event("startup")
def warm_entity_source_index() -> None:
    """启动时把解析配置的来源和签名推一遍，索引随示例数据就绪。"""
    try:
        from app.services.dom_parser import DomParserService
        DomParserService().push_all_signatures(origin="服务启动")
        source_index.register_unsigned_entities()
    except Exception:  # 索引预热失败不能拦住服务启动
        pass


# 资源路由含 /api/{module}，必须放在 health / overview 之后注册，
# 否则固定路径会被动态路径抢先匹配。
for module in ROUTERS:
    app.include_router(module.router)
