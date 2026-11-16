from __future__ import annotations

import json
import os
import unittest
from urllib.parse import parse_qs

from tests.helpers import Clock, now_ms, temp_dir
from tests.helpers.mock_feishu import MockFeishu

from feishu_sdk.auth import FeishuAuth
from feishu_sdk.bridge import BridgeContext, run_bridge
from feishu_sdk.cli import Context, main
from feishu_sdk.config import REQUIRED_SCOPES, AppCredentials
from feishu_sdk.paths import resolve_data_paths


def logged_in(mock: MockFeishu, folder: str, **extra_env: str) -> Context:
    """A user who has logged in through the device flow, and the environment a command for them runs in."""
    clock = Clock(now_ms())
    auth = FeishuAuth(paths=resolve_data_paths({"LARK_AGENT_USER_DIR": folder}), app=AppCredentials(mock.app_id, mock.app_secret),
                      endpoints=mock.endpoints, now=clock.now, sleep=clock.sleep)
    auth.start_login()
    mock.device_script = ["issue"]
    auth.poll_login(10_000)
    return Context(env={"LARK_AGENT_USER_DIR": folder, "FEISHU_APP_ID": mock.app_id, "FEISHU_APP_SECRET": mock.app_secret, **extra_env}, endpoints=mock.endpoints)


def verify(ctx: Context) -> dict:
    return run_bridge("auth-verify", None, BridgeContext(env=ctx.env, endpoints=ctx.endpoints))


def status(ctx: Context) -> dict:
    return run_bridge("auth-status", None, BridgeContext(env=ctx.env, endpoints=ctx.endpoints))


class LoginCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = MockFeishu().start()
        self.addCleanup(self.mock.stop)

    def test_the_check_asks_feishu_with_the_stored_login_and_reports_who_it_is(self) -> None:
        self.mock.scope = " ".join([*REQUIRED_SCOPES, "offline_access"])
        ctx = logged_in(self.mock, temp_dir("fs-chk-"))
        before = len(self.mock.requests("/open-apis/authen/v1/user_info"))
        reply = verify(ctx)
        self.assertIs(reply["ok"], True)
        data = reply["data"]
        self.assertEqual([data["logged_in"], data["open_id"], data["name"], data["missing_scopes"]], [True, "ou_alice", "李小明", []])
        self.assertEqual(list(data), ["logged_in", "open_id", "name", "missing_scopes", "refresh_expires_at"])
        self.assertEqual(len(self.mock.requests("/open-apis/authen/v1/user_info")), before + 1, "Feishu was asked, not the local file")
        self.assertNotIn(self.mock.current_access, json.dumps(reply), "no token in the reply")

    def test_a_login_feishu_no_longer_accepts_is_renewed_when_it_can_be_reported_when_it_cannot(self) -> None:
        self.mock.scope = " ".join([*REQUIRED_SCOPES, "offline_access"])
        ctx = logged_in(self.mock, temp_dir("fs-chk-"))
        # The access token was forgotten but the refresh token is good: the check renews the login and passes.
        self.mock.revoke_access()
        refreshed = self.mock.refresh_count
        self.assertIs(verify(ctx)["ok"], True)
        self.assertEqual(self.mock.refresh_count, refreshed + 1)
        # The user withdrew the authorization: both are gone. The local file still says "logged in" until Feishu is asked.
        self.assertIs(status(ctx)["data"]["logged_in"], True, "the local record alone would have passed")
        self.mock.revoke_all()
        self.assertEqual(verify(ctx)["error"]["kind"], "auth_required")
        self.assertIs(status(ctx)["data"]["logged_in"], False, "and the dead login is dropped, so nothing keeps trying it")

    def test_no_login_the_wrong_person_missing_permissions_and_an_unreachable_feishu_are_told_apart(self) -> None:
        none = verify(Context(env={"LARK_AGENT_USER_DIR": temp_dir("fs-chk-none-"), "FEISHU_APP_ID": self.mock.app_id, "FEISHU_APP_SECRET": self.mock.app_secret},
                              endpoints=self.mock.endpoints))
        self.assertEqual(none["error"]["kind"], "auth_required")
        self.mock.scope = " ".join([*REQUIRED_SCOPES[:3], "offline_access"])
        ctx = logged_in(self.mock, temp_dir("fs-chk-"))
        self.assertEqual(verify(ctx)["data"]["missing_scopes"], list(REQUIRED_SCOPES[3:]))
        # The host says this process serves somebody else than the saved login belongs to.
        other = verify(Context(env={**ctx.env, "LARK_AGENT_USER_OPEN_ID": "ou_bob"}, endpoints=ctx.endpoints))
        self.assertEqual(other["error"]["kind"], "identity_mismatch")
        self.mock.stop()
        self.assertEqual(verify(ctx)["error"]["kind"], "network")

    def test_login_check_says_ok_with_who_it_is_or_what_is_wrong_and_what_to_do(self) -> None:
        nobody, code = main(["login", "check"], Context(env={"LARK_AGENT_USER_DIR": temp_dir("fs-chk-none-"), "FEISHU_APP_ID": self.mock.app_id,
                                                             "FEISHU_APP_SECRET": self.mock.app_secret}, endpoints=self.mock.endpoints))
        self.assertEqual((code, nobody["status"]), (2, "login_required"))
        self.assertRegex(nobody["todo"][0], r"login start.*authorize --qr")
        self.mock.scope = " ".join([*REQUIRED_SCOPES, "offline_access"])
        ctx = logged_in(self.mock, temp_dir("fs-chk-"))
        good, code = main(["login", "check"], ctx)
        self.assertEqual((code, good), (0, {"status": "ok", "name": "李小明"}))
        other, code = main(["login", "check"], Context(env={**ctx.env, "LARK_AGENT_USER_OPEN_ID": "ou_bob"}, endpoints=ctx.endpoints))
        self.assertEqual((code, other["status"]), (2, "identity_mismatch"))
        self.assertNotIn("todo", other, "logging in again as the wrong person fixes nothing")
        self.mock.stop()
        down, code = main(["login", "check"], ctx)
        self.assertEqual((code, down["status"]), (2, "unreachable"))

    def test_login_check_reports_a_login_without_every_permission_with_what_is_missing(self) -> None:
        self.mock.scope = " ".join([*REQUIRED_SCOPES[:2], "offline_access"])
        partial, code = main(["login", "check"], logged_in(self.mock, temp_dir("fs-chk-")))
        self.assertEqual((code, partial["status"], partial["missing_scopes"]), (2, "scopes_missing", list(REQUIRED_SCOPES[2:])))

    def test_a_login_approved_for_somebody_else_signs_the_hidden_browser_out_and_logout_browser_does_too(self) -> None:
        folder = temp_dir("fs-chk-")
        env = {"LARK_AGENT_USER_DIR": folder, "LARK_AGENT_USER_OPEN_ID": "ou_bob", "FEISHU_APP_ID": self.mock.app_id, "FEISHU_APP_SECRET": self.mock.app_secret}
        ctx = Context(env=env, endpoints=self.mock.endpoints)
        profile = resolve_data_paths(env).browser_profile
        self.assertEqual(main(["login", "start"], ctx)[1], 0)
        # Alice scanned Bob's code: the browser is signed in to Feishu web as her, and pressed 【授权】 for her.
        os.makedirs(profile, exist_ok=True)
        with open(os.path.join(profile, "Cookies"), "w", encoding="utf-8") as handle:
            handle.write("alice")
        self.mock.device_script = ["issue"]
        poll, code = main(["login", "poll", "--wait", "10"], ctx)
        self.assertEqual((code, poll["status"]), (2, "blocked"))
        self.assertRegex(poll["reason"], r"不是同一个人[\s\S]*网页登录也已退出")
        self.assertFalse(os.path.exists(profile), "her session is gone from his browser")
        self.assertFalse(os.path.exists(resolve_data_paths(env).auth), "and her token was never stored")
        os.makedirs(profile, exist_ok=True)
        out, code = main(["login", "logout", "--browser"], ctx)
        self.assertEqual((code, out), (0, {"logged_in": False, "browser_signed_out": True}))
        self.assertFalse(os.path.exists(profile))

    def test_login_start_and_poll_through_the_command_line(self) -> None:
        folder = temp_dir("fs-chk-")
        ctx = Context(env={"LARK_AGENT_USER_DIR": folder, "FEISHU_APP_ID": self.mock.app_id, "FEISHU_APP_SECRET": self.mock.app_secret}, endpoints=self.mock.endpoints)
        start, code = main(["login", "start", "--extra-scope", "sheets:spreadsheet:create"], ctx)
        self.assertEqual(code, 0)
        self.assertEqual(list(start), ["verification_uri", "verification_uri_complete", "user_code", "expires_in_s", "interval_s", "reused"])
        self.assertIn("sheets:spreadsheet:create", parse_qs(self.mock.requests("/oauth/v1/device_authorization")[0].body)["scope"][0].split(" "))
        bad, code = main(["login", "start", "--extra-scope", "Bad Scope"], ctx)
        self.assertEqual((code, bad["reason"]), (2, "--extra-scope 需要飞书权限名，如 sheets:spreadsheet:create"))
        waiting, code = main(["login", "poll", "--wait", "0"], ctx)
        self.assertEqual((code, waiting["state"]), (0, "pending"))
        self.mock.device_script = ["issue"]
        done, code = main(["login", "poll", "--wait", "5"], ctx)
        self.assertEqual((code, done["state"], done["open_id"]), (0, "authorized", "ou_alice"))
        for wait in ("51", "1.5", "-1", "x"):
            refused, code = main(["login", "poll", f"--wait={wait}"], ctx)
            self.assertEqual((code, refused["reason"]), (2, "--wait 必须在 0 到 50 秒之间"), wait)


if __name__ == "__main__":
    unittest.main()
