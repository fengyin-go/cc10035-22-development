"""实体来源索引与重放清单的领域测试。

只用标准库 unittest，不依赖 httpx/pytest：
    cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import threading
import unittest

from app import domain_signature as sig
from app.seed import SEED_ROWS
from app.services import source_index
from app.services.dom_parser import DomParserService
from app.services.entity_extract import EntityExtractService
from app.store import store

SHOP = "https://shop.example.com/item/1001"
NEWS = "https://news.example.com/a/20261119"
OLD_SIGNATURE = "660c5b358cba"      # 配置 1 的 v1 选择器 .product-title
NEW_SIGNATURE = "08d37b7a78d1"      # 配置 1 的 v2 选择器 .product-title-v2, .product-name


def reset_world() -> None:
    store._tables = {name: [copy.deepcopy(row) for row in rows]
                     for name, rows in SEED_ROWS.items()}
    source_index.reset_domain_state()
    DomParserService().push_all_signatures(origin="测试准备")
    source_index.register_unsigned_entities()


class SourceIndexTestCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_world()
        self.dom = DomParserService()
        self.entity = EntityExtractService()

    # ---------------------------------------------------------------- 索引

    def test_index_locates_entities_by_page_field_and_signature(self) -> None:
        result = self.entity.search_source_index(
            page_uri=SHOP, field="title", signature=OLD_SIGNATURE)
        self.assertEqual(result["total"], 1)
        hit = result["hits"][0]
        self.assertEqual(hit["实体ID"], "7ae2fa62")
        self.assertEqual(hit["配置版本"], "v1")
        self.assertEqual(hit["签名状态"], "已签名")

    def test_same_page_two_configs_stay_separate(self) -> None:
        # 同页配置 1（title/brand）跨 v1/v2 两个签名；配置 2（price/currency）自成一版。
        titles = self.entity.search_source_index(page_uri=SHOP, field="title")
        prices = self.entity.search_source_index(page_uri=SHOP, field="price")
        self.assertEqual({hit["解析签名"] for hit in titles["hits"]},
                         {OLD_SIGNATURE, NEW_SIGNATURE})
        self.assertEqual({hit["字段"] for hit in prices["hits"]}, {"price"})
        self.assertEqual({hit["解析签名"] for hit in prices["hits"]}, {"6767adc5390c"})
        self.assertNotIn("title", {hit["字段"] for hit in prices["hits"]})

    def test_version_filter_limits_fields_to_same_version(self) -> None:
        v1 = self.entity.search_source_index(page_uri=SHOP, version="v1")
        v2 = self.entity.search_source_index(page_uri=SHOP, version="v2")
        self.assertEqual(sorted({hit["字段"] for hit in v1["hits"]}),
                         ["brand", "currency", "price", "title"])
        self.assertEqual(sorted({hit["字段"] for hit in v2["hits"]}), ["brand", "title"])
        self.assertEqual({hit["配置版本"] for hit in v2["hits"]}, {"v2"})

    # ---------------------------------------------------------------- 回写

    def test_selected_hits_writeback_freezes_snapshot(self) -> None:
        written = self.entity.writeback_hits(
            query={"page_uri": SHOP, "signature": OLD_SIGNATURE}, entity_ids=["7ae2fa62"])
        self.assertTrue(written["ok"])
        self.assertEqual(len(written["replays"]), 1)
        replay = written["replays"][0]
        self.assertEqual(replay["命中数"], 1)
        self.assertEqual(replay["配置快照"]["选择器规则"], ".product-title")
        self.assertEqual(replay["配置快照版本"], "v1")

    def test_writeback_rejects_hits_outside_query_scope(self) -> None:
        written = self.entity.writeback_hits(
            query={"page_uri": SHOP, "field": "price"}, entity_ids=["7ae2fa62"])
        self.assertFalse(written["ok"])

    # ---------------------------------------------------------------- 重放

    def test_replay_uses_snapshot_after_another_operator_corrects_config(self) -> None:
        written = self.entity.writeback_hits(
            query={"page_uri": SHOP, "signature": OLD_SIGNATURE}, entity_ids=["7ae2fa62"])
        replay_id = written["replays"][0]["重放ID"]

        revised = self.dom.revise_config(1, {"选择器规则": ".product-title-v3"},
                                        operator="另一操作员")
        self.assertEqual(revised["previous_version"], "v2")
        self.assertEqual(revised["current_version"], "v3")
        self.assertNotEqual(revised["current_signature"], OLD_SIGNATURE)

        result = self.dom.handoff_replay_to_dom(replay_id)
        self.assertEqual(result["状态"], "已复验")
        self.assertEqual(result["实际读取字段"], ["title"])
        self.assertTrue(result["配置已更正"])
        self.assertEqual(result["配置快照版本"], "v1")
        self.assertEqual(result["快照签名"], OLD_SIGNATURE)
        # 实际读取值由快照选择器决定，而不是更正后的当前选择器。
        expected = self.dom._field_values(written["replays"][0]["配置快照"], ["title"])
        self.assertEqual(result["字段核对"][0]["实际读取值"], expected["title"])

    def test_replay_without_snapshot_reports_unverifiable_version(self) -> None:
        unsigned = self.entity.search_source_index(
            page_uri=NEWS, include_unsigned=True)
        hit = next(item for item in unsigned["hits"]
                   if item["实体ID"] == "88880002")
        self.assertEqual(hit["签名状态"], "无法补签名")
        written = self.entity.writeback_hits(
            query={"page_uri": NEWS, "include_unsigned": True},
            entity_ids=["88880002"])
        self.assertTrue(written["ok"])
        result = self.dom.replay(written["replays"][0]["重放ID"])
        self.assertEqual(result["状态"], "无法复验")
        self.assertEqual(result["实际读取字段"], [])
        self.assertIn("历史记录/无法补签名", result["无法复验版本"])
        self.assertTrue(result["无法复验明细"])

    def test_partial_replay_lists_each_unverifiable_version(self) -> None:
        written = self.entity.search_and_writeback(
            page_uri=NEWS, include_unsigned=True)
        statuses = []
        for replay in written["replays"]:
            result = self.dom.replay(replay["重放ID"])
            statuses.append((replay["解析签名"], result["状态"]))
        self.assertIn(("95e7f6ec0da1", "已复验"), statuses)
        self.assertIn(("0b44c9033881", "已复验"), statuses)
        self.assertIn(("", "无法复验"), statuses)

    def test_version_mismatch_is_not_replayed_across_versions(self) -> None:
        written = self.entity.writeback_hits(
            query={"page_uri": SHOP, "signature": OLD_SIGNATURE}, entity_ids=["7ae2fa62"])
        replay_id = written["replays"][0]["重放ID"]
        item = source_index.find_replay_item(replay_id)
        item["查询范围"]["version"] = "v2"
        result = self.dom.replay(replay_id)
        self.assertEqual(result["状态"], "无法复验")
        self.assertEqual(result["实际读取字段"], [])
        self.assertIn("不在同一查询范围内", result["不能复验说明"])

    def test_field_scoped_replay_only_reads_query_field(self) -> None:
        written = self.entity.writeback_hits(
            query={"page_uri": SHOP, "signature": OLD_SIGNATURE, "field": "brand"},
            entity_ids=["7ae2fa62", "bb6ecfc1"])
        self.assertEqual(written["replays"][0]["命中数"], 1)
        result = self.dom.replay(written["replays"][0]["重放ID"])
        self.assertEqual(result["实际读取字段"], ["brand"])
        self.assertTrue(all(row["字段"] == "brand" for row in result["字段核对"]))

    # ---------------------------------------------------------------- 并发与历史

    def test_concurrent_push_keeps_single_source_version(self) -> None:
        signature = OLD_SIGNATURE
        before = source_index.search_entries(
            page_uri=SHOP, signature=signature, auto_index=False)

        def push() -> None:
            self.dom.push_signature(1, origin="并发提交")

        threads = [threading.Thread(target=push) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        after = source_index.search_entries(
            page_uri=SHOP, signature=signature, auto_index=False)
        self.assertEqual(len(after), len(before))
        for entry in after:
            self.assertEqual(entry["来源版本列表"], ["config:1@v1"])
            self.assertEqual(entry["候选来源版本"], [])
            self.assertEqual(entry["来源版本"], "config:1@v1")

    def test_unsigned_records_backfill_only_when_version_is_unique(self) -> None:
        entries = source_index.unsigned_index_entries(page_uri=NEWS)
        self.assertEqual([entry["字段"] for entry in entries], ["headline"])
        self.assertIn("多个解析签名", entries[0]["无法补签名原因"])
        signed = self.entity.search_source_index(page_uri=NEWS, field="author")
        self.assertEqual(signed["hits"][0]["签名状态"], "已补签")

    def test_new_entity_registration_joins_matching_signature(self) -> None:
        self.entity.create_entry({
            "实体ID": "new-brand",
            "实体类型": "属性实体",
            "实体名称": "brand",
            "页面来源": SHOP,
            "解析签名": OLD_SIGNATURE,
        })
        result = self.entity.search_source_index(
            page_uri=SHOP, field="brand", signature=OLD_SIGNATURE)
        self.assertIn("new-brand", [hit["实体ID"] for hit in result["hits"]])


class SignatureTestCase(unittest.TestCase):
    def test_signature_changes_with_selector(self) -> None:
        base = sig.compute_parse_signature(SHOP, "beautifulsoup", ".product-title", "title, brand")
        changed = sig.compute_parse_signature(SHOP, "beautifulsoup", ".product-title-v2", "title, brand")
        self.assertNotEqual(base, changed)
        self.assertEqual(len(base), 12)

    def test_signature_requires_all_parts(self) -> None:
        self.assertEqual(sig.compute_parse_signature(SHOP, "lxml", "", "title"), "")
        self.assertEqual(sig.compute_field_signature("", "title"), "")

    def test_version_normalization(self) -> None:
        self.assertEqual(sig.normalize_version("2"), "v2")
        self.assertEqual(sig.normalize_version("config:3@v1"), "v1")
        self.assertEqual(sig.source_version_of(3, "v1"), "config:3@v1")


if __name__ == "__main__":
    unittest.main()
