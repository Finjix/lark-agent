from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from tests.helpers import ended_pid, now_ms, temp_dir
from tests.helpers.mock_feishu import MockFeishu

from feishu_sdk.auth import FeishuAuth
from feishu_sdk.config import AppCredentials
from feishu_sdk.errors import BridgeError
from feishu_sdk.fsutil import with_lock
from feishu_sdk.paths import resolve_data_paths

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "helpers", "refresh_worker.py")


class RefreshTest(unittest.TestCase):
    def test_several_processes_needing_a_renewal_at_the_same_moment_cause_one_refresh(self) -> None:
        # ... and all get the new token.
        mock = MockFeishu().start()
        self.addCleanup(mock.stop)
        folder = temp_dir("fs-race-")
        auth = FeishuAuth(paths=resolve_data_paths({"LARK_AGENT_USER_DIR": folder}), app=AppCredentials(mock.app_id, mock.app_secret), endpoints=mock.endpoints)
        auth.start_login()
        mock.device_script = ["issue"]
        auth.poll_login(10_000)
        stale = mock.current_access
        mock.refresh_delay_ms = 300  # keeps the first refresh in flight while the others arrive

        # Each child believes it is six hours later, so the stored access token is due for renewal in all of them.
        env = {**os.environ, "MOCK_URL": mock.url, "MOCK_APP_ID": mock.app_id, "MOCK_APP_SECRET": mock.app_secret, "LARK_AGENT_USER_DIR": folder,
               "FAKE_NOW": str(6 * 3600 * 1000)}
        children = [subprocess.Popen([sys.executable, WORKER], env=env, stdout=subprocess.PIPE, text=True) for _ in range(4)]
        replies = [json.loads(child.communicate(timeout=60)[0]) for child in children]
        self.assertTrue(all(reply["ok"] for reply in replies), replies)
        self.assertEqual(mock.refresh_count, 1, "the single-use refresh token was spent once")
        self.assertEqual(len({reply["token"] for reply in replies}), 1)
        self.assertNotEqual(replies[0]["token"], stale)
        self.assertEqual(replies[0]["token"], mock.current_access)
        with open(auth.paths.auth, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["refresh_token"], mock.current_refresh)

    def test_the_lock_is_exclusive_released_after_use_and_a_dead_one_is_taken_over(self) -> None:
        file = os.path.join(temp_dir("fs-lock-"), "x.lock")
        order: list[str] = []
        a_in = threading.Event()

        def a() -> None:
            def body() -> None:
                order.append("a-in")
                a_in.set()
                time.sleep(0.08)
                order.append("a-out")

            with_lock(file, body)

        def b() -> None:
            a_in.wait(5)
            with_lock(file, lambda: (order.append("b-in"), order.append("b-out")))

        with ThreadPoolExecutor(2) as pool:
            for future in [pool.submit(a), pool.submit(b)]:
                future.result()
        self.assertEqual([item[2:] for item in order], ["in", "out", "in", "out"])
        self.assertFalse(os.path.exists(file))

        def write(text: str) -> None:
            with open(file, "w", encoding="utf-8") as handle:
                handle.write(text)

        write('{"pid":0}')
        old = time.time() - 120
        os.utime(file, (old, old))
        self.assertEqual(with_lock(file, lambda: "took over", stale_ms=1000), "took over")

        write('{"pid":0}')
        with self.assertRaises(BridgeError) as caught:
            with_lock(file, lambda: "never", timeout_ms=150, stale_ms=60_000)
        self.assertEqual(caught.exception.kind, "network")

        # Left by a process that has ended (killed while renewing, say): taken over at once, not only once it has gone stale.
        write(json.dumps({"pid": ended_pid(), "at": now_ms(), "owner": "gone"}))
        self.assertEqual(with_lock(file, lambda: "took over", timeout_ms=500, stale_ms=60_000), "took over")


if __name__ == "__main__":
    unittest.main()
