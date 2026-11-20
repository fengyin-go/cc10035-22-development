"""实体来源索引与重放清单接口。

路径放在通用资源路由之前注册，避免 /api/{module}/{entry_id} 抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from app import domain_signature as sig
from app.services import source_index
from app.services.dom_parser import DomParserService
from app.services.entity_extract import EntityExtractService

router = APIRouter(prefix="/api", tags=["实体来源索引"])

entity_service = EntityExtractService()
dom_service = DomParserService()


@router.get("/entity-source-index")
def search_source_index(
    page_uri: str | None = Query(default=None),
    field: str | None = Query(default=None),
    signature: str | None = Query(default=None),
    version: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    include_unsigned: bool = Query(default=False),
) -> dict[str, Any]:
    """按页面、字段、解析签名（可带版本条件）检索可复验的实体批次。"""
    return entity_service.search_source_index(
        page_uri=page_uri, field=field, signature=signature, version=version,
        entity_id=entity_id, include_unsigned=include_unsigned,
    )


@router.post("/entity-source-index/search")
def search_and_writeback(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """检索；payload.writeback=true 时把选中命中回写到重放清单。"""
    query = {
        "page_uri": payload.get("page_uri"),
        "field": payload.get("field"),
        "signature": payload.get("signature"),
        "version": payload.get("version"),
        "entity_id": payload.get("entity_id"),
        "include_unsigned": bool(payload.get("include_unsigned")),
    }
    if payload.get("writeback", True):
        return entity_service.search_and_writeback(**query)
    return entity_service.search_source_index(**query)


@router.post("/entity-source-index/writeback")
def writeback(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """把已选中的命中回写到重放清单，固化查询范围与配置快照。"""
    query = dict(payload.get("query") or {})
    entity_ids = [str(item) for item in (payload.get("entity_ids") or [])]
    result = entity_service.writeback_hits(
        query=query, entity_ids=entity_ids,
        submit_note=sig.text(payload.get("submit_note")),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("reason", "回写失败"))
    return result


@router.get("/entity-source-index/stats")
def source_index_stats() -> dict[str, Any]:
    return entity_service.source_index_overview()


@router.get("/entity-source-index/entries")
def source_index_entries(
    page_uri: str | None = Query(default=None),
    signature: str | None = Query(default=None),
    version: str | None = Query(default=None),
) -> dict[str, Any]:
    return entity_service.source_index_entries(
        page_uri=page_uri, signature=signature, version=version)


@router.get("/entity-source-index/unsigned")
def unsigned_entries(
    page_uri: str | None = Query(default=None),
    field: str | None = Query(default=None),
) -> dict[str, Any]:
    """历史未补签名的记录：能唯一判定版本才补签，否则如实列出。"""
    return entity_service.unsigned_entities(page_uri=page_uri, field=field)


@router.post("/entity-source-index/rebuild")
def rebuild_source_index(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return entity_service.build_source_index(page_uri=payload.get("page_uri"))


@router.get("/replay-manifest")
def list_replay_manifest(
    status: str | None = Query(default=None),
    signature: str | None = Query(default=None),
    page_uri: str | None = Query(default=None),
    version: str | None = Query(default=None),
) -> dict[str, Any]:
    return entity_service.list_replays(
        status=status, signature=signature, page_uri=page_uri, version=version)


@router.get("/replay-manifest/{replay_id}")
def get_replay_manifest(replay_id: str) -> dict[str, Any]:
    result = entity_service.replay_manifest(replay_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "记录不存在"))
    return result


@router.post("/replay-manifest/{replay_id}/submit-to-dom")
def submit_to_dom(replay_id: str,
                  payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """值班员查到旧签名命中后，送回 DOM 解析复验。"""
    result = entity_service.submit_hit_to_dom(
        replay_id,
        operator=sig.text(payload.get("operator")) or "值班员",
        note=sig.text(payload.get("note")),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "记录不存在"))
    return result


@router.post("/replay-manifest/{replay_id}/dom-replay")
def dom_replay(replay_id: str) -> dict[str, Any]:
    """DOM 解析按重放清单里的快照复验实际字段。"""
    result = dom_service.replay(replay_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "记录不存在"))
    return result


@router.get("/dom-parser-configs/{entry_id}/snapshots")
def list_config_snapshots(entry_id: int) -> dict[str, Any]:
    entry = dom_service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="解析配置不存在")
    snapshots = dom_service.iter_snapshots(entry)
    return {"ok": True, "config_id": entry_id, "items": snapshots,
            "current_version": sig.normalize_version(entry.get("配置版本")),
            "total": len(snapshots)}


@router.post("/dom-parser-configs/{entry_id}/revise")
def revise_config(entry_id: int,
                  payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """更正配置（页面/引擎/选择器/Schema），自动升版并推送来源索引。"""
    result = dom_service.revise_config(
        entry_id, dict(payload.get("changes") or {}),
        operator=sig.text(payload.get("operator")) or "值班员",
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("reason", "更正失败"))
    return result


@router.post("/dom-parser-configs/{entry_id}/push-signature")
def push_signature(entry_id: int) -> dict[str, Any]:
    return dom_service.handoff_to_entity_index(entry_id)


@router.get("/dom-parser-configs/replay-candidates")
def replay_candidates() -> dict[str, Any]:
    """DOM 解析侧查看待复验的重放清单。"""
    items = source_index.list_replay_items(status="待重放")
    return {"ok": True, "items": items, "total": len(items)}
