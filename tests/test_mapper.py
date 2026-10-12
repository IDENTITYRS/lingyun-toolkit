"""图片匹配回归：模拟宿主按实体 ID 展开所有历史图片的行为。"""

import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lingyun_features import mapper


def entity(kind, eid, name, primary, *history):
    return {
        kind + "_id": eid, "name": name, "image_path": primary,
        "reference_items": [
            {"path": path, "media_type": "image"} for path in (primary, *history)
        ],
        "extra_images": list(history),
    }


class Host:
    def __init__(self):
        self.calls = []
        self.panels = [{"unique_name": "p1", "panel_index": 1,
                        "prompt": "罗秀英与林秋禾在缝纫车间使用缝纫机。"}]
        self.entities = {
            "character": [entity("character", "c1", "罗秀英", "C:/refs/new.png", "C:/refs/old.png"),
                          entity("character", "c2", "林秋禾", "C:/refs/lin.png")],
            "location": [entity("location", "l1", "缝纫车间", "C:/refs/room.png", "C:/refs/room-old.png")],
            "item": [entity("item", "i1", "缝纫机", "C:/refs/machine.png", "C:/refs/machine-old.png")],
        }
        self.references = {}
        self.selections = {}
        self.extra_references = []
        self.read_error = False
        self.save_error = False
        self.saved_count = None
        self.entity_error = False

    async def call_tool(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name == "zzdh_get_entity_list":
            if self.entity_error:
                return {"success": False, "error": "读取资产失败"}
            return {"entities": copy.deepcopy(self.entities[args["entity_type"]])}
        if name == "zzdh_get_panels":
            return {"panels": copy.deepcopy(self.panels)}
        uid = args["unique_name"]
        if name == "zzdh_set_panel_entity_selections":
            self.selections[uid] = copy.deepcopy(args)
            refs = []
            for kind in mapper.ENTITY_KINDS:
                for asset in self.entities[kind]:
                    if asset[kind + "_id"] in args[kind + "_ids"]:
                        refs.extend(copy.deepcopy(asset["reference_items"]))
            self.references[uid] = refs + copy.deepcopy(self.extra_references)
            return {"success": True}
        if name == "zzdh_get_edit_view_data":
            if self.read_error:
                return {"unique_name": uid}  # 无参考项字段不可当成空列表覆盖。
            return {"unique_name": uid, "reference_images": copy.deepcopy(self.references[uid])}
        if name == "zzdh_save_reference_items":
            if self.save_error:
                return {"success": False, "error": "保存失败"}
            self.references[uid] = copy.deepcopy(args["items"])
            return {"success": True, "saved": self.saved_count if self.saved_count is not None else len(args["items"])}
        raise AssertionError(name)


class MapperTests(unittest.TestCase):
    def run_mapper(self, host, **args):
        return asyncio.run(mapper.execute({"tools": host, "arguments": args}))

    def test_regenerated_images_keep_one_current_image_per_asset(self):
        host = Host()
        result = self.run_mapper(host)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([r["path"] for r in host.references["p1"]], [
            "C:/refs/new.png", "C:/refs/lin.png", "C:/refs/room.png", "C:/refs/machine.png"])
        self.assertEqual(result["success"][0]["removed_history_count"], 3)
        # 历史候选仍留在资产库，清理只影响分镜参考项。
        self.assertEqual(len(host.entities["character"][0]["reference_items"]), 2)

    def test_repeated_matching_and_switching_back_to_old_main_image(self):
        host = Host()
        self.run_mapper(host)
        self.run_mapper(host)
        self.assertEqual(len(host.references["p1"]), 4)
        asset = host.entities["character"][0]
        asset["image_path"] = "C:/refs/old.png"
        # 即使列表顺序未刷新，也以宿主明确提供的主图为准。
        self.run_mapper(host)
        paths = [r["path"] for r in host.references["p1"]]
        self.assertIn("C:/refs/old.png", paths)
        self.assertNotIn("C:/refs/new.png", paths)

    def test_other_references_and_stable_ids_survive_cleanup(self):
        host = Host()
        host.extra_references = [
            {"path": "C:/refs/voice.wav", "media_type": "audio", "reference_id": "voice-1"},
            {"path": "C:/refs/clip.mp4", "media_type": "video", "reference_id": "video-1"},
            {"path": "C:/refs/manual.png", "media_type": "image", "reference_id": "manual-1"},
        ]
        self.run_mapper(host)
        self.assertEqual(host.references["p1"][-3:], host.extra_references)

    def test_history_used_as_another_matched_assets_primary_is_kept(self):
        host = Host()
        host.entities["character"][1] = entity("character", "c2", "林秋禾", "C:/refs/old.png")
        result = self.run_mapper(host)
        self.assertEqual(result["status"], "ok")
        self.assertIn("C:/refs/old.png", [r["path"] for r in host.references["p1"]])

    def test_windows_path_normalization(self):
        host = Host()
        host.extra_references = [{"path": "c:\\REFS\\old.png", "media_type": "image"}]
        result = self.run_mapper(host)
        self.assertEqual(result["success"][0]["removed_history_count"], 4)
        self.assertNotIn(host.extra_references[0], host.references["p1"])

    def test_primary_fallback_skips_audio_and_uses_first_image(self):
        host = Host()
        asset = host.entities["character"][0]
        del asset["image_path"]
        asset["reference_items"].insert(0, {"path": "C:/refs/voice.wav", "media_type": "audio"})
        self.run_mapper(host)
        self.assertIn("C:/refs/new.png", [r["path"] for r in host.references["p1"]])
        self.assertNotIn("C:/refs/old.png", [r["path"] for r in host.references["p1"]])

    def test_single_image_does_not_need_extra_read_or_save(self):
        host = Host()
        host.panels[0]["prompt"] = "林秋禾"
        result = self.run_mapper(host)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(any(n in {"zzdh_get_edit_view_data", "zzdh_save_reference_items"} for n, _ in host.calls))

    def test_no_match_clears_all_entity_categories(self):
        host = Host()
        host.panels[0]["prompt"] = "空镜头"
        result = self.run_mapper(host)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(host.references["p1"], [])
        self.assertEqual(host.selections["p1"], {"unique_name": "p1", "character_ids": [], "location_ids": [], "item_ids": []})

    def test_specified_lines_quotes_and_processed_context(self):
        host = Host()
        host.panels[0].update(prompt="人物：罗秀英\n对白：“林秋禾”。\n背景：缝纫车间与缝纫机",
                              processed_prompt="人物：林秋禾")
        result = self.run_mapper(host, match_mode="specified", match_keywords="人物、对白")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(host.references["p1"], [{"path": "C:/refs/new.png", "media_type": "image"}])

    def test_read_failure_never_saves_empty_list(self):
        host = Host()
        host.read_error = True
        result = self.run_mapper(host)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["success_count"], 0)
        self.assertFalse(any(n == "zzdh_save_reference_items" for n, _ in host.calls))

    def test_save_failure_or_dropped_items_cannot_report_success(self):
        for mode in ("save_error", "saved_count"):
            with self.subTest(mode=mode):
                host = Host()
                setattr(host, mode, True if mode == "save_error" else 1)
                result = self.run_mapper(host)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["success_count"], 0)

    def test_entity_read_failure_does_not_clear_selections(self):
        host = Host()
        host.entity_error = True
        with self.assertRaisesRegex(RuntimeError, "读取资产失败"):
            self.run_mapper(host)
        self.assertFalse(host.selections)

    def test_range_does_not_change_other_panels(self):
        host = Host()
        host.panels.append({"unique_name": "p2", "panel_index": 2, "prompt": "罗秀英"})
        host.references["p1"] = [{"path": "untouched.png", "media_type": "image"}]
        result = self.run_mapper(host, start_index=2, end_index=2)
        self.assertEqual(result["target_count"], 1)
        self.assertEqual(host.references["p1"][0]["path"], "untouched.png")

    def test_original_self_test(self):
        result = asyncio.run(mapper.self_test({}))
        self.assertTrue(result["ok"], result)

    def test_manifest_declares_new_reference_tools(self):
        meta = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        cap = next(c for c in meta["declared_capabilities"] if c["id"] == "match_prompt_reference_images")
        self.assertTrue({"zzdh_get_edit_view_data", "zzdh_save_reference_items"}.issubset(cap["requires_tools"]))


if __name__ == "__main__":
    unittest.main()
