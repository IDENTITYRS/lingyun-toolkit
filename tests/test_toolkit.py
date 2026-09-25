"""离线回归：模拟宿主接口，不访问实际作品。"""

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("tool_plugin_lingyun_test", ROOT / "main.py")
toolkit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(toolkit)


class FakeTools:
    def __init__(self):
        self.calls = []
        self.panels = [{"unique_name": "p1", "panel_index": 1, "video_prompt": "陈平走进城门，拿起长剑。"}]
        self.entities = {"character": [], "location": [], "item": []}
        self.closed = False

    def call_sync(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "zzdh_get_panels":
            return {"panels": list(self.panels)}
        if name == "zzdh_add_panel":
            panel = {"unique_name": "p" + str(len(self.panels) + 1), "panel_index": len(self.panels) + 1}
            self.panels.append(panel)
            return {"success": True, **panel}
        if name == "zzdh_get_entity_list":
            return {"success": True, "entities": self.entities[args["entity_type"]]}
        if name == "zzdh_batch_create_entity":
            for entity in args["entities"]:
                self.entities[entity["entity_type"]].append(entity)
            return {"success": True, "count": len(args["entities"]), "results": args["entities"], "errors": []}
        if name.startswith("zzdh_update_") or name == "zzdh_set_panel_entity_selections":
            return {"success": True}
        raise AssertionError("Unexpected host call: " + name)

    async def call_tool(self, name, args):
        return self.call_sync(name, args)

    async def close(self):
        self.closed = True


class ToolkitTests(unittest.TestCase):
    def setUp(self):
        # 业务回归只隔离授权门禁；授权与门禁由 test_license.py 单独覆盖。
        active = patch.object(toolkit.license_manager, "require_active", return_value={"active": True})
        active.start()
        self.addCleanup(active.stop)

    def test_original_self_checks(self):
        result = asyncio.run(toolkit.self_test({"tools": FakeTools()}))
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["checks"]), 19)

    def test_sections_async_all_fields_and_append(self):
        for field, (name, key) in toolkit.sections.FIELD_TOOL.items():
            tools = FakeTools()
            result = asyncio.run(toolkit.execute({"tools": tools, "arguments": {
                "feature": "sections", "text": "第一段\n\n第二段", "field": field}}))
            self.assertEqual(result["written"], 2)
            self.assertEqual(result["added_panels"], 1)
            writes = [args for tool, args in tools.calls if tool == name]
            self.assertEqual([args[key] for args in writes], ["第一段", "第二段"])
            self.assertEqual(writes[1]["unique_name"], "p2")

    def test_ui_toolclient_routes_and_close(self):
        for action, payload in (("import_sections", {"text": "第一段\n\n第二段"}),
                                ("map_images", {"start_index": 1})):
            tools = FakeTools()
            client_module = types.ModuleType("tool_plugins.tool_client")
            client_module.ToolClient = lambda *args, **kwargs: tools
            with patch.dict(sys.modules, {"tool_plugins": types.ModuleType("tool_plugins"),
                                          "tool_plugins.tool_client": client_module}):
                result = toolkit.handle_action(action, payload, {})
            self.assertTrue(result["ok"], result)
            self.assertTrue(tools.closed)
            if action == "map_images":
                self.assertTrue(any(name == "zzdh_set_panel_entity_selections" for name, _ in tools.calls))

    def test_assets_scan_import_skip_duplicates_and_async_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            for folder, name in (("角色", "陈平"), ("场景", "城门"), ("物品", "长剑")):
                path = Path(directory) / folder / (name + ".png")
                path.parent.mkdir()
                path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 24)
            tools = FakeTools()
            ctx = {"call_tool": tools.call_sync}
            scan = toolkit.handle_action("scan_assets", {"folder_path": directory}, ctx)
            self.assertTrue(scan["ok"], scan)
            self.assertEqual(scan["data"]["importable_count"], 3)
            imported = toolkit.handle_action("import_assets", {
                "folder_path": directory, "entity_types": ["character"]}, ctx)
            self.assertEqual(imported["data"]["created"], 1)
            self.assertEqual(len(tools.entities["location"]), 0)
            result = asyncio.run(toolkit.execute({"tools": tools, "arguments": {
                "feature": "assets", "action": "import_assets", "folder_path": directory}}))
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["data"]["created"], 2)
            self.assertEqual(result["data"]["skipped_existing"], 1)
            scan = asyncio.run(toolkit.execute({"tools": tools, "arguments": {
                "feature": "assets", "folder_path": directory}}))
            self.assertEqual(scan["data"]["importable_count"], 0)

    def test_picker_cancel_and_errors(self):
        with patch.object(toolkit.assets, "_choose_folder", return_value=""):
            self.assertTrue(toolkit.handle_action("choose_folder", {}, {})["cancelled"])
        with patch.object(toolkit.assets, "_choose_folder", side_effect=RuntimeError("picker error")):
            self.assertIn("picker error", toolkit.handle_action("choose_folder", {}, {})["error"])
        self.assertFalse(toolkit.handle_action("unknown", {}, {})["ok"])
        self.assertFalse(toolkit.handle_action("import_sections", {"text": " \n\n"}, {})["ok"])
        self.assertFalse(toolkit.handle_action("scan_assets", {"folder_path": ""}, {})["ok"])
        result = asyncio.run(toolkit.execute({"arguments": {"feature": "unknown"}}))
        self.assertEqual(result["status"], "error")

    def test_mapping_range_and_failure_reporting(self):
        tools = FakeTools()
        tools.panels.extend([{"unique_name": "p2", "panel_index": 2, "prompt": "城门"},
                             {"unique_name": "p3", "panel_index": 3, "prompt": ""}])
        original = tools.call_sync

        def fail_selection(name, args):
            if name == "zzdh_set_panel_entity_selections":
                return {"success": False, "error": "模拟写入失败"}
            return original(name, args)

        tools.call_sync = fail_selection
        result = asyncio.run(toolkit.execute({"tools": tools, "arguments": {
            "feature": "mapper", "start_index": 2, "end_index": 3, "require_prompt": True}}))
        self.assertEqual(result["target_count"], 1)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["failed"][0]["panel_index"], 2)

    def test_mapping_specified_mode_only_matches_keyword_lines(self):
        tools = FakeTools()
        tools.panels = [{
            "unique_name": "p1",
            "panel_index": 1,
            "prompt": (
                "【出场人物】新郎父亲、林大成、林秋禾\n"
                "【场景地点】村宴会厅结算桌\n"
                "【动作/情绪】桂香嫂已经离席，舅舅看向林秋禾。"
            ),
        }]
        tools.entities = {
            "character": [
                {"character_id": "c_father", "name": "新郎父亲"},
                {"character_id": "c_dacheng", "name": "林大成"},
                {"character_id": "c_qiuhe", "name": "林秋禾"},
                {"character_id": "c_gui", "name": "桂香嫂"},
                {"character_id": "c_uncle", "name": "舅舅"},
            ],
            "location": [{"location_id": "l_table", "name": "村宴会厅结算桌"}],
            "item": [],
        }
        result = asyncio.run(toolkit.execute({"tools": tools, "arguments": {
            "feature": "mapper", "match_mode": "specified", "match_keywords": "人物、场景"
        }}))
        self.assertEqual(result["status"], "ok", result)
        selection = next(args for name, args in tools.calls if name == "zzdh_set_panel_entity_selections")
        self.assertEqual(set(selection["character_ids"]), {"c_father", "c_dacheng", "c_qiuhe"})
        self.assertEqual(selection["location_ids"], ["l_table"])
        self.assertEqual(selection["item_ids"], [])
        self.assertNotIn("c_gui", selection["character_ids"])
        self.assertNotIn("c_uncle", selection["character_ids"])

    def test_mapping_specified_mode_requires_keywords(self):
        with self.assertRaisesRegex(ValueError, "至少填写一个匹配词"):
            asyncio.run(toolkit.execute({"tools": FakeTools(), "arguments": {
                "feature": "mapper", "match_mode": "specified", "match_keywords": ""
            }}))

    def test_relocated_plugin_loads_without_original_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for program in ROOT.glob("*.py"):
                shutil.copy2(program, target / program.name)
            shutil.copy2(ROOT / "plugin.json", target / "plugin.json")
            shutil.copytree(ROOT / "lingyun_features", target / "lingyun_features", ignore=shutil.ignore_patterns("__pycache__"))
            script = (
                "import sys, importlib.util; from pathlib import Path; "
                "entry=Path(sys.argv[1]); assert str(entry.parent) not in sys.path; "
                "spec=importlib.util.spec_from_file_location('isolated_toolkit', entry); "
                "mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); "
                "assert Path(mod.license_manager.__file__).parent == entry.parent / 'lingyun_features'; "
                "assert mod.license_manager.PUBLIC_KEY_FILE.is_file(); "
                "assert mod.license_manager.machine_code().startswith('LY-'); "
                "assert mod.sections._split_sections('a\\n\\nb') == ['a','b']; "
                "assert not mod.handle_action('unknown', {}, {})['ok']; print('ISOLATED_OK')"
            )
            host_directory = target / 'host-working-directory'
            host_directory.mkdir()
            result = subprocess.run([sys.executable, "-I", "-c", script, str(target / 'main.py')], cwd=host_directory,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ISOLATED_OK", result.stdout)

    def test_manifest_covers_actual_host_tools(self):
        meta = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        declared = {name for cap in meta["declared_capabilities"] for name in cap["requires_tools"]}
        expected = {name for name, _ in toolkit.sections.FIELD_TOOL.values()} | {
            "zzdh_get_panels", "zzdh_add_panel", "zzdh_get_entity_list",
            "zzdh_batch_create_entity", "zzdh_set_panel_entity_selections"}
        self.assertEqual(declared, expected)


if __name__ == "__main__":
    unittest.main()
