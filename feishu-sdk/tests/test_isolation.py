from __future__ import annotations

import json
import os
import unittest

from tests.helpers import Clock, temp_dir
from tests.helpers.mock_feishu import MockFeishu

from feishu_sdk.auth import FeishuAuth
from feishu_sdk.bridge import BridgeContext, run_bridge
from feishu_sdk.config import AppCredentials
from feishu_sdk.errors import BridgeError, redact
from feishu_sdk.paths import expected_open_id, resolve_data_paths


def login(mock: MockFeishu, user_dir: str, open_id: str) -> FeishuAuth:
    mock.open_id = open_id
    clock = Clock()
    auth = FeishuAuth(paths=resolve_data_paths({"AGENT_USER_DIR": user_dir}), app=AppCredentials(mock.app_id, mock.app_secret),
                      endpoints=mock.endpoints, expected_open_id=open_id, now=clock.now, sleep=clock.sleep)
    auth.start_login()
    mock.device_script = ["issue"]
    assert auth.poll_login(10_000)["state"] == "authorized"
    return auth


def stored(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class IsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = MockFeishu().start()
        self.addCleanup(self.mock.stop)

    def test_two_users_on_one_machine_keep_separate_logins_in_their_own_directories(self) -> None:
        alice_dir, bob_dir = temp_dir("fs-iso-"), temp_dir("fs-iso-")
        alice = login(self.mock, alice_dir, "ou_alice")
        alice_token = self.mock.current_access
        bob = login(self.mock, bob_dir, "ou_bob")
        bob_token = self.mock.current_access
        self.assertNotEqual(alice_token, bob_token)
        alice_file, bob_file = stored(os.path.join(alice_dir, "feishu", "auth.json")), stored(os.path.join(bob_dir, "feishu", "auth.json"))
        self.assertEqual((alice_file["open_id"], alice_file["access_token"]), ("ou_alice", alice_token))
        self.assertEqual((bob_file["open_id"], bob_file["access_token"]), ("ou_bob", bob_token))
        self.assertEqual(alice.status()["open_id"], "ou_alice")
        self.assertEqual(bob.status()["open_id"], "ou_bob")
        alice.logout()
        self.assertIs(alice.status()["logged_in"], False)
        self.assertIs(bob.status()["logged_in"], True, "logging out one user leaves the other logged in")
        self.assertFalse(os.path.exists(os.path.join(alice_dir, "feishu", "auth.json")))

    def test_the_data_directory_comes_from_the_host_environment_only_and_only_as_a_safe_absolute_path(self) -> None:
        folder = temp_dir("fs-iso-")
        host = resolve_data_paths({"AGENT_USER_DIR": folder})
        self.assertEqual(host.mode, "host")
        self.assertEqual(host.root, os.path.join(folder, "feishu"))
        self.assertEqual(host.auth, os.path.join(folder, "feishu", "auth.json"))
        self.assertEqual(host.browser_profile, os.path.join(folder, "feishu", "browser-profile"))
        # A path with .. is normalised, not followed out of the user's directory by a later join.
        self.assertEqual(resolve_data_paths({"AGENT_USER_DIR": os.path.join(folder, "a", "..", "b")}).root, os.path.join(folder, "b", "feishu"))
        for bad in ["user/alice", os.path.splitdrive(folder)[0] + os.sep, f"{folder}\0x"]:
            with self.assertRaises(BridgeError) as caught:
                resolve_data_paths({"AGENT_USER_DIR": bad})
            self.assertEqual(caught.exception.kind, "config", bad)
        # Without the host variable the login is the one of whoever uses this computer, in the home folder.
        self.assertEqual(resolve_data_paths({}, folder).root, os.path.join(folder, ".agent-skills", "user", "feishu"))
        self.assertEqual(resolve_data_paths({}, folder).mode, "standalone")
        self.assertEqual(expected_open_id({"AGENT_USER_OPEN_ID": "ou_abc123"}), "ou_abc123")
        self.assertIsNone(expected_open_id({}))
        for bad in ("../ou", "ou abc"):
            with self.assertRaises(BridgeError):
                expected_open_id({"AGENT_USER_OPEN_ID": bad})

    def test_bridge_commands_cannot_name_another_user(self) -> None:
        # The user is whoever the environment says.
        alice_dir, bob_dir = temp_dir("fs-iso-"), temp_dir("fs-iso-")
        login(self.mock, alice_dir, "ou_alice")
        as_bob = BridgeContext(env={"AGENT_USER_DIR": bob_dir, "FEISHU_APP_ID": self.mock.app_id, "FEISHU_APP_SECRET": self.mock.app_secret},
                               endpoints=self.mock.endpoints)
        # Extra fields that look like an identity are simply not read.
        status = run_bridge("auth-status", {"user_dir": alice_dir, "open_id": "ou_alice", "AGENT_USER_DIR": alice_dir}, as_bob)
        self.assertEqual(status, {"ok": True, "data": {"logged_in": False, "app_configured": True, "storage": "host"}})
        read = run_bridge("api-get", {"path": "/open-apis/wiki/v2/spaces/get_node", "params": {"token": "x"}, "user_dir": alice_dir}, as_bob)
        self.assertEqual((read["ok"], read["error"]["kind"]), (False, "auth_required"))

    def test_error_text_that_leaves_the_bridge_never_contains_the_app_secret_or_a_token(self) -> None:
        folder = temp_dir("fs-iso-")
        login(self.mock, folder, "ou_alice")
        env = {"AGENT_USER_DIR": folder, "FEISHU_APP_ID": self.mock.app_id, "FEISHU_APP_SECRET": self.mock.app_secret}
        self.mock.write_reply = (200, {"code": 1, "msg": f"boom {self.mock.app_secret} Bearer {self.mock.current_access} {self.mock.current_refresh}"})
        reply = run_bridge("sheets-cells-set", {"token": "shtABCDEFGH", "writes": [{"sheet_id": "s1", "range": "A1:A1", "cells": [[{"value": "x"}]],
                                                                                    "allow_overwrite": False}]}, BridgeContext(env=env, endpoints=self.mock.endpoints))
        text = json.dumps(reply, ensure_ascii=False)
        self.assertIs(reply["ok"], False)
        for secret in (self.mock.app_secret, self.mock.current_access, self.mock.current_refresh):
            self.assertNotIn(secret, text)

    def test_redaction(self) -> None:
        self.assertEqual(redact("Authorization: bearer abc.def-1 and u-" + "a" * 20 + " x", []), "Authorization: bearer [REDACTED] and [REDACTED] x")
        self.assertEqual(redact("k=secret-value", ["secret-value", "short"]), "k=[REDACTED]")


if __name__ == "__main__":
    unittest.main()
