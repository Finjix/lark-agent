"""The command line as a skill runs it: a separate process with that skill's environment (whose user is the host's)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from tests import ENTRY
from tests.helpers import temp_dir


def env_for(folder: str) -> dict[str, str]:
    return {**os.environ, "LARK_AGENT_USER_DIR": folder, "FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "secret-000000", "LARK_AGENT_USER_OPEN_ID": ""}


def run(args: list[str], env: dict[str, str], stdin: str | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([sys.executable, ENTRY, *args], env=env, input=(stdin or "").encode("utf-8"), capture_output=True, timeout=60)


class ServeTest(unittest.TestCase):
    def test_serve_answers_one_request_per_line_with_its_id_and_ends_when_its_input_does(self) -> None:
        folder = temp_dir("fs-serve-")
        lines = [json.dumps({"id": 1, "command": "info"}), json.dumps({"id": "b", "command": "auth-status"}), "not json", "",
                 json.dumps({"id": 3, "command": "rm-rf"}) + "\r"]
        done = run(["serve"], env_for(folder), "\n".join(lines) + "\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        out = done.stdout.decode("utf-8")
        self.assertNotIn("\r\n", out)
        replies = {}
        for line in out.strip().split("\n"):
            message = json.loads(line)
            replies[message.get("id", "<none>")] = message["reply"]
        self.assertEqual(len(replies), 4)
        self.assertEqual(replies[1]["data"]["browser_profile"], os.path.join(folder, "feishu", "browser-profile"))
        self.assertIs(replies["b"]["data"]["logged_in"], False)
        self.assertEqual(replies["<none>"]["error"]["kind"], "invalid_request")
        self.assertEqual(replies[3]["error"]["kind"], "invalid_request")
        self.assertNotIn("secret-000000", out)

    def test_bridge_runs_one_command_with_its_input_from_stdin_and_human_commands_exit_2_when_refused(self) -> None:
        folder = temp_dir("fs-cli-")
        bridge = run(["bridge", "auth-check-scopes"], env_for(folder), '{"required":["docx:document:readonly"]}')
        self.assertEqual(bridge.returncode, 0)
        self.assertEqual(json.loads(bridge.stdout), {"ok": True, "data": {"complete": False, "granted": [], "missing": ["docx:document:readonly"]}})
        bad_json = run(["bridge", "api-get"], env_for(folder), "{nope")
        self.assertEqual((bad_json.returncode, json.loads(bad_json.stdout)["error"]["message"]), (2, "标准输入不是有效的 JSON"))
        # A command without input does not wait for stdin: it answers with stdin left open.
        info = subprocess.run([sys.executable, ENTRY, "bridge", "info"], env=env_for(folder), stdin=subprocess.PIPE, capture_output=True, timeout=30)
        self.assertEqual(json.loads(info.stdout)["data"]["bridge"], "feishu-sdk-bridge/1")
        bad = run(["login", "nope"], env_for(folder))
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(json.loads(bad.stdout)["status"], "blocked")
        shared = run(["info"], {**os.environ, "LARK_AGENT_USER_OPEN_ID": "ou_x", "LARK_AGENT_USER_DIR": ""})
        self.assertEqual(shared.returncode, 2, "a user named without a directory is refused, not served from a shared folder")
        self.assertEqual(json.loads(shared.stdout)["status"], "blocked")

    def test_a_line_that_is_json_but_no_request_is_answered_as_an_invalid_request_and_serve_goes_on(self) -> None:
        folder = temp_dir("fs-serve-")
        done = run(["serve"], env_for(folder), "null\n[1]\n" + json.dumps({"id": 9, "command": "auth-status"}) + "\n" + json.dumps({"id": None}) + "\n")
        replies = [json.loads(line) for line in done.stdout.decode("utf-8").strip().split("\n")]
        self.assertEqual(len(replies), 4)
        self.assertEqual(len([r for r in replies if not r["reply"]["ok"] and r["reply"]["error"]["kind"] == "invalid_request"]), 3)
        self.assertTrue(any(r.get("id") == 9 and r["reply"]["ok"] for r in replies))
        self.assertTrue(any("id" in r and r["id"] is None for r in replies), "an id of null is given back as null")


if __name__ == "__main__":
    unittest.main()
