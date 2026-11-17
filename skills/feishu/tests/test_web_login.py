from __future__ import annotations

import json
import os
import unittest

from tests.helpers import now_ms, temp_dir

from feishu_sdk.cli import Context, main
from feishu_sdk.errors import Blocked
from feishu_sdk.paths import resolve_data_paths
from feishu_sdk.web_login import authorize, check_verification_url

LINK = "https://accounts.feishu.cn/oauth/v1/device/verify?flow_id=abc&user_code=ABCD-1234"


def pending(**over) -> dict:
    return {"logged_in": False, "app_configured": True, "storage": "host",
            "pending_login": {"user_code": "ABCD-1234", "verification_uri_complete": LINK, "expires_at": now_ms() + 300_000, **over}}


class Web:
    """A stand-in for the hidden browser: records what it was asked to open and answers with a set outcome."""

    def __init__(self, outcome: dict) -> None:
        self.outcome = outcome
        self.requests: list[dict] = []

    def drive(self, *, url: str, mode: str, profile_dir: str) -> dict:
        self.requests.append({"url": url, "mode": mode, "profile_dir": profile_dir})
        return self.outcome


def scratch():
    """The paths of a scratch user, so that the QR record and the browser profile land in a temp dir."""
    return resolve_data_paths({"AGENT_USER_DIR": temp_dir("fs-web-")})


def read(path: str, mode: str = "r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as handle:
        return handle.read()


class WebLoginTest(unittest.TestCase):
    def test_only_the_device_page_of_this_authorization_is_ever_opened_in_the_browser(self) -> None:
        self.assertEqual(check_verification_url(LINK, "ABCD-1234"), LINK)
        for bad in ["http://accounts.feishu.cn/oauth/v1/device/verify?user_code=ABCD-1234", "https://evil.example/oauth/v1/device/verify?user_code=ABCD-1234",
                    "https://accounts.feishu.cn.evil.example/oauth/v1/device/verify?user_code=ABCD-1234", "https://accounts.feishu.cn/other?user_code=ABCD-1234",
                    "not a url"]:
            with self.assertRaisesRegex(Blocked, "refusing|valid", msg=bad):
                check_verification_url(bad, "ABCD-1234")
        with self.assertRaisesRegex(Blocked, "does not match the current user code"):
            check_verification_url(LINK, "WXYZ-9999")

    def test_authorize_qr_writes_the_sign_in_qr_and_an_existing_web_session_is_approved_without_a_scan(self) -> None:
        paths = scratch()
        qr = Web({"state": "qr", "png": b"png-bytes"})
        shown = authorize(pending(), paths, qr, os.path.join(temp_dir("fs-out-"), "q.png"))
        self.assertEqual(shown["status"], "qr_ready")
        self.assertEqual(qr.requests, [{"url": LINK, "mode": "start", "profile_dir": paths.browser_profile}])
        self.assertEqual(read(shown["qr_file"], "rb"), b"png-bytes")
        self.assertTrue(os.path.exists(paths.qr), "the picture is recorded, to be removed once the login is done")
        record = json.loads(read(paths.qr))
        self.assertEqual((list(record), record["path"]), (["path", "created_at"], shown["qr_file"]))

        done = authorize(pending(), paths, Web({"state": "approved"}), os.path.join(temp_dir("fs-out-"), "q.png"))
        self.assertEqual(done["status"], "approved")
        self.assertRegex(done["message"], "login poll")
        self.assertFalse(os.path.exists(shown["qr_file"]) or os.path.exists(paths.qr), "and the earlier picture is gone")

    def test_authorize_without_qr_finishes_after_the_scan_and_says_so_when_nobody_has_scanned(self) -> None:
        paths = scratch()
        finish = Web({"state": "approved"})
        self.assertEqual(authorize(pending(), paths, finish, None)["status"], "approved")
        self.assertEqual([r["mode"] for r in finish.requests], ["finish"])
        self.assertEqual(authorize(pending(), paths, Web({"state": "login_required"}), None)["status"], "login_required")
        stuck = authorize(pending(), paths, Web({"state": "stuck", "where": "x/y"}), None)
        self.assertEqual((stuck["status"], stuck["where"]), ("stuck", "x/y"))

    def test_authorize_needs_a_pending_still_valid_authorization_and_never_opens_the_browser_without_one(self) -> None:
        paths = scratch()
        none = Web({"state": "approved"})
        self.assertEqual(authorize({"logged_in": False, "app_configured": True, "storage": "host"}, paths, none, None)["status"], "no_pending")
        self.assertEqual(authorize(pending(expires_at=now_ms() - 1), paths, none, None)["status"], "no_pending")
        with self.assertRaisesRegex(Blocked, "refusing"):
            authorize(pending(verification_uri_complete="https://evil.example/x?user_code=ABCD-1234"), paths, none, None)
        with self.assertRaisesRegex(Blocked, r"\.png"):
            authorize(pending(), paths, none, os.path.join(temp_dir("fs-out-"), "q.jpg"))
        self.assertEqual(len(none.requests), 0)

        # Through the command line: nothing pending for a fresh user, and the browser is not touched.
        env = {"AGENT_USER_DIR": temp_dir("fs-web-"), "FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "secret-000000"}
        result, code = main(["authorize", "--qr", os.path.join(temp_dir("fs-out-"), "q.png")], Context(env=env, web=none))
        self.assertEqual((code, result["status"]), (0, "no_pending"))
        self.assertEqual(len(none.requests), 0)

    def test_a_picture_that_may_not_be_written_is_refused_before_the_browser_waits_and_an_old_code_is_reaped(self) -> None:
        paths = scratch()
        qr = Web({"state": "qr", "png": b"png-bytes"})
        taken = os.path.join(temp_dir("fs-out-"), "mine.png")
        with open(taken, "w", encoding="utf-8") as handle:
            handle.write("the user's own picture")
        with self.assertRaisesRegex(Blocked, "already exists"):
            authorize(pending(), paths, qr, taken)
        self.assertEqual(len(qr.requests), 0, "the browser was never opened")
        self.assertEqual(read(taken), "the user's own picture")

        old = os.path.join(temp_dir("fs-out-"), "old.png")
        with open(old, "w", encoding="utf-8") as handle:
            handle.write("png")
        os.makedirs(os.path.dirname(paths.qr), exist_ok=True)
        with open(paths.qr, "w", encoding="utf-8") as handle:
            json.dump({"path": old, "created_at": now_ms() - 20 * 60_000}, handle)
        self.assertEqual(authorize({"logged_in": True, "app_configured": True, "storage": "host"}, paths, qr, None)["status"], "no_pending")
        self.assertFalse(os.path.exists(old) or os.path.exists(paths.qr), "the picture nobody scanned is gone, and so would be its browser")


if __name__ == "__main__":
    unittest.main()
