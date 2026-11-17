from __future__ import annotations

import json
import os
import unittest
from urllib.parse import parse_qs

from tests.helpers import Clock, temp_dir
from tests.helpers.mock_feishu import MockFeishu

from feishu_sdk.auth import FeishuAuth
from feishu_sdk.config import REQUIRED_SCOPES, AppCredentials
from feishu_sdk.errors import BridgeError
from feishu_sdk.paths import resolve_data_paths


class Setup:
    def __init__(self, test: unittest.TestCase, expected_open_id: str | None = None) -> None:
        self.mock = MockFeishu().start()
        test.addCleanup(self.mock.stop)
        self.user_dir = temp_dir("fs-auth-")
        self.clock = Clock()
        self.auth = FeishuAuth(paths=resolve_data_paths({"AGENT_USER_DIR": self.user_dir}), app=AppCredentials(self.mock.app_id, self.mock.app_secret),
                               endpoints=self.mock.endpoints, expected_open_id=expected_open_id, now=self.clock.now, sleep=self.clock.sleep)

    def login(self) -> None:
        self.auth.start_login()
        self.mock.device_script = ["issue"]
        self.auth.poll_login(10_000)


def stored(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class AuthTest(unittest.TestCase):
    def kind(self, kind: str, call) -> BridgeError:
        with self.assertRaises(BridgeError) as caught:
            call()
        self.assertEqual(caught.exception.kind, kind, caught.exception.message)
        return caught.exception

    def test_login_asks_for_every_needed_scope_with_basic_auth_and_a_repeat_reuses_the_request(self) -> None:
        s = Setup(self)
        first = s.auth.start_login()
        self.assertEqual(first["user_code"], "ABCD-1234")
        self.assertRegex(first["verification_uri_complete"], r"/verify\?user_code=ABCD-1234$")
        self.assertIs(first["reused"], False)
        form = parse_qs(s.mock.requests("/oauth/v1/device_authorization")[0].body)
        self.assertEqual(form["client_id"], [s.mock.app_id])
        scopes = form["scope"][0].split(" ")
        for scope in [*REQUIRED_SCOPES, "offline_access", "docx:document"]:
            self.assertIn(scope, scopes)
        second = s.auth.start_login()
        self.assertIs(second["reused"], True)
        self.assertEqual(len(s.mock.requests("/oauth/v1/device_authorization")), 1)
        # A pending request shows in the status, without anything secret.
        status = s.auth.status()
        self.assertIs(status["logged_in"], False)
        self.assertEqual(status["pending_login"]["user_code"], "ABCD-1234")
        self.assertNotIn("dev-code-1", json.dumps(status))
        # The pending request is stored as the TypeScript plugin stored it.
        self.assertEqual(list(stored(s.auth.paths.login)), ["v", "app_id", "device_code", "user_code", "verification_uri", "verification_uri_complete",
                                                           "scope", "expires_at", "interval_s", "next_poll_at"])

    def test_polling_waits_out_authorization_pending_then_stores_the_login(self) -> None:
        # The status never carries a token.
        s = Setup(self)
        s.auth.start_login()
        s.mock.device_script = [(400, {"error": "authorization_pending"}), (400, {"error": "authorization_pending"}), "issue"]
        result = s.auth.poll_login(40_000)
        self.assertEqual((result["state"], result["open_id"]), ("authorized", "ou_alice"))
        self.assertFalse(os.path.exists(s.auth.paths.login))
        token = stored(s.auth.paths.auth)
        self.assertEqual(list(token), ["v", "app_id", "open_id", "access_token", "access_expires_at", "refresh_expires_at", "scope", "granted_at",
                                       "refreshed_at", "name", "refresh_token"])
        self.assertEqual((token["open_id"], token["access_token"], token["refresh_token"]), ("ou_alice", s.mock.current_access, s.mock.current_refresh))
        self.assertIsInstance(token["access_expires_at"], int)
        status = s.auth.status()
        self.assertEqual((status["logged_in"], status["name"], status["refreshable"]), (True, "李小明", True))
        text = json.dumps(status)
        self.assertNotIn(s.mock.current_access, text)
        self.assertNotIn(s.mock.current_refresh, text)
        self.assertEqual(status["missing_scopes"], [item for item in REQUIRED_SCOPES if item not in s.mock.scope.split(" ")])

    def test_slow_down_adds_five_seconds_and_a_short_wait_returns_pending_without_asking(self) -> None:
        s = Setup(self)
        s.auth.start_login()
        s.mock.device_script = [(400, {"error": "slow_down"}), "issue"]
        self.assertEqual(s.auth.poll_login(40_000)["state"], "authorized")
        # interval 1 s, then 1 + 5 s after slow_down
        self.assertEqual(s.clock.slept, [1000, 6000])

        other = Setup(self)
        other.mock.device_interval = 30
        other.auth.start_login()
        self.assertEqual(other.auth.poll_login(1000)["state"], "pending")
        self.assertEqual(len(other.mock.requests("/oauth/v3/token")), 0)

    def test_a_refusal_and_an_expired_request_end_the_pending_authorization(self) -> None:
        denied = Setup(self)
        denied.auth.start_login()
        denied.mock.device_script = [(400, {"error": "access_denied"})]
        self.assertEqual(denied.auth.poll_login(10_000), {"state": "denied"})
        self.assertFalse(os.path.exists(denied.auth.paths.login))
        self.assertFalse(os.path.exists(denied.auth.paths.auth))

        expired = Setup(self)
        expired.auth.start_login()
        expired.mock.device_script = [(400, {"error": "expired_token"})]
        self.assertEqual(expired.auth.poll_login(10_000), {"state": "expired"})
        expired.auth.start_login()
        expired.clock.t += 241_000
        self.assertEqual(expired.auth.poll_login(10_000), {"state": "expired"})

    def test_a_login_of_somebody_else_than_the_user_this_process_serves_is_refused(self) -> None:
        s = Setup(self, expected_open_id="ou_bob")
        s.auth.start_login()
        s.mock.device_script = ["issue"]
        self.kind("identity_mismatch", lambda: s.auth.poll_login(10_000))
        self.assertFalse(os.path.exists(s.auth.paths.auth), "nothing is stored")
        self.assertFalse(os.path.exists(s.auth.paths.login))

    def test_a_stored_login_that_belongs_to_another_user_is_not_used(self) -> None:
        s = Setup(self)
        s.login()
        intruder = FeishuAuth(paths=s.auth.paths, app=AppCredentials(s.mock.app_id, s.mock.app_secret), endpoints=s.mock.endpoints, expected_open_id="ou_bob")
        self.kind("identity_mismatch", intruder.access_token)
        self.kind("identity_mismatch", intruder.status)
        self.assertTrue(os.path.exists(os.path.join(s.user_dir, "feishu", "auth.json")))

    def test_the_access_token_is_renewed_shortly_before_it_ends_and_the_refresh_token_rotates(self) -> None:
        s = Setup(self)
        s.login()
        first = s.mock.current_access
        self.assertEqual(s.auth.access_token(), first)
        self.assertEqual(s.mock.refresh_count, 0)
        s.clock.t += (7200 - 200) * 1000  # inside the five-minute margin
        renewed = s.auth.access_token()
        self.assertNotEqual(renewed, first)
        self.assertEqual(renewed, s.mock.current_access)
        self.assertEqual(s.mock.refresh_count, 1)
        self.assertEqual(stored(s.auth.paths.auth)["refresh_token"], s.mock.current_refresh)
        self.assertEqual(s.auth.access_token(), renewed, "a second call needs no renewal")
        self.assertEqual(s.mock.refresh_count, 1)

    def test_a_refresh_token_the_server_no_longer_accepts_ends_the_login_an_unreachable_server_does_not(self) -> None:
        s = Setup(self)
        s.login()
        s.clock.t += 7_100_000
        s.mock.stop()  # nobody to answer
        self.kind("network", s.auth.access_token)
        self.assertTrue(os.path.exists(s.auth.paths.auth), "the login survives a failed attempt to reach Feishu")
        s.mock.start()
        token = stored(s.auth.paths.auth)
        token["refresh_token"] = "ur-already-used"
        with open(s.auth.paths.auth, "w", encoding="utf-8") as handle:
            json.dump(token, handle)
        second = FeishuAuth(paths=s.auth.paths, app=AppCredentials(s.mock.app_id, s.mock.app_secret), endpoints=s.mock.endpoints, now=s.clock.now, sleep=s.clock.sleep)
        self.kind("auth_required", second.access_token)
        self.assertFalse(os.path.exists(s.auth.paths.auth))

    def test_without_a_refresh_token_the_login_is_good_for_the_access_token_alone(self) -> None:
        s = Setup(self)
        s.mock.with_refresh_token = False
        s.auth.start_login()
        s.mock.device_script = ["issue"]
        self.assertIs(s.auth.poll_login(10_000)["refreshable"], False)
        self.assertIs(s.auth.status()["refreshable"], False)
        s.clock.t += 7_300_000
        self.assertIs(s.auth.status()["logged_in"], False)
        self.kind("auth_required", s.auth.access_token)

    def test_logging_out_removes_the_stored_login_and_the_pending_request(self) -> None:
        s = Setup(self)
        s.login()
        try:
            s.auth.start_login()
        except BridgeError:
            pass
        s.auth.logout()
        self.assertFalse(os.path.exists(s.auth.paths.auth))
        self.assertFalse(os.path.exists(s.auth.paths.login))
        self.assertIs(s.auth.status()["logged_in"], False)

    def test_wrong_app_credentials_are_reported_as_a_configuration_problem(self) -> None:
        s = Setup(self)
        auth = FeishuAuth(paths=resolve_data_paths({"AGENT_USER_DIR": temp_dir("fs-auth-")}), app=AppCredentials(s.mock.app_id, "wrong-secret-0000"),
                          endpoints=s.mock.endpoints)
        self.kind("config", auth.start_login)

    def test_a_login_the_typescript_plugin_stored_is_read_as_it_is(self) -> None:
        # The format on disk is shared with the plugin's earlier version: a file it wrote is a login here.
        s = Setup(self)
        s.login()
        token = stored(s.auth.paths.auth)
        os.remove(s.auth.paths.auth)
        with open(s.auth.paths.auth, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(token, indent=2, ensure_ascii=False))
        self.assertEqual(s.auth.status()["open_id"], "ou_alice")
        self.assertEqual(s.auth.access_token(), token["access_token"])


if __name__ == "__main__":
    unittest.main()
