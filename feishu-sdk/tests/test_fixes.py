from __future__ import annotations

import json
import os
import unittest

from tests.helpers import Clock, now_ms, temp_dir
from tests.helpers.mock_feishu import MockFeishu

from feishu_sdk.auth import FeishuAuth
from feishu_sdk.config import AppCredentials
from feishu_sdk.errors import BridgeError
from feishu_sdk.fsutil import with_lock
from feishu_sdk.paths import resolve_data_paths


class FixesTest(unittest.TestCase):
    def test_a_lock_holder_that_was_taken_over_does_not_remove_the_lock_of_whoever_has_it_now(self) -> None:
        file = os.path.join(temp_dir("fs-lock-"), "x.lock")

        def taken_over() -> None:
            os.remove(file)  # taken over as stale ...
            with open(file, "w", encoding="utf-8") as handle:  # ... and held by another process
                json.dump({"pid": 1, "at": now_ms(), "owner": "somebody-else"}, handle)

        with_lock(file, taken_over)
        with open(file, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["owner"], "somebody-else")
        os.remove(file)
        with_lock(file, lambda: self.assertTrue(os.path.exists(file)))
        self.assertFalse(os.path.exists(file), "its own lock is released")

    def setup_auth(self, refresh: bool = True) -> tuple[MockFeishu, FeishuAuth, Clock]:
        mock = MockFeishu().start()
        self.addCleanup(mock.stop)
        mock.with_refresh_token = refresh
        clock = Clock()
        auth = FeishuAuth(paths=resolve_data_paths({"LARK_AGENT_USER_DIR": temp_dir("fs-auth2-")}), app=AppCredentials(mock.app_id, mock.app_secret),
                          endpoints=mock.endpoints, now=clock.now, sleep=clock.sleep)
        return mock, auth, clock

    def test_a_gateway_error_while_the_user_is_confirming_does_not_cost_them_the_authorization(self) -> None:
        mock, auth, _ = self.setup_auth()
        auth.start_login()
        mock.device_script = [(502, {}), (429, {"code": 99}), "issue"]
        self.assertEqual(auth.poll_login(60_000)["state"], "authorized")

    def test_a_login_without_a_refresh_token_stays_until_its_access_token_really_ends(self) -> None:
        mock, auth, clock = self.setup_auth(refresh=False)
        auth.start_login()
        mock.device_script = ["issue"]
        self.assertEqual(auth.poll_login(40_000)["state"], "authorized")
        first = auth.access_token()
        clock.t += (7200 - 120) * 1000  # inside the renewal margin, still valid
        self.assertEqual(auth.access_token(), first)
        self.assertTrue(os.path.exists(auth.paths.auth), "the login is not deleted early")
        clock.t += 200 * 1000  # now it has ended
        with self.assertRaisesRegex(BridgeError, "已过期"):
            auth.access_token()
        self.assertFalse(os.path.exists(auth.paths.auth))


if __name__ == "__main__":
    unittest.main()
