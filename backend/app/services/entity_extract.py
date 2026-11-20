"""实体抽取领域服务：从DOM解析结果中识别命名实体。

除了抽取本身，这里承接“实体是从哪一版解析配置来的”：

* 解析配置把来源和签名下发过来后，按页面/字段/签名建实体来源数据索引；
* 按页面、字段、解析签名（可带版本条件）检索一批复验得动的命中；
* 检索选中的命中回写到重放清单，再由 DOM 解析按清单复验实际字段。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from app import domain_signature as sig
from app.services.base import BaseModuleService
from app.services import source_index
from app.store import store


ENTITY_TYPE_MAP = {
    "title": re.compile(r"<title[^>]*>(.*?)</title>", re.I),
    "heading": re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>", re.I),
    "link": re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I),
    "meta_value": re.compile(r'<meta[^>]+content=["\']([^"\']+)["\']', re.I),
}


class EntityExtractService(BaseModuleService):
    MODULE = "entity_extract"
    ENTITY = "实体条目"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["实体ID", "实体类型", "实体名称", "页面来源", "原文片段", "置信度", "抽取时间",
                   "关联字段", "解析签名", "配置版本"]
    REQUIRED_FIELDS = ["实体ID", "实体类型", "实体名称"]
    STATUS_ORDER = ["已抽取", "已校验", "已入库", "已废弃"]
    ACTION_RULES = {"校验实体": "已抽取", "确认入库": "已校验", "标记废弃": "已抽取"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记实体后立即挂到来源索引；历史无签名记录按补签规则处理。"""
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        # 允许登记时带配置ID+版本反查签名，避免操作员手工抄签名。
        if not sig.text(entry.get("解析签名")) and sig.text(entry.get("配置ID")):
            from app.services.dom_parser import DomParserService
            resolved = DomParserService().resolve_snapshot(
                config_id=int(entry["配置ID"]) if str(entry["配置ID"]).isdigit() else None,
                version=entry.get("配置版本"),
                page_uri=entry.get("页面来源"),
            )
            if resolved.get("ok"):
                entry["解析签名"] = resolved["snapshot"].get("解析签名", "")
                entry["配置版本"] = sig.normalize_version(
                    entry.get("配置版本")
                ) or sig.normalize_version(resolved["snapshot"].get("配置版本"))
        source_index.register_entity_entry(entry)
        return entry, missing

    # ------------------------------------------------------------------ 来源索引

    def accept_config_source(self, config: dict[str, Any]) -> dict[str, Any]:
        """接收解析配置下发的来源与签名，登记实体来源数据索引（幂等）。"""
        return source_index.register_snapshot(config, origin="实体抽取接收")

    def build_source_index(self, page_uri: str | None = None) -> dict[str, Any]:
        """按页面构建来源索引：拉取该页所有配置版本，并兼容历史未签名记录。"""
        from app.services.dom_parser import DomParserService

        dps = DomParserService()
        outcomes: list[dict[str, Any]] = []
        pages: list[str] = []
        if page_uri:
            pages = [sig.text(page_uri)]
        else:
            pages = sorted({sig.text(row.get("目标URI")) for row in store.rows("dom_parser")
                            if sig.text(row.get("目标URI"))})
        for page in pages:
            for row in store.rows("dom_parser"):
                if sig.text(row.get("目标URI")) != page:
                    continue
                for snapshot in dps.iter_snapshots(row):
                    outcomes.append(source_index.register_snapshot(
                        snapshot, origin="实体抽取构建索引"))
        unsigned = source_index.register_unsigned_entities(page_uri=page_uri)
        stats = source_index.source_index_stats()
        return {
            "ok": True,
            "pages": pages,
            "config_pushes": len(outcomes),
            "index_entries": sum(int(item.get("entry_count", 0)) for item in outcomes),
            "unsigned_entries": len(unsigned),
            "stats": stats,
        }

    def search_source_index(self, **query: Any) -> dict[str, Any]:
        """按页面、字段、解析签名（可带版本）检索一批评审命中。"""
        entries = source_index.search_entries(**query)
        hits = source_index.flatten_hits(entries, requested_version=query.get("version"))
        scope = {
            "page_uri": sig.text(query.get("page_uri")),
            "field": sig.text(query.get("field")),
            "signature": sig.text(query.get("signature")),
            "version": sig.normalize_version(query.get("version")),
            "include_unsigned": bool(query.get("include_unsigned")),
        }
        return {
            "ok": True,
            "query": scope,
            "entries": entries,
            "hits": hits,
            "total": len(hits),
            "query_scope_note": (
                "结果只包含查询条件限定的页面/字段/签名/版本范围内的命中"
            ),
        }

    def _writeback_scope(self, query: dict[str, Any], entries: list[dict[str, Any]],
                         hits: list[dict[str, Any]], source_replay_id: str | None = None,
                         submit_note: str = "") -> dict[str, Any]:
        """按来源版本拆清单：一份重放清单只对应一个解析签名/版本快照。"""
        scoped_entries = [
            entry for entry in entries
            if any(hit.get("索引ID") == entry.get("索引ID") for hit in hits)
        ] or entries
        groups = source_index.group_by_source(scoped_entries)
        items = []
        unverifiable = []
        for signature, version, group in groups:
            group_ids = {entry.get("索引ID") for entry in group}
            group_hits = [hit for hit in hits if hit.get("索引ID") in group_ids]
            if not group_hits:
                continue
            if not signature:
                # 无法补签名的历史记录也要进清单，复验时才能如实说明不能复验。
                unverifiable.extend(group_hits)
                items.append(source_index.create_replay_item(
                    query={**query, "signature": "", "version": ""},
                    entries=group, hits=group_hits, snapshot=None,
                    snapshot_version="", source_replay_id=source_replay_id,
                    submit_note=submit_note,
                ))
                continue
            snapshot, snapshot_version = source_index.pick_snapshot(group, version)
            group_query = {
                **query,
                "signature": signature,
                "version": version,
            }
            items.append(source_index.create_replay_item(
                query=group_query, entries=group, hits=group_hits,
                snapshot=snapshot, snapshot_version=snapshot_version,
                source_replay_id=source_replay_id, submit_note=submit_note,
            ))
        return {"replays": items, "unverifiable_hits": unverifiable}

    def search_and_writeback(self, **query: Any) -> dict[str, Any]:
        """检索并立刻把选中的命中回写到重放清单（固化查询范围和配置快照）。"""
        result = self.search_source_index(**query)
        entries = result["entries"]
        hits = result["hits"]
        if not hits:
            return {**result, "replay": None, "replays": [],
                    "message": "查询范围内没有可复验的实体命中"}
        written = self._writeback_scope(result["query"], entries, hits)
        first = written["replays"][0] if written["replays"] else None
        return {**result, "replay": first, "replays": written["replays"],
                "unverifiable_hits": written["unverifiable_hits"]}

    def writeback_hits(self, *, query: dict[str, Any], entity_ids: list[str],
                       submit_note: str = "") -> dict[str, Any]:
        """把已选中的命中（而非整次检索）回写到重放清单。"""
        entries = source_index.search_entries(
            page_uri=query.get("page_uri"),
            field=query.get("field"),
            signature=query.get("signature"),
            version=query.get("version"),
            include_unsigned=bool(query.get("include_unsigned")),
            # 回写也要补齐该页全部版本，检索与回写必须落在同一索引范围。
            auto_index=True,
        )
        wanted = {sig.text(item) for item in entity_ids}
        hits = [
            hit for hit in source_index.flatten_hits(
                entries, requested_version=query.get("version"))
            if not wanted or sig.text(hit.get("实体ID")) in wanted
        ]
        if not hits:
            return {"ok": False, "reason": "选中的实体不在当前查询范围内", "replay": None}
        written = self._writeback_scope(query, entries, hits, submit_note=submit_note)
        first = written["replays"][0] if written["replays"] else None
        return {"ok": True, "replay": first, "replays": written["replays"],
                "hits": hits, "total": len(hits),
                "unverifiable_hits": written["unverifiable_hits"]}

    def list_replays(self, **filters: Any) -> dict[str, Any]:
        items = source_index.list_replay_items(**filters)
        return {"ok": True, "items": items, "total": len(items)}

    def replay_manifest(self, replay_id: Any) -> dict[str, Any]:
        item = source_index.find_replay_item(replay_id)
        if item is None:
            return {"ok": False, "reason": "重放清单记录不存在"}
        return {"ok": True, "item": item}

    def submit_hit_to_dom(self, replay_id: Any, *, operator: str = "值班员",
                          note: str = "") -> dict[str, Any]:
        """值班员查到旧签名的实体命中后，把整条清单送回 DOM 解析复验。

        旧签名的命中可能来自已被更正的配置版本，这里显式固化“来源重放清单”，
        由 DOM 解析按清单快照复验，不会按当前配置重新检索。
        """
        from app.services.dom_parser import DomParserService

        item = source_index.find_replay_item(replay_id)
        if item is None:
            return {"ok": False, "reason": "重放清单记录不存在"}
        item["转送状态"] = "已转送DOM解析"
        item["转送人"] = operator
        item["转送时间"] = source_index.utc_now()
        if note:
            item["转送说明"] = note
        result = DomParserService().handoff_replay_to_dom(item.get("重放ID"), operator=operator)
        result["重放清单"] = {
            "重放ID": item.get("重放ID"),
            "解析签名": item.get("解析签名"),
            "配置快照版本": item.get("配置快照版本"),
            "命中数": item.get("命中数", 0),
        }
        return result

    def unsigned_entities(self, page_uri: str | None = None,
                          field: str | None = None) -> dict[str, Any]:
        entries = source_index.unsigned_index_entries(page_uri=page_uri, field=field)
        return {"ok": True, "items": entries, "total": len(entries)}

    def source_index_overview(self) -> dict[str, Any]:
        return {"ok": True, "stats": source_index.source_index_stats()}

    def source_index_entries(self, page_uri: str | None = None,
                             signature: str | None = None,
                             version: str | None = None) -> dict[str, Any]:
        entries = source_index.search_entries(
            page_uri=page_uri, signature=signature, version=version,
            include_unsigned=True,
        )
        return {"ok": True, "items": entries, "total": len(entries)}

    # ------------------------------------------------------------------ 抽取与统计

    def extract_from_parse(self, dom_parser_config_id: int) -> dict[str, Any]:
        """从DOM解析配置的输出中抽取实体，整合 dom_parser -> entity_extract 消费链。"""
        from app.services.dom_parser import DomParserService
        dps = DomParserService()
        feed = dps.feed_entity_extract(dom_parser_config_id)
        if not feed["ok"]:
            return feed
        source_uri = feed["source_uri"]
        signature = feed["parse_hash"]
        version = sig.normalize_version(feed.get("config_version"))
        fields = feed["fields_for_extraction"]
        entities = []
        for idx, field in enumerate(fields):
            eid = hashlib.md5(f"{source_uri}|{field}|{signature}".encode()).hexdigest()[:8]
            confidence = round(0.95 - (0.05 * (idx % 3)), 2)
            entities.append({
                "实体ID": eid,
                "实体类型": self._guess_type(field),
                "实体名称": field,
                "页面来源": source_uri,
                "原文片段": f"提取自 {source_uri} #{idx+1}",
                "置信度": str(confidence),
                "抽取时间": "2026-11-19",
                "关联字段": field,
                "解析签名": signature,
                "配置ID": dom_parser_config_id,
                "配置版本": version,
                "字段签名": sig.compute_field_signature(signature, field),
            })
        # 抽取产物同步挂到来源索引，检索才有东西可查。
        outcome = source_index.register_snapshot(
            feed["snapshot"],
            entities_by_field={item["关联字段"]: [item] for item in entities},
            origin="抽取产物登记",
            refresh_entities=False,
        )
        return {
            "ok": True,
            "source_uri": source_uri,
            "parse_hash": signature,
            "config_version": version,
            "entity_count": len(entities),
            "entities": entities,
            "source_index": {
                "entry_count": outcome.get("entry_count", 0),
                "signature": outcome.get("signature", ""),
            },
        }

    def compute_entity_distribution(self) -> dict[str, int]:
        """计算实体类型分布，供聚合视图消费。"""
        rows = store.rows(self.MODULE)
        dist: dict[str, int] = {}
        for row in rows:
            etype = row.get("实体类型", "未知")
            dist[etype] = dist.get(etype, 0) + 1
        return dist

    def extract_for_relation_graph(self) -> list[dict[str, Any]]:
        """将已入库实体提供给关系图谱模块构建关系。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("status") == "已入库"]
        return [
            {"entity_id": r.get("实体ID"), "entity_type": r.get("实体类型"),
             "entity_name": r.get("实体名称"), "source_page": r.get("页面来源"),
             "parse_signature": r.get("解析签名", ""),
             "config_version": r.get("配置版本", "")}
            for r in rows
        ]

    def _guess_type(self, field_name: str) -> str:
        field_lower = field_name.lower()
        if any(k in field_lower for k in ("name", "名称", "title", "标题")):
            return "命名实体"
        if any(k in field_lower for k in ("price", "价格", "amount", "金额")):
            return "数值实体"
        if any(k in field_lower for k in ("date", "日期", "time", "时间")):
            return "时间实体"
        if any(k in field_lower for k in ("url", "link", "链接")):
            return "链接实体"
        return "属性实体"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["entity_distribution"] = self.compute_entity_distribution()
        base["ready_for_relation"] = len(self.extract_for_relation_graph())
        base["source_index"] = source_index.source_index_stats()
        return base
