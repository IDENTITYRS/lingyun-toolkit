"""真实 Windows 验签与 DPAPI 的离线授权回归；仅使用测试私钥签发的夹具。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lingyun_features import license_manager as lm
spec = importlib.util.spec_from_file_location("licensed_toolkit_test", ROOT / "main.py")
toolkit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(toolkit)
FIXTURE = json.loads(Path(__file__).with_name("license_fixture.json").read_text(encoding="utf-8"))


class LicenseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "license.dat"
        public_path = self.path.with_name("public.json")
        public_path.write_text(json.dumps(FIXTURE["public"]), encoding="utf-8")
        self.now = FIXTURE["now"]
        for patcher in (patch.object(lm, "_state_path", return_value=self.path),
                        patch.object(lm, "machine_code", return_value=FIXTURE["machine"]),
                        patch.object(lm, "PUBLIC_KEY_FILE", public_path),
                        patch.object(lm.time, "time", side_effect=lambda: self.now)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.codes = FIXTURE["codes"]

    def test_native_signature_verification_all_tiers(self):
        for name, expected in (("month", 30), ("year", 365), ("permanent", 0), ("custom", 19)):
            self.assertEqual(lm.verify_code(self.codes[name])["days"], expected)

    def test_new_activation_and_cumulative_renewal(self):
        self.assertFalse(lm.get_status()["active"])
        self.assertEqual(lm.activate(self.codes["month"])["expires_at"], self.now + 30 * 86400)
        self.now += 10 * 86400
        status = lm.activate(self.codes["year"])
        self.assertEqual(status["expires_at"], FIXTURE["now"] + 395 * 86400)
        status = lm.activate(self.codes["custom"])
        self.assertEqual(status["expires_at"], FIXTURE["now"] + 414 * 86400)
        self.assertEqual(lm.get_status()["expires_at"], status["expires_at"])

    def test_expired_renewal_starts_now(self):
        expiry = lm.activate(self.codes["month"])["expires_at"]
        self.now = expiry
        self.assertFalse(lm.get_status()["active"])
        self.now += 10 * 86400
        self.assertEqual(lm.activate(self.codes["month2"])["expires_at"], self.now + 30 * 86400)

    def test_reused_code_cannot_extend(self):
        first = lm.activate(self.codes["month"])
        with self.assertRaisesRegex(lm.LicenseError, "已经使用"):
            lm.activate(self.codes["month"])
        self.assertEqual(first["expires_at"], lm.get_status()["expires_at"])

    def test_permanent_and_no_downgrade(self):
        lm.activate(self.codes["month"])
        self.assertEqual(lm.activate(self.codes["permanent"])["label"], "已激活·永久")
        self.now += 4000 * 86400
        self.assertTrue(lm.get_status()["active"])
        with self.assertRaisesRegex(lm.LicenseError, "永久激活"):
            lm.activate(self.codes["year"])
        self.assertTrue(lm.get_status()["permanent"])

    def test_wrong_machine_tamper_and_incorrect_key(self):
        with self.assertRaises(lm.LicenseError):
            lm.verify_code(self.codes["month"], machine="OTHER")
        for code in ("bad", self.codes["month"][:-10], self.codes["month"].replace("LY1", "LY2")):
            with self.assertRaises(lm.LicenseError):
                lm.activate(code)
        code = self.codes["month"]
        position = code.rfind(".") + 5
        tampered = code[:position] + ("A" if code[position] != "A" else "B") + code[position + 1:]
        with self.assertRaises(lm.LicenseError):
            lm.activate(tampered)
        public = json.loads((ROOT / "lingyun_features/license_public.json").read_text())
        with self.assertRaises(lm.LicenseError):
            lm.verify_code(code, public=public)
        self.assertFalse(self.path.exists())

    def test_dpapi_state_tampering_fails_closed(self):
        lm.activate(self.codes["month"])
        self.assertNotIn(b"expires_at", self.path.read_bytes())
        self.path.write_bytes(b'{"permanent":true}')
        with self.assertRaises(lm.LicenseError):
            lm.get_status()

    def test_clock_rollback_rejected(self):
        lm.activate(self.codes["month"])
        self.now -= 3600
        self.assertFalse(lm.get_status()["active"])
        with self.assertRaises(lm.LicenseError):
            lm.activate(self.codes["year"])
        self.now = FIXTURE["now"]
        self.assertTrue(lm.get_status()["active"])

    def test_parallel_duplicate_activation_once(self):
        def activate():
            try:
                return lm.activate(self.codes["month"])["active"]
            except lm.LicenseError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: activate(), range(2)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(lm.get_status()["expires_at"], self.now + 30 * 86400)

    def test_ui_and_execute_gate_real_status(self):
        for action in ("import_sections", "scan_assets", "import_assets", "map_images", "choose_folder"):
            self.assertTrue(toolkit.handle_action(action, {}, {})["license_required"])
        self.assertTrue(asyncio.run(toolkit.execute({"arguments": {"feature": "sections"}}))["license_required"])
        response = toolkit.handle_action("license_status", {}, {})
        self.assertTrue(response["ok"])
        self.assertFalse(response["data"]["active"])
        response = toolkit.handle_action("activate_license", {"code": self.codes["month"]}, {})
        self.assertTrue(response["data"]["active"])
        with patch.object(toolkit.sections, "handle_action", return_value={"ok": True, "test": "routed"}):
            self.assertEqual(toolkit.handle_action("import_sections", {}, {})["test"], "routed")
        self.assertTrue(toolkit.handle_action("license_status", {}, {})["data"]["active"])
        self.assertFalse(toolkit.handle_action("activate_license", {"code": self.codes["month"]}, {})["license_required"])


if __name__ == "__main__":
    unittest.main()
