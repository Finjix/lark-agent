from __future__ import annotations

import unittest

from tests.helpers import Clock, now_ms, temp_dir
from tests.helpers.mock_feishu import MockFeishu

from feishu_sdk.auth import FeishuAuth
from feishu_sdk.bridge import BRIDGE_VERSION, COMMANDS, INPUT_COMMANDS, BridgeContext, run_bridge
from feishu_sdk.config import REQUIRED_SCOPES, AppCredentials, Endpoints
from feishu_sdk.paths import resolve_data_paths

NOWHERE = Endpoints(open="http://127.0.0.1:9", accounts="http://127.0.0.1:9")


class BridgeTest(unittest.TestCase):
    def test_the_bridge_version_and_every_command_known_to_the_entry_point(self) -> None:
        self.assertEqual(BRIDGE_VERSION, "feishu-sdk-bridge/1")
        self.assertEqual(COMMANDS, ("info", "auth-status", "auth-verify", "auth-login", "auth-poll", "auth-logout", "auth-check-scopes", "api-get",
                                    "sheets-revision", "sheets-export", "sheets-cells-set", "sheets-create", "sheets-dropdown-add", "docx-blocks",
                                    "docx-insert", "docx-set-texts"))
        for name in INPUT_COMMANDS:
            self.assertIn(name, COMMANDS)
        for name in ("info", "auth-status", "auth-logout"):
            self.assertNotIn(name, INPUT_COMMANDS, f"{name} must not wait on stdin")
        # Each one reaches its own handling (none is an unknown command); nothing gets far enough to need Feishu, which is a closed port here.
        env = {"LARK_AGENT_USER_DIR": temp_dir("fs-br-"), "FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "secret-000000"}
        for name in COMMANDS:
            reply = run_bridge(name, None, BridgeContext(env=env, endpoints=NOWHERE))
            self.assertTrue(reply["ok"] or "未知命令" not in reply["error"]["message"], name)

    def test_unknown_commands_and_malformed_arguments_are_refused_not_guessed(self) -> None:
        env = {"LARK_AGENT_USER_DIR": temp_dir("fs-br-"), "FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "secret-000000"}
        ctx = BridgeContext(env=env)
        self.assertEqual(run_bridge("rm-rf", {}, ctx)["error"]["kind"], "invalid_request")
        self.assertEqual(run_bridge("api-get", [1, 2], ctx)["error"]["kind"], "invalid_request")
        self.assertEqual(run_bridge("auth-check-scopes", {"required": [1]}, ctx)["error"]["kind"], "invalid_request")
        alone = BridgeContext(env={"LARK_AGENT_USER_DIR": env["LARK_AGENT_USER_DIR"]})
        self.assertEqual(run_bridge("auth-login", {}, alone)["error"]["kind"], "config")
        # Status and logout work without the app configured: they only touch local files.
        self.assertIs(run_bridge("auth-status", None, alone)["ok"], True)
        self.assertIs(run_bridge("auth-logout", None, alone)["ok"], True)

    def test_the_scope_check_reports_what_is_granted_and_missing_without_the_word_ok(self) -> None:
        # requirement-auto's command line reads "ok" as failure.
        mock = MockFeishu().start()
        self.addCleanup(mock.stop)
        folder = temp_dir("fs-br-")
        mock.scope = " ".join([*REQUIRED_SCOPES[:5], "offline_access"])
        clock = Clock(now_ms())
        auth = FeishuAuth(paths=resolve_data_paths({"LARK_AGENT_USER_DIR": folder}), app=AppCredentials(mock.app_id, mock.app_secret),
                          endpoints=mock.endpoints, now=clock.now, sleep=clock.sleep)
        auth.start_login()
        mock.device_script = ["issue"]
        auth.poll_login(10_000)
        ctx = BridgeContext(env={"LARK_AGENT_USER_DIR": folder, "FEISHU_APP_ID": mock.app_id, "FEISHU_APP_SECRET": mock.app_secret}, endpoints=mock.endpoints)
        partial = run_bridge("auth-check-scopes", {"required": list(REQUIRED_SCOPES)}, ctx)
        self.assertIs(partial["ok"], True)
        self.assertIs(partial["data"]["complete"], False)
        self.assertEqual(partial["data"]["missing"], list(REQUIRED_SCOPES[5:]))
        self.assertNotIn("ok", partial["data"])
        enough = run_bridge("auth-check-scopes", {"required": list(REQUIRED_SCOPES[:5])}, ctx)
        self.assertIs(enough["data"]["complete"], True)
        self.assertEqual(run_bridge("info", None, ctx)["data"]["bridge"], BRIDGE_VERSION)

    def test_numbers_and_strings_of_the_input_are_read_as_the_typescript_bridge_read_them(self) -> None:
        env = {"LARK_AGENT_USER_DIR": temp_dir("fs-br-"), "FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "secret-000000"}
        ctx = BridgeContext(env=env, endpoints=NOWHERE)
        # Refused locally before the login is even looked at: a row given as text that is no number, a true (Number(true) is 1), no row at all.
        base = {"token": "shtABCDEFGH", "sheet_id": "s1", "column": "M", "last_row": 3, "option": "a", "color": "#000000"}
        for first_row in ("x", True, None):
            value = {**base, **({"first_row": first_row} if first_row is not None else {})}
            reply = run_bridge("sheets-dropdown-add", value, ctx)
            self.assertEqual((reply["error"]["kind"], reply["error"]["message"]), ("invalid_request", "行范围无效（必须从第 2 行起，一次不超过 5000 行）"))
        # A row given as a numeric string is a number, as Number("3") was: it gets as far as needing the login.
        reply = run_bridge("sheets-dropdown-add", {**base, "first_row": "2"}, ctx)
        self.assertEqual(reply["error"]["kind"], "auth_required")
        # A path that is no string is made one (String(x)) and refused by the allowlist.
        self.assertEqual(run_bridge("api-get", {"path": 5}, ctx)["error"]["message"], "未许可的飞书读取路径")


if __name__ == "__main__":
    unittest.main()
