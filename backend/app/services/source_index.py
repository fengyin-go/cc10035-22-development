"""实体抽取来源数据索引与 DOM 解析重放清单。

这一层解决“实体是从哪一版解析配置来的”：

* 解析配置在登记/更正时产出版本快照和解析签名；
* 实体抽取按“页面 + 字段 + 解析签名”建立来源索引；
* 每次检索都把选中命中的**当时配置范围**写进重放清单，之后配置再被
  别人更正，重放也只能用清单里的快照，不会悄悄改跑当前选择器。

索引和重放清单是领域内部数据，不注册成通用业务模块，避免污染模块列表。
线程锁保证同一配置的旧记录并发提交时只归并到一个来源版本。
"""
from __future__ import annotations

import copy
import hashlib
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from app import domain_signature as sig
from app.store import store


# 同一个签名只允许有一个来源索引版本；并发登记在本进程内串行化。
SOURCE_INDEX_LOCK = threading.RLock()

# 领域内部表。放在模块级而不是 store 里，避免 /api/overview 把它们当成业务模块。
SOURCE_INDEX_ENTRIES: list[dict[str, Any]] = []
REPLAY_ITEMS: list[dict[str, Any]] = []


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def text(value: Any) -> str:
    return str(value or "").strip()


def entity_field(row: dict[str, Any]) -> str:
    return text(row.get("关联字段")) or text(row.get("实体名称"))


def entity_signature(row: dict[str, Any]) -> str:
    return text(row.get("解析签名"))


def _derived_ref(page_uri: str, field: str, signature: str, version: str,
                 config_id: Any) -> dict[str, Any]:
    """没有落库实体时，为索引生成字段级占位引用，仍按签名区分。"""
    return {
        "实体ID": hashlib.md5(f"{page_uri}|{field}|{signature}".encode()).hexdigest()[:8],
        "实体名称": field,
        "实体类型": "属性实体",
        "页面来源": page_uri,
        "关联字段": field,
        "解析签名": signature,
        "配置ID": config_id,
        "配置版本": sig.normalize_version(version),
        "字段签名": sig.compute_field_signature(signature, field),
        "签名状态": "已签名",
        "来源类型": "字段占位",
    }


def build_entity_ref(row: dict[str, Any], *, field: str | None = None,
                     signature: str | None = None,
                     version: str | None = None,
                     signature_state: str | None = None) -> dict[str, Any]:
    """把一条实体记录整理成来源索引里的实体引用。"""
    resolved_field = text(field) or entity_field(row)
    resolved_signature = entity_signature(row) if signature is None else text(signature)
    ref_version = text(row.get("配置版本")) or (text(version) if version else "")
    return {
        "实体ID": text(row.get("实体ID")) or str(row.get("id") or ""),
        "实体名称": row.get("实体名称"),
        "实体类型": row.get("实体类型"),
        "页面来源": row.get("页面来源"),
        "关联字段": resolved_field,
        "解析签名": resolved_signature,
        "配置ID": row.get("配置ID"),
        "配置版本": sig.normalize_version(ref_version),
        "字段签名": sig.compute_field_signature(resolved_signature, resolved_field),
        "签名状态": signature_state or ("已签名" if resolved_signature else "无法补签名"),
    }


def _index_id(page_uri: str, field: str, signature: str) -> str:
    """同一页面/字段/签名只有一条索引，天然具备幂等去重能力。"""
    raw = f"{page_uri}|{field}|{signature or 'unsigned'}"
    return "esrc-" + hashlib.md5(raw.encode()).hexdigest()[:12]


