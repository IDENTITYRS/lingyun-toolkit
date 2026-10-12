"""可选集成回归：用字字动画自带 Python 3.12 运行真实后端，作品数据全部隔离。"""

import asyncio
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
HOST_ROOT = ROOT.parents[3]
sys.path.insert(0, str(ROOT))
from lingyun_features import mapper

HOST_AVAILABLE = sys.version_info[:2] == (3, 12) and (HOST_ROOT / "backend/handler_core.cp312-win_amd64.pyd").is_file()


@unittest.skipUnless(HOST_AVAILABLE, "需要字字动画安装目录及其 Python 3.12")
class NativeHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path[:0] = [str(HOST_ROOT), str(HOST_ROOT / "site-packages")]
        from backend.handler_core import BackendMessageHandler
        from gui.viewer.viewer_data_serializer import ViewerDataSerializer
        cls.handler_type = BackendMessageHandler
        cls.serializer = ViewerDataSerializer

    def test_actual_host_expansion_and_cleanup_for_all_asset_types(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            host = MagicMock()
            host.project_path = directory
            host._char_auto_ref_enabled.return_value = True
            host._norm_ref_path = self.handler_type._norm_ref_path
            host._entity_image_candidates.return_value = []
            host._find_model_reference_screenshot.return_value = ""
            viewer = SimpleNamespace(data=SimpleNamespace(reference_items=[]))
            host._get_viewer.return_value = viewer
            entities = {}
            for kind, name in (("character", "罗秀英"), ("location", "缝纫车间"), ("item", "缝纫机")):
                paths = [str(Path(directory) / (kind + suffix + ".png")) for suffix in ("-new", "-old")]
                for path in paths:
                    Path(path).touch()
                asset = {kind + "_id": kind + "-1", "name": name,
                         "reference_items": [{"path": path, "media_type": "image"} for path in paths]}
                entities[kind] = asset
            host.context.characters_data = {"character-1": entities["character"]}
            host.context.locations_data = {"location-1": entities["location"]}
            host.context.items_data = {"item-1": entities["item"]}
            selection = {"unique_name": "test-panel", "character_ids": ["character-1"],
                         "location_ids": ["location-1"], "item_ids": ["item-1"]}
            self.handler_type._handle_api_set_panel_entity_selections(host, selection)
            # 原宿主行为：3 个资产展开成 6 张新旧图片。
            self.assertEqual(len(viewer.data.reference_items), 6)

            test = self

            class Tools:
                async def call_tool(self, name, args):
                    if name == "zzdh_get_entity_list":
                        kind = args["entity_type"]
                        cfg = test.handler_type._ENTITY_CONFIG[kind]
                        host._load_entity_json.return_value = {cfg["list_key"]: [entities[kind]]}
                        return {"entities": test.handler_type._build_entity_list_for_vue(host, cfg)}
                    if name == "zzdh_get_panels":
                        return {"panels": [{"unique_name": "test-panel", "panel_index": 1,
                                            "prompt": "罗秀英在缝纫车间使用缝纫机。"}]}
                    if name == "zzdh_set_panel_entity_selections":
                        return test.handler_type._handle_api_set_panel_entity_selections(host, args)
                    if name == "zzdh_get_edit_view_data":
                        return {"unique_name": "test-panel", "reference_images":
                                test.serializer._get_reference_images(viewer.data, directory)}
                    if name == "zzdh_save_reference_items":
                        return test.handler_type._handle_api_save_reference_items(host, args)
                    raise AssertionError(name)

            tools = Tools()
            result = asyncio.run(mapper.execute({"tools": tools, "arguments": {}}))
            self.assertEqual(result["status"], "ok", result)
            refs = self.serializer._get_reference_images(viewer.data, directory)
            self.assertEqual(len(refs), 3)
            self.assertTrue(all(r["path"].endswith("-new.png") for r in refs))
            # 宿主把用户选中的旧图放到首位，再次匹配应跟随当前主图。
            for asset in entities.values():
                asset["reference_items"].reverse()
            result = asyncio.run(mapper.execute({"tools": tools, "arguments": {}}))
            self.assertEqual(result["status"], "ok", result)
            refs = self.serializer._get_reference_images(viewer.data, directory)
            self.assertEqual(len(refs), 3)
            self.assertTrue(all(r["path"].endswith("-old.png") for r in refs))
            self.assertTrue(all(len(asset["reference_items"]) == 2 for asset in entities.values()))


if __name__ == "__main__":
    unittest.main()
