"""`login qr`, `login finish` and `prune`: the commands an agent runs, in any harness."""

from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from tests.helpers import temp_dir
from tests.helpers.mock_feishu import MockFeishu
from tests.test_web_login import Web

from feishu_sdk.cli import Context, main
from feishu_sdk.config import REQUIRED_SCOPES
from feishu_sdk.paths import resolve_data_paths
from feishu_sdk.prune import prune


def write(path: str, text: str = "x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class LoginFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = MockFeishu().start()
        self.addCleanup(self.mock.stop)
        self.env = {"AGENT_USER_DIR": temp_dir("fs-flow-"), "FEISHU_APP_ID": self.mock.app_id, "FEISHU_APP_SECRET": self.mock.app_secret}
        # The local Feishu's device page is not on accounts.feishu.cn: let the browser stand-in "open" it.
        patch = mock.patch("feishu_sdk.web_login.check_verification_url", lambda raw, _code: raw)
        patch.start()
        self.addCleanup(patch.stop)

    def ctx(self, web: Web, **env: str) -> Context:
        return Context(env={**self.env, **env}, endpoints=self.mock.endpoints, web=web)

    def test_login_qr_saves_the_code_for_the_user_and_login_finish_collects_and_checks_the_login(self) -> None:
        qr = os.path.join(temp_dir("fs-flow-out-"), "login.png")
        shown, code = main(["login", "qr", "--output", qr], self.ctx(Web({"state": "qr", "png": b"png"})))
        self.assertEqual((code, shown["status"], shown["qr_file"]), (0, "qr_ready", qr))
        self.assertRegex(shown["message"], "login finish")
        with open(qr, "rb") as handle:
            self.assertEqual(handle.read(), b"png")
        self.mock.scope = " ".join([*REQUIRED_SCOPES, "offline_access"])
        self.mock.device_script = ["issue"]
        done, code = main(["login", "finish"], self.ctx(Web({"state": "approved"})))
        self.assertEqual((code, done["status"]), (0, "ok"), done)
        # Logged in already: finish says so instead of starting anything.
        again, code = main(["login", "finish"], self.ctx(Web({"state": "approved"})))
        self.assertEqual((code, again["status"]), (0, "ok"))

    def test_the_qr_is_never_made_in_a_group_chat_and_without_a_browser_the_link_is_given(self) -> None:
        web = Web({"state": "qr", "png": b"png"})
        refused, code = main(["login", "qr", "--output", os.path.join(temp_dir("fs-flow-out-"), "q.png")], self.ctx(web, AGENT_CHAT_TYPE="group"))
        self.assertEqual((code, refused["status"]), (2, "blocked"))
        self.assertRegex(refused["reason"], "group chat")
        self.assertEqual(web.requests, [], "nothing was started")

        class NoBrowser(Web):
            def drive(self, **_: object) -> dict:
                from feishu_sdk.errors import Blocked
                raise Blocked("The browser did not start: make sure Chrome or Edge is installed")

        linked, code = main(["login", "qr", "--output", os.path.join(temp_dir("fs-flow-out-"), "q.png")], self.ctx(NoBrowser({})))
        self.assertEqual((code, linked["status"]), (0, "no_browser"))
        self.assertTrue(linked["verification_uri_complete"] and linked["user_code"])
        missing, code = main(["login", "qr"], self.ctx(web))
        self.assertEqual((code, missing["status"]), (2, "blocked"))


class PruneTest(unittest.TestCase):
    def test_stale_temporary_files_and_the_browser_caches_go_the_login_and_the_sign_in_stay(self) -> None:
        env = {"AGENT_USER_DIR": temp_dir("fs-prune-")}
        paths = resolve_data_paths(env)
        stale = write(os.path.join(paths.root, "auth.json.123.0123456789ab.tmp"))
        login = write(paths.auth, "{}")
        profile = paths.browser_profile
        caches = [write(os.path.join(profile, "Default", "Cache", "Cache_Data", "f_000001")),
                  write(os.path.join(profile, "Default", "Service Worker", "CacheStorage", "a", "b")),
                  write(os.path.join(profile, "GrShaderCache", "data_0"))]
        session = [write(os.path.join(profile, "Default", "Network", "Cookies")), write(os.path.join(profile, "Local State"))]

        # A command holds the profile: its caches stay, however old.
        lock = write(f"{profile}.lock", json.dumps({"pid": os.getpid(), "at": int(time.time() * 1000), "token": "t"}))
        # A file's creation time cannot be set back: it is judged a month from now.
        held = prune(env, now=time.time() + 30 * 86_400)
        self.assertEqual((held["status"], held["files"]), ("ok", 1), "only the stale temporary file went")
        self.assertFalse(os.path.exists(stale))
        fresh = write(os.path.join(paths.root, "auth.json.124.0123456789ab.tmp"))
        self.assertTrue(all(os.path.exists(file) for file in caches))
        os.remove(lock)

        done, code = main(["prune"], Context(env=env))
        self.assertEqual((code, done["files"]), (0, 3))
        self.assertTrue(all(not os.path.exists(file) for file in caches))
        self.assertTrue(all(os.path.exists(file) for file in [fresh, login, *session]))
        self.assertFalse(os.path.exists(f"{profile}.lock"), "the lock prune took is let go of")

    def test_nothing_there_is_nothing_to_do(self) -> None:
        self.assertEqual(main(["prune"], Context(env={"AGENT_USER_DIR": temp_dir("fs-prune-")})), ({"status": "ok", "files": 0, "bytes": 0}, 0))


if __name__ == "__main__":
    unittest.main()