def _new_index_entry(page_uri: str, field: str, signature: str, version: str,
                     config_id: Any, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    version = sig.normalize_version(version) or "未版本化"
    entry = {
        "id": max((int(item.get("id", 0)) for item in SOURCE_INDEX_ENTRIES), default=0) + 1,
        "索引ID": _index_id(page_uri, field, signature),
        "页面URI": page_uri,
        "字段": field,
        "解析签名": signature,
        "字段签名": sig.compute_field_signature(signature, field),
        "配置ID": config_id,
        "配置版本": version,
        "来源版本": sig.source_version_of(config_id, version) if signature
        else "历史记录/无法补签名",
        "来源版本列表": [],
        "版本快照": {},
        "签名状态": "已签名" if signature else "无法补签名",
        "候选来源版本": [],
        "实体批次": [],
        "实体ID列表": [],
        "实体数量": 0,
        "归并次数": 0,
        "索引时间": utc_now(),
        "最近索引时间": utc_now(),
        "最近复验": "",
        "最近复验状态": "",
        "状态": "已索引",
    }
    if signature and snapshot:
        entry["来源版本列表"] = [sig.source_version_of(config_id, version)]
        entry["版本快照"][version] = copy.deepcopy(snapshot)
    SOURCE_INDEX_ENTRIES.append(entry)
    return entry


def _merge_batch(entry: dict[str, Any], refs: Iterable[dict[str, Any]]) -> int:
    """按实体ID归并批次，返回新增数量；重复提交只归并，不新增来源版本。"""
    batch = entry.setdefault("实体批次", [])
    seen = {str(item.get("实体ID")) for item in batch if str(item.get("实体ID"))}
    anonymous_fields = {item.get("关联字段") for item in batch if not item.get("实体ID")}
    added = 0
    for ref in refs:
        entity_id = str(ref.get("实体ID") or "")
        if entity_id:
            if entity_id in seen:
                continue
            seen.add(entity_id)
        elif ref.get("关联字段") in anonymous_fields:
            continue
        else:
            anonymous_fields.add(ref.get("关联字段"))
        batch.append(dict(ref))
        added += 1
    entry["实体ID列表"] = [item.get("实体ID") for item in batch if item.get("实体ID")]
    entry["实体数量"] = len(batch)
    if added:
        entry["归并次数"] = int(entry.get("归并次数", 0)) + 1
    return added


def _find_index_entry(page_uri: str, field: str, signature: str) -> dict[str, Any] | None:
    wanted = _index_id(page_uri, field, signature)
    for entry in SOURCE_INDEX_ENTRIES:
        if entry.get("索引ID") == wanted:
            return entry
    return None


def _detach_from_unsigned(page_uri: str, field: str, entity_id: Any) -> None:
    """补签成功后，把实体从“无法补签名”桶里摘掉，避免一条实体挂两个出处。"""
    entry = _find_index_entry(page_uri, field, "")
    if entry is None:
        return
    wanted = str(entity_id or "")
    entry["实体批次"] = [
        item for item in entry.get("实体批次", [])
        if str(item.get("实体ID") or "") != wanted
    ]
    entry["实体ID列表"] = [
        item.get("实体ID") for item in entry["实体批次"] if item.get("实体ID")
    ]
    entry["实体数量"] = len(entry["实体批次"])
    if not entry["实体批次"]:
        SOURCE_INDEX_ENTRIES[:] = [
            item for item in SOURCE_INDEX_ENTRIES
            if item.get("索引ID") != entry.get("索引ID")
        ]


def register_snapshot(snapshot: dict[str, Any],
                      entities_by_field: dict[str, list[dict[str, Any]]] | None = None,
                      *, origin: str = "配置下发",
                      refresh_entities: bool = True) -> dict[str, Any]:
    """把一版解析配置的来源和签名登记为实体抽取来源索引。

    相同页面/字段/签名只会存在一个来源版本；并发提交由 SOURCE_INDEX_LOCK 串行化，
    后到的记录只做实体批次归并，不会分裂出第二个来源版本。
    """
    signature = text(snapshot.get("解析签名"))
    page_uri = text(snapshot.get("页面URI"))
    fields = snapshot.get("字段列表") or sig.split_fields(snapshot.get("输出Schema"))
    config_id = snapshot.get("配置ID")
    version = sig.normalize_version(snapshot.get("配置版本")) or "未版本化"
    if not signature:
        return {"ok": False, "reason": "该配置无法形成解析签名，索引记为无法补签名",
                "signature": "", "entries": [], "entry_count": 0, "created_count": 0}

    results: list[dict[str, Any]] = []
    created_count = 0
    with SOURCE_INDEX_LOCK:
        for field in fields:
            entry = _find_index_entry(page_uri, field, signature)
            if entry is None:
                entry = _new_index_entry(page_uri, field, signature, version, config_id, snapshot)
                created_count += 1
            candidate = sig.source_version_of(config_id, version)
            versions = entry.setdefault("来源版本列表", [])
            if candidate and candidate not in versions:
                versions.append(candidate)
            # 同一签名被另一个配置版本登记时，保留首个来源版本，其余记为候选。
            if candidate and candidate != entry.get("来源版本"):
                candidates = entry.setdefault("候选来源版本", [])
                if candidate not in candidates:
                    candidates.append(candidate)
            entry.setdefault("版本快照", {})[version] = copy.deepcopy(snapshot)
            refs = list((entities_by_field or {}).get(field, []))
            if refresh_entities and not refs:
                # 只认签名完全一致的实体，绝不把旧签名/无签名的记录挂到新签名下。
                refs = [
                    build_entity_ref(row, field=field, signature=signature,
                                     version=version, signature_state="已签名")
                    for row in store.rows("entity_extract")
                    if text(row.get("页面来源")) == page_uri
                    and entity_field(row) == field
                    and entity_signature(row) == signature
                ]
            if not refs:
                # 页面上还没有实体落库时，先用字段级占位，保证索引可检索、可复验。
                refs = [_derived_ref(page_uri, field, signature, version, config_id)]
            _merge_batch(entry, refs)
            entry["最近索引时间"] = utc_now()
            entry["索引来源"] = origin
            results.append(copy.deepcopy(entry))
    return {
        "ok": True,
        "signature": signature,
        "config_version": version,
        "indexed_fields": fields,
        "entries": results,
        "entry_count": len(results),
        "created_count": created_count,
    }


def _signature_candidates(page_uri: str, field: str) -> list[dict[str, Any]]:
    """按页面/字段找可补签的配置版本快照。"""
    from app.services.dom_parser import DomParserService  # 延迟导入避免循环

    return DomParserService().snapshots_for_field(page_uri, field)


def register_unsigned_entities(page_uri: str | None = None,
                               field: str | None = None) -> list[dict[str, Any]]:
    """兼容历史未补签名的实体记录。

    * 页面/字段能唯一对应到一个配置版本时，补签名并归入该版本索引；
    * 对应不到或对应到多个版本时，保留为“无法补签名”，不猜测当前配置。
    """
    results: list[dict[str, Any]] = []
    wanted_field = text(field)
    with SOURCE_INDEX_LOCK:
        for row in list(store.rows("entity_extract")):
            page = text(row.get("页面来源"))
            if not page or (page_uri and page != text(page_uri)):
                continue
            if entity_signature(row):
                continue
            row_field = entity_field(row)
            if not row_field or (wanted_field and row_field != wanted_field):
                continue
            candidates = _signature_candidates(page, row_field)
            signatures = sorted({text(item.get("解析签名")) for item in candidates
                                 if text(item.get("解析签名"))})
            if len(signatures) == 1:
                snapshot = next(item for item in candidates
                                if text(item.get("解析签名")) == signatures[0])
                version = text(snapshot.get("配置版本"))
                entry = _find_index_entry(page, row_field, signatures[0])
                if entry is None:
                    entry = _new_index_entry(page, row_field, signatures[0], version,
                                             snapshot.get("配置ID"), snapshot)
                # 补签成功说明这条历史记录确实属于该版本，字段占位已被真实实体取代。
                _merge_batch(entry, [
                    build_entity_ref(row, field=row_field, signature=signatures[0],
                                     version=version, signature_state="已补签")
                ])
                entry["实体批次"] = [
                    item for item in entry.get("实体批次", [])
                    if item.get("来源类型") != "字段占位"
                ]
                entry["实体ID列表"] = [
                    item.get("实体ID") for item in entry["实体批次"] if item.get("实体ID")
                ]
                entry["实体数量"] = len(entry["实体批次"])
                entry.pop("签名状态", None)
                _detach_from_unsigned(page, row_field, row.get("实体ID") or row.get("id"))
                entry["补签名来源"] = sig.source_version_of(
                    snapshot.get("配置ID"), version)
                results.append(copy.deepcopy(entry))
            else:
                entry = _find_index_entry(page, row_field, "")
                if entry is None:
                    entry = _new_index_entry(page, row_field, "", "", None, None)
                _merge_batch(entry, [
                    build_entity_ref(row, field=row_field, signature="",
                                     signature_state="无法补签名")
                ])
                entry["无法补签名原因"] = (
                    "同页同字段对应多个解析签名，无法判定出处"
                    if signatures else "同页没有包含该字段的解析配置"
                )
                results.append(copy.deepcopy(entry))
    return results


def register_entity_entry(row: dict[str, Any]) -> dict[str, Any] | None:
    """新建一条实体时，把它挂到来源索引上（签名为空走历史补签逻辑）。"""
    page = text(row.get("页面来源"))
    field = entity_field(row)
    if not page or not field:
        return None
    signature = entity_signature(row)
    with SOURCE_INDEX_LOCK:
        if signature:
            from app.services.dom_parser import DomParserService
            found = DomParserService().resolve_snapshot(signature=signature, page_uri=page)
            snapshot = found["snapshot"] if found.get("ok") else None
            version = sig.normalize_version(row.get("配置版本")) or (
                text(found["snapshot"].get("配置版本")) if found.get("ok") else "")
            entry = _find_index_entry(page, field, signature)
            if entry is None:
                entry = _new_index_entry(
                    page, field, signature, version,
                    row.get("配置ID") or (snapshot or {}).get("配置ID"), snapshot)
            _merge_batch(entry, [
                build_entity_ref(row, field=field, signature=signature,
                                 version=version, signature_state="已签名")
            ])
            return copy.deepcopy(entry)
        results = register_unsigned_entities(page_uri=page, field=field)
    for entry in results:
        if entry.get("字段") == field and entry.get("页面URI") == page:
            return entry
    return None


def index_page(page_uri: str) -> dict[str, Any]:
    """把某页所有配置版本的快照和历史未签名实体补进索引（幂等）。"""
    from app.services.dom_parser import DomParserService

    dps = DomParserService()
    registered: list[dict[str, Any]] = []
    for row in store.rows("dom_parser"):
        if text(row.get("目标URI")) != text(page_uri):
            continue
        for snapshot in dps.iter_snapshots(row):
            if snapshot.get("解析签名"):
                outcome = register_snapshot(snapshot, origin="检索前自动索引")
                registered.extend(outcome.get("entries", []))
    registered.extend(register_unsigned_entities(page_uri=page_uri))
    return {"ok": True, "page_uri": page_uri, "entries": registered,
            "entry_count": len(registered)}


def _matches_version(entry: dict[str, Any], version: str) -> bool:
    if not version:
        return True
    if version in (entry.get("来源版本列表") or []):
        return True
    return version == text(entry.get("配置版本"))


def search_entries(
    *,
    page_uri: str | None = None,
    field: str | None = None,
    signature: str | None = None,
    version: str | None = None,
    entity_id: str | None = None,
    include_unsigned: bool = False,
    auto_index: bool = True,
) -> list[dict[str, Any]]:
    """按页面、字段、解析签名（以及可选的配置版本）定位索引条目。"""
    page = text(page_uri)
    if auto_index and page:
        # 每次检索前补齐该页的全部配置版本：检索既能命中新签名，也能命中旧签名。
        index_page(page)

    with SOURCE_INDEX_LOCK:
        entries = list(SOURCE_INDEX_ENTRIES)

    result: list[dict[str, Any]] = []
    wanted_field = text(field)
    wanted_signature = text(signature)
    wanted_version = sig.normalize_version(version)
    wanted_entity = text(entity_id)
    for entry in entries:
        if page and entry.get("页面URI") != page:
            continue
        if wanted_field and entry.get("字段") != wanted_field:
            continue
        if wanted_signature and entry.get("解析签名") != wanted_signature:
            continue
        if wanted_version and not _matches_version(entry, wanted_version):
            continue
        if not wanted_signature and not text(entry.get("解析签名")) and not include_unsigned:
            continue
        if wanted_entity and wanted_entity not in {str(item) for item in entry.get("实体ID列表", [])}:
            continue
        result.append(copy.deepcopy(entry))
    return result


def flatten_hits(entries: Iterable[dict[str, Any]],
                 *, requested_version: str | None = None) -> list[dict[str, Any]]:
    """把索引批次展开成一条条可回写重放清单的实体命中。"""
    hits: list[dict[str, Any]] = []
    wanted_version = sig.normalize_version(requested_version)
    for entry in entries:
        signature = text(entry.get("解析签名"))
        version = text(entry.get("配置版本"))
        if wanted_version and _matches_version(entry, wanted_version) and version != wanted_version:
            # 版本条件命中候选版本时，按查询版本标注命中，保证重放取数范围一致。
            version = wanted_version
        base = {
            "页面URI": entry.get("页面URI"),
            "字段": entry.get("字段"),
            "解析签名": signature,
            "配置ID": entry.get("配置ID"),
            "配置版本": version,
            "来源版本": entry.get("来源版本"),
            "来源版本列表": list(entry.get("来源版本列表") or []),
            "索引ID": entry.get("索引ID"),
            "字段签名": entry.get("字段签名")
            or sig.compute_field_signature(signature, entry.get("字段")),
            "签名状态": entry.get("签名状态"),
        }
        batch = entry.get("实体批次") or []
        if not batch:
            hit = dict(base)
            hit.update({"实体ID": "", "实体名称": "", "实体类型": ""})
            hits.append(hit)
            continue
        for ref in batch:
            hit = dict(base)
            hit.update({
                "实体ID": ref.get("实体ID", ""),
                "实体名称": ref.get("实体名称"),
                "实体类型": ref.get("实体类型"),
                "关联字段": ref.get("关联字段") or entry.get("字段"),
                "解析签名": ref.get("解析签名") or signature,
                "配置ID": ref.get("配置ID") if ref.get("配置ID") is not None
                else entry.get("配置ID"),
                "配置版本": ref.get("配置版本") or version,
                "字段签名": ref.get("字段签名")
                or sig.compute_field_signature(signature, entry.get("字段")),
                "签名状态": ref.get("签名状态") or entry.get("签名状态"),
            })
            hits.append(hit)
    return hits


def pick_snapshot(entries: Iterable[dict[str, Any]], version: str | None = None
                  ) -> tuple[dict[str, Any] | None, str]:
    """从索引条目里取选中时的配置快照，优先匹配指定版本。"""
    wanted = sig.normalize_version(version)
    snapshots: list[tuple[str, dict[str, Any]]] = []
    for entry in entries:
        for snap_version, snapshot in (entry.get("版本快照") or {}).items():
            if snapshot:
                snapshots.append((str(snap_version), snapshot))
    if not snapshots:
        return None, ""
    if wanted:
        for snap_version, snapshot in snapshots:
            if snap_version == wanted:
                return copy.deepcopy(snapshot), snap_version
    return copy.deepcopy(snapshots[0][1]), snapshots[0][0]


def group_by_source(entries: Iterable[dict[str, Any]]
                    ) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """按解析签名（及命中的版本）分组，一组只能有一个快照、一份清单。"""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in entries:
        signature = text(entry.get("解析签名"))
        snapshots = entry.get("版本快照") or {}
        version = text(entry.get("配置版本"))
        if version not in snapshots and snapshots:
            version = next(iter(snapshots))
        key = (signature, version if signature else "")
        groups.setdefault(key, []).append(entry)
    return [(signature, version, group) for (signature, version), group in groups.items()]


def create_replay_item(
    *,
    query: dict[str, Any],
    entries: list[dict[str, Any]],
    hits: list[dict[str, Any]],
    snapshot: dict[str, Any] | None = None,
    snapshot_version: str = "",
    source_replay_id: str | None = None,
    submit_note: str = "",
) -> dict[str, Any]:
    """把一次检索选中的命中回写到重放清单，同时固化当时的配置快照。"""
    with SOURCE_INDEX_LOCK:
        next_id = max((int(row.get("id", 0)) for row in REPLAY_ITEMS), default=0) + 1
        item = {
            "id": next_id,
            "重放ID": f"rpl-{next_id}",
            "页面URI": text(query.get("page_uri")) or (hits[0].get("页面URI") if hits else ""),
            "字段": text(query.get("field")),
            "解析签名": text(query.get("signature")) or (hits[0].get("解析签名") if hits else ""),
            "配置版本": sig.normalize_version(query.get("version")) or snapshot_version
            or (hits[0].get("配置版本") if hits else ""),
            "配置ID": (snapshot or {}).get("配置ID")
            or (hits[0].get("配置ID") if hits else None),
            "查询范围": copy.deepcopy(query),
            "索引条目": copy.deepcopy(entries),
            "命中实体": copy.deepcopy(hits),
            "命中数": len(hits),
            "配置快照": copy.deepcopy(snapshot),
            "配置快照版本": snapshot_version,
            "来源": "命中转送" if source_replay_id else "检索回写",
            "来源重放ID": source_replay_id or "",
            "转送说明": submit_note,
            "状态": "待重放",
            "创建时间": utc_now(),
            "最近检索时间": utc_now(),
            "复验": None,
        }
        REPLAY_ITEMS.append(item)
        return copy.deepcopy(item)


def list_replay_items(*, status: str | None = None,
                      signature: str | None = None,
                      page_uri: str | None = None,
                      version: str | None = None,
                      limit: int = 200) -> list[dict[str, Any]]:
    with SOURCE_INDEX_LOCK:
        rows = list(REPLAY_ITEMS)
    wanted_version = sig.normalize_version(version)
    result = []
    for item in rows:
        if status and item.get("状态") != status:
            continue
        if signature and item.get("解析签名") != text(signature):
            continue
        if page_uri and item.get("页面URI") != text(page_uri):
            continue
        if wanted_version and item.get("配置版本") != wanted_version:
            continue
        result.append(copy.deepcopy(item))
    return result[:limit]


def find_replay_item(replay_id: Any) -> dict[str, Any] | None:
    wanted = text(replay_id)
    with SOURCE_INDEX_LOCK:
        for item in REPLAY_ITEMS:
            if wanted in (str(item.get("重放ID")), str(item.get("id"))):
                return item
    return None


def save_replay_result(replay_id: Any, result: dict[str, Any]) -> dict[str, Any] | None:
    item = find_replay_item(replay_id)
    if item is None:
        return None
    with SOURCE_INDEX_LOCK:
        item["状态"] = result.get("状态", item.get("状态"))
        item["复验"] = copy.deepcopy(result)
        item["复验时间"] = result.get("复验时间", utc_now())
        for hit in item.get("命中实体", []):
            for entry in SOURCE_INDEX_ENTRIES:
                if entry.get("索引ID") == hit.get("索引ID"):
                    entry["最近复验"] = item["复验时间"]
                    entry["最近复验状态"] = item["状态"]
        return copy.deepcopy(item)


def unsigned_index_entries(page_uri: str | None = None,
                           field: str | None = None) -> list[dict[str, Any]]:
    if page_uri:
        register_unsigned_entities(page_uri=page_uri, field=field)
    with SOURCE_INDEX_LOCK:
        rows = [copy.deepcopy(item) for item in SOURCE_INDEX_ENTRIES]
    result = []
    for item in rows:
        if text(item.get("解析签名")):
            continue
        if page_uri and item.get("页面URI") != text(page_uri):
            continue
        if field and item.get("字段") != text(field):
            continue
        result.append(item)
    return result


def source_index_stats() -> dict[str, Any]:
    with SOURCE_INDEX_LOCK:
        entries = list(SOURCE_INDEX_ENTRIES)
        replays = list(REPLAY_ITEMS)
    return {
        "index_entries": len(entries),
        "signed_entries": sum(1 for item in entries if text(item.get("解析签名"))),
        "unsigned_entries": sum(1 for item in entries if not text(item.get("解析签名"))),
        "entity_refs": sum(int(item.get("实体数量", 0)) for item in entries),
        "replay_items": len(replays),
        "pending_replays": sum(1 for item in replays if item.get("状态") == "待重放"),
        "verified_replays": sum(1 for item in replays if item.get("状态") == "已复验"),
        "partial_replays": sum(1 for item in replays if item.get("状态") == "部分复验"),
        "unverifiable_replays": sum(1 for item in replays if item.get("状态") == "无法复验"),
    }


def reset_domain_state() -> None:
    """测试辅助：清空索引与重放清单。"""
    with SOURCE_INDEX_LOCK:
        SOURCE_INDEX_ENTRIES.clear()
        REPLAY_ITEMS.clear()
