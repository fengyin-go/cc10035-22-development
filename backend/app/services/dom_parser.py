"""DOM解析领域服务：管理DOM解析配置，驱动下游实体抽取流水线。

配置在登记和更正时都会生成带版本号的**签名快照**（页面、引擎、选择器、
输出 Schema 的解析签名）。实体来源索引和重放清单都只认快照，不认“当前配置”，
这样检索与重放之间别人改了选择器，已选中的命中仍然按当时的配置范围取数。
"""
from __future__ import annotations

import hashlib
import re
import threading
from typing import Any

from app import domain_signature as sig
from app.services.base import BaseModuleService
from app.services import source_index
from app.store import store


CSS_SELECTOR_RE = re.compile(r"^[.#]?[a-zA-Z_][\w\-]*(?:\s*[>+~]\s*[.#]?[a-zA-Z_][\w\-]*)*$")
XPath_RE = re.compile(r"^(//|[.]/|/)[\w\[\]@='\"\-,.()*/]+")

SNAPSHOT_CONFIG_FIELDS = ("目标URI", "解析引擎", "选择器规则", "输出Schema")

# 并发更正同一配置时串行化升版，避免两个请求各升出一个同号新版本。
CONFIG_LOCK = threading.RLock()


class DomParserService(BaseModuleService):
    MODULE = "dom_parser"
    ENTITY = "解析配置"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["配置名称", "目标URI", "解析引擎", "选择器规则", "输出Schema", "等待策略", "渲染模式"]
    REQUIRED_FIELDS = ["配置名称", "目标URI", "解析引擎"]
    STATUS_ORDER = ["草稿", "已配置", "解析中", "已停止"]
    ACTION_RULES = {"确认配置": "草稿", "启动解析": "已配置", "停止解析": "解析中"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------ 版本快照

    def _snapshot_from_row(self, entry: dict[str, Any], version: str,
                           created_at: str | None = None) -> dict[str, Any]:
        uri = sig.text(entry.get("目标URI"))
        engine = sig.text(entry.get("解析引擎"))
        selector = sig.text(entry.get("选择器规则"))
        schema = sig.text(entry.get("输出Schema"))
        return {
            "配置ID": entry.get("id"),
            "配置名称": entry.get("配置名称"),
            "配置版本": version,
            "页面URI": uri,
            "解析引擎": engine,
            "选择器规则": selector,
            "选择器列表": sig.split_selectors(selector),
            "输出Schema": schema,
            "字段列表": sig.split_fields(schema),
            "解析签名": sig.compute_parse_signature(uri, engine, selector, schema),
            "快照时间": created_at or source_index.utc_now(),
        }

    def ensure_snapshot(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """保证配置有版本历史和当前快照；配置内容被直接改过时自动升版。

        返回当前版本快照。字段不完整（无 URI/引擎/选择器等）时快照签名可能为空，
        此时仍然记录版本，但下游会标记为“无法补签名”。
        """
        if entry is None:
            return None
        if not entry.get("配置版本"):
            row_id = entry.get("id")
            if row_id is None:
                return None
            entry["配置版本"] = "v1"
            first = self._snapshot_from_row(entry, "v1")
            entry["历史版本"] = [first]
            entry["解析签名"] = first["解析签名"]
            return first

        history: list[dict[str, Any]] = entry.setdefault("历史版本", [])
        latest = history[-1] if history else None
        current = self._snapshot_from_row(entry, entry.get("配置版本") or "v1")
        if latest is None:
            history.append(current)
            entry["解析签名"] = current["解析签名"]
            return current
        if current["解析签名"] != latest.get("解析签名") or any(
            sig.text(current.get(key)) != sig.text(latest.get(key))
            for key in SNAPSHOT_CONFIG_FIELDS
        ):
            version = sig.next_version(history)
            bumped = self._snapshot_from_row(entry, version)
            history.append(bumped)
            entry["配置版本"] = version
            entry["解析签名"] = bumped["解析签名"]
            return bumped
        entry["解析签名"] = latest.get("解析签名", "")
        return latest

    def iter_snapshots(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """返回配置的全部版本快照（旧版本在前）。"""
        self.ensure_snapshot(entry)
        return [snapshot for snapshot in entry.get("历史版本", []) if snapshot]

    def current_snapshot(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        return self.ensure_snapshot(entry)

    def resolve_snapshot(self, *, signature: str | None = None,
                         page_uri: str | None = None,
                         config_id: int | None = None,
                         version: str | None = None) -> dict[str, Any]:
        """按签名/页面/配置/版本定位一条历史快照。"""
        wanted_signature = sig.text(signature)
        wanted_page = sig.text(page_uri)
        wanted_version = sig.normalize_version(version)
        for row in store.rows(self.MODULE):
            if config_id is not None and int(row.get("id", 0)) != int(config_id):
                continue
            for snapshot in self.iter_snapshots(row):
                if wanted_signature and snapshot.get("解析签名") != wanted_signature:
                    continue
                if wanted_page and snapshot.get("页面URI") != wanted_page:
                    continue
                if wanted_version and sig.normalize_version(snapshot.get("配置版本")) != wanted_version:
                    continue
                return {"ok": True, "snapshot": snapshot, "config": row}
        return {"ok": False, "reason": "没有匹配的解析配置版本快照"}

    def snapshots_for_field(self, page_uri: str, field: str) -> list[dict[str, Any]]:
        """同页同字段的所有配置版本快照，用于历史记录补签名。"""
        result: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            if sig.text(row.get("目标URI")) != sig.text(page_uri):
                continue
            for snapshot in self.iter_snapshots(row):
                if field in (snapshot.get("字段列表") or []):
                    result.append(snapshot)
        return result

    def sync_config_versions(self) -> list[dict[str, Any]]:
        """给所有配置补齐版本快照，被检索索引和概览调用。"""
        snapshots = []
        for row in store.rows(self.MODULE):
            snapshot = self.ensure_snapshot(row)
            if snapshot:
                snapshots.append(snapshot)
        return snapshots

    # ------------------------------------------------------------------ 登记/更正

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        # 登记即 v1：把来源和签名推给实体抽取来源索引。
        snapshot = self.ensure_snapshot(entry)
        entry["配置版本"] = snapshot["配置版本"] if snapshot else "v1"
        entry["解析签名"] = snapshot["解析签名"] if snapshot else ""
        self.push_signature(entry["id"], origin="配置登记")
        return entry, missing

    def revise_config(self, entry_id: int, changes: dict[str, Any],
                      *, operator: str = "值班员") -> dict[str, Any]:
        """更正配置的页面/引擎/选择器/Schema，自动升版并推送到来源索引。

        旧版本快照与旧签名的索引条目继续保留，已回写的重放清单不受影响。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": "解析配置不存在"}
        with CONFIG_LOCK:
            self.ensure_snapshot(entry)
            applied: dict[str, Any] = {}
            for key in SNAPSHOT_CONFIG_FIELDS:
                if key in changes and sig.text(changes.get(key)) != sig.text(entry.get(key)):
                    applied[key] = sig.text(changes.get(key))
            if not applied:
                return {"ok": False, "reason": "没有可更正的配置字段",
                        "snapshot": entry.get("历史版本", [])[-1]}
            previous = entry.get("历史版本", [])[-1]
            entry.update(applied)
            entry["更正人"] = operator
            entry["最近更正时间"] = source_index.utc_now()
            snapshot = self.ensure_snapshot(entry)
            push = self.push_signature(entry_id, origin="配置更正")
        return {
            "ok": True,
            "config_id": entry_id,
            "previous_version": previous.get("配置版本"),
            "previous_signature": previous.get("解析签名"),
            "current_version": snapshot.get("配置版本"),
            "current_signature": snapshot.get("解析签名"),
            "changed_fields": sorted(applied),
            "index_push": push,
            "snapshot": snapshot,
        }

    def push_signature(self, entry_id: int, *, origin: str = "配置下发") -> dict[str, Any]:
        """把配置的当前来源和签名交给实体抽取，形成/更新来源数据索引。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": "解析配置不存在"}
        snapshot = self.ensure_snapshot(entry)
        if snapshot is None:
            return {"ok": False, "reason": "配置缺少可生成快照的字段"}
        result = source_index.register_snapshot(snapshot, origin=origin)
        result["config_id"] = entry_id
        return result

    def push_all_signatures(self, *, origin: str = "批量下发") -> dict[str, Any]:
        pushed = [self.push_signature(int(row["id"]), origin=origin)
                  for row in store.rows(self.MODULE) if row.get("id") is not None]
        return {
            "ok": True,
            "config_count": len(pushed),
            "signed_configs": sum(1 for item in pushed if item.get("signature")),
            "unsigned_configs": sum(1 for item in pushed if not item.get("signature")),
            "index_entries": sum(int(item.get("entry_count", 0)) for item in pushed),
        }

    # ------------------------------------------------------------------ 解析与联动

    def validate_selector(self, selector_rule: str) -> dict[str, Any]:
        """校验选择器规则语法，返回类型与合法标志。"""
        is_css = bool(CSS_SELECTOR_RE.match(selector_rule.strip()))
        is_xpath = bool(XPath_RE.match(selector_rule.strip()))
        return {
            "selector": selector_rule,
            "syntax_type": "css" if is_css else ("xpath" if is_xpath else "unknown"),
            "valid": is_css or is_xpath,
        }

    def simulate_parse(self, entry: dict[str, Any],
                       snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        """模拟一次DOM解析，输出解析字段摘要和哈希签名。

        传入快照时严格按快照的选择器/字段解析，不读取配置的当前值。
        """
        source = snapshot or entry
        uri = sig.text(source.get("页面URI") or source.get("目标URI"))
        engine = sig.text(source.get("解析引擎")) or "beautifulsoup"
        selector = sig.text(source.get("选择器规则"))
        schema = sig.text(source.get("输出Schema"))
        selectors = sig.split_selectors(selector)
        fields = sig.split_fields(schema)
        parser_hash = sig.compute_parse_signature(uri, engine, selector, schema)
        return {
            "config_name": source.get("配置名称") or entry.get("配置名称"),
            "config_version": sig.normalize_version(source.get("配置版本")),
            "target_uri": uri,
            "parser_engine": engine,
            "selector_count": len(selectors),
            "schema_fields": fields,
            "dom_hash": parser_hash,
            "extracted_records": 0,
        }

    def _field_values(self, snapshot: dict[str, Any], fields: list[str]) -> dict[str, str]:
        """按快照选择器模拟取数：同样的选择器+字段得到稳定值，便于复验核对。"""
        selector = sig.text(snapshot.get("选择器规则"))
        values: dict[str, str] = {}
        for field in fields:
            token = hashlib.md5(
                f"{snapshot.get('解析签名')}|{selector}|{field}".encode()
            ).hexdigest()[:6]
            values[field] = f"{field}#{token}"
        return values

    def feed_entity_extract(self, entry_id: int) -> dict[str, Any]:
        """将解析结果输出为实体抽取的输入：被 entity_extract 消费。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": "解析配置不存在"}
        snapshot = self.ensure_snapshot(entry)
        parse_result = self.simulate_parse(entry, snapshot)
        source_uri = parse_result["target_uri"]
        existing = [
            r for r in store.rows("entity_extract")
            if sig.text(r.get("页面来源")) == source_uri
            and (not parse_result["dom_hash"]
                 or sig.text(r.get("解析签名")) == parse_result["dom_hash"])
        ]
        return {
            "ok": True,
            "config_id": entry_id,
            "config_version": parse_result["config_version"],
            "source_uri": source_uri,
            "parse_hash": parse_result["dom_hash"],
            "fields_for_extraction": parse_result["schema_fields"],
            "existing_entity_count": len(existing),
            "snapshot": snapshot,
        }

    def handoff_to_entity_index(self, entry_id: int) -> dict[str, Any]:
        """值班员入口：把解析配置的来源与签名交给实体抽取登记索引。"""
        return self.push_signature(entry_id, origin="值班员下发")

    # ------------------------------------------------------------------ 重放复验

    def _current_config_snapshot(self, config_id: Any, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        if not config_id:
            return None
        row = store.find(self.MODULE, int(config_id))
        if row is None:
            return None
        return self.ensure_snapshot(row)

    def _unverifiable_versions(self, hits: list[dict[str, Any]]) -> list[str]:
        versions: list[str] = []
        for hit in hits:
            version = sig.source_version_of(hit.get("配置ID"), hit.get("配置版本"))
            if not version:
                version = "历史记录/无法补签名"
            if version not in versions:
                versions.append(version)
        return versions

    def replay(self, replay_id: Any) -> dict[str, Any]:
        """按重放清单里的快照复验实际字段，绝不改跑当前选择器。

        证据不足时给出状态“无法复验”，并明确哪个版本/哪条历史记录不能复验；
        部分命中可复验时返回“部分复验”，逐条给出原因。
        """
        item = source_index.find_replay_item(replay_id)
        if item is None:
            return {"ok": False, "reason": "重放清单记录不存在"}

        query = item.get("查询范围") or {}
        snapshot = item.get("配置快照")
        hits = item.get("命中实体") or []
        requested_version = sig.normalize_version(query.get("version"))
        snapshot_version = sig.normalize_version(
            item.get("配置快照版本") or (snapshot or {}).get("配置版本"))

        base = {
            "ok": True,
            "重放ID": item.get("重放ID"),
            "页面URI": item.get("页面URI"),
            "字段": item.get("字段"),
            "解析签名": item.get("解析签名"),
            "查询范围": {
                "页面URI": sig.text(query.get("page_uri")),
                "字段": sig.text(query.get("field")),
                "解析签名": sig.text(query.get("signature")),
                "配置版本": requested_version,
            },
            "配置快照版本": snapshot_version,
            "命中数": len(hits),
            "复验时间": source_index.utc_now(),
        }

        # 没有快照：通常是历史未签名记录，证据不足，不能拿当前选择器顶替。
        if not snapshot or not sig.text(snapshot.get("解析签名")):
            unsigned_label = "历史记录/无法补签名"
            details = []
            for hit in hits:
                details.append({
                    "实体ID": hit.get("实体ID"),
                    "字段": hit.get("字段") or hit.get("关联字段"),
                    "解析签名": hit.get("解析签名", ""),
                    "字段签名": hit.get("字段签名", ""),
                    "结果": "无法复验",
                    "原因": "命中来自无法补签名的历史记录，没有可复验的配置快照",
                })
            result = {
                **base,
                "状态": "无法复验",
                "按快照复验": False,
                "实际读取字段": [],
                "字段核对": details,
                "无法复验版本": [unsigned_label],
                "无法复验明细": [
                    {
                        "版本": unsigned_label,
                        "实体ID": detail["实体ID"],
                        "原因": detail["原因"],
                    }
                    for detail in details
                ],
                "不能复验说明": "重放清单缺少配置快照，无法判定当时的解析签名，不按当前选择器重跑",
            }
            source_index.save_replay_result(item.get("重放ID"), result)
            return result

        # 带版本条件筛选时，快照版本必须落在同一查询范围内。
        if requested_version and snapshot_version and requested_version != snapshot_version:
            blocked_version = sig.source_version_of(item.get("配置ID"), snapshot_version)
            result = {
                **base,
                "状态": "无法复验",
                "按快照复验": False,
                "实际读取字段": [],
                "字段核对": [],
                "无法复验版本": [blocked_version],
                "无法复验明细": [
                    {
                        "版本": blocked_version,
                        "实体ID": hit.get("实体ID", ""),
                        "原因": f"清单快照是 {snapshot_version}，不在查询限定的 {requested_version} 内",
                    }
                    for hit in hits
                ],
                "不能复验说明": (
                    f"查询条件指定版本 {requested_version}，但清单快照为 {snapshot_version}，"
                    "不在同一查询范围内，不做跨版本取数"
                ),
            }
            source_index.save_replay_result(item.get("重放ID"), result)
            return result

        snapshot_fields = list(snapshot.get("字段列表") or [])
        selector = sig.text(snapshot.get("选择器规则"))
        query_field = sig.text(query.get("field"))
        details: list[dict[str, Any]] = []
        verified_fields: list[str] = []
        for hit in hits:
            field = sig.text(hit.get("字段") or hit.get("关联字段"))
            hit_signature = sig.text(hit.get("解析签名"))
            reason = ""
            if query_field and field != query_field:
                reason = f"字段 {field} 不在查询条件限定的字段范围内"
            elif not hit_signature:
                reason = "命中来自无法补签名的历史记录，没有可复验的解析签名"
            elif hit_signature != snapshot.get("解析签名"):
                reason = "命中签名与清单快照签名不一致"
            elif field not in snapshot_fields:
                reason = f"该版本输出 Schema 不含字段 {field}"
            if not reason and not selector:
                reason = "该版本快照没有选择器规则，无法复现解析"
            if reason:
                details.append({
                    "实体ID": hit.get("实体ID"),
                    "字段": field,
                    "解析签名": hit_signature,
                    "字段签名": hit.get("字段签名", ""),
                    "结果": "无法复验",
                    "原因": reason,
                })
                continue
            values = self._field_values(snapshot, [field])
            expected_field_signature = sig.compute_field_signature(
                snapshot.get("解析签名"), field)
            details.append({
                "实体ID": hit.get("实体ID"),
                "字段": field,
                "解析签名": hit_signature,
                "字段签名": hit.get("字段签名", ""),
                "期望字段签名": expected_field_signature,
                "字段签名一致": sig.text(hit.get("字段签名")) in ("", expected_field_signature),
                "实际读取值": values[field],
                "结果": "已复验",
                "原因": "",
            })
            if field not in verified_fields:
                verified_fields.append(field)

        unverifiable_count = sum(1 for row in details if row["结果"] != "已复验")
        if not details or unverifiable_count == len(details):
            status = "无法复验"
        elif unverifiable_count:
            status = "部分复验"
        else:
            status = "已复验"

        current = self._current_config_snapshot(item.get("配置ID"), snapshot)
        current_signature = sig.text((current or {}).get("解析签名"))
        config_corrected = bool(current and current_signature != snapshot.get("解析签名"))
        # 实际读取字段严格限定在查询范围内：查询指定字段时只读该字段。
        actual_fields = [
            field for field in verified_fields
            if (not query_field or field == query_field)
            and field in snapshot_fields
        ]
        result = {
            **base,
            "状态": status,
            "按快照复验": True,
            "快照签名": snapshot.get("解析签名"),
            "快照版本": snapshot_version,
            "当前配置版本": sig.normalize_version((current or {}).get("配置版本")),
            "当前配置签名": current_signature,
            "配置已更正": config_corrected,
            "配置更正说明": (
                f"配置已更正为 {sig.normalize_version((current or {}).get('配置版本'))}，"
                f"重放仍按检索时的快照 {snapshot_version} 取数，没有改跑当前选择器"
                if config_corrected else ""
            ),
            "实际读取字段": actual_fields,
            "字段核对": details,
        }
        if status != "已复验":
            result["无法复验版本"] = self._unverifiable_versions(
                [row for row in hits if any(
                    detail.get("实体ID") == row.get("实体ID")
                    and detail.get("结果") != "已复验" for detail in details)]
            )
            # 逐版本说明为什么不能复验，避免只给一个笼统结论。
            reasons: list[dict[str, str]] = []
            for detail in details:
                if detail.get("结果") == "已复验":
                    continue
                version = "历史记录/无法补签名"
                if sig.text(detail.get("解析签名")):
                    hit = next((row for row in hits
                                if row.get("实体ID") == detail.get("实体ID")), {})
                    version = sig.source_version_of(
                        hit.get("配置ID"), hit.get("配置版本"))
                reasons.append({
                    "版本": version,
                    "实体ID": detail.get("实体ID", ""),
                    "原因": detail.get("原因", ""),
                })
            result["无法复验明细"] = reasons
            result["不能复验说明"] = (
                "部分命中没有落在清单快照的字段范围内" if status == "部分复验"
                else "所有命中都无法在清单快照中复现"
            )
        source_index.save_replay_result(item.get("重放ID"), result)
        return result

    def handoff_replay_to_dom(self, replay_id: Any,
                              *, operator: str = "值班员") -> dict[str, Any]:
        """值班员把实体命中送回 DOM 解析复验的统一入口。"""
        item = source_index.find_replay_item(replay_id)
        if item is None:
            return {"ok": False, "reason": "重放清单记录不存在"}
        item["转送DOM解析"] = True
        item["转送人"] = operator
        item["转送时间"] = source_index.utc_now()
        result = self.replay(item.get("重放ID"))
        result["转送人"] = operator
        return result

    def compute_parse_quality(self, entry_id: int) -> dict[str, Any]:
        """计算解析质量指标，被质量检查模块消费。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": "配置不存在"}
        snapshot = self.ensure_snapshot(entry)
        selectors = (snapshot or {}).get("选择器列表") or []
        uri = sig.text((snapshot or {}).get("页面URI"))
        return {
            "config_id": entry_id,
            "config_version": sig.normalize_version((snapshot or {}).get("配置版本")),
            "selector_validity_ratio": 1.0 if selectors else 0.0,
            "selector_count": len(selectors),
            "uri_format_valid": uri.startswith("http") if uri else False,
            "has_schema": bool(sig.text((snapshot or {}).get("输出Schema"))),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        snapshots = self.sync_config_versions()
        base["parser_quality"] = {
            "total_configs": len(rows),
            "with_selectors": sum(1 for snapshot in snapshots if snapshot.get("选择器规则")),
            "with_schema": sum(1 for snapshot in snapshots if snapshot.get("输出Schema")),
            "valid_uris": sum(1 for snapshot in snapshots
                              if sig.text(snapshot.get("页面URI")).startswith("http")),
            "versions": sum(len(self.iter_snapshots(row)) for row in rows),
        }
        base["entity_source_index"] = source_index.source_index_stats()
        return base
