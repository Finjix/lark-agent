from __future__ import annotations

import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tests.helpers import ended_pid, now_ms, temp_dir

from feishu_sdk.browser import lock_profile, open_browser
from feishu_sdk.errors import Blocked


def lock_of(profile_dir: str) -> str:
    return f"{os.path.abspath(profile_dir)}.lock"


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


class FakeBrowser:
    """Answers /json/version like a browser, but its DevTools socket cannot be joined."""

    def __init__(self, endpoint: str) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                if self.path == "/json/version":
                    data = json.dumps({"Browser": "Chrome", "webSocketDebuggerUrl": f"ws://127.0.0.1:{outer.port}{endpoint}"}).encode()
                    self.send_response(200)
                else:
                    data = b""
                    self.send_response(404)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def profile() -> str:
    return os.path.join(temp_dir("fs-lock-"), "browser-profile")


class BrowserLockTest(unittest.TestCase):
    def test_a_profile_is_used_by_one_command_at_a_time(self) -> None:
        # Another process holding it is waited for, and given up on with a clear reason.
        profile_dir = profile()
        os.makedirs(os.path.dirname(profile_dir), exist_ok=True)
        write(lock_of(profile_dir), json.dumps({"pid": os.getpid(), "at": now_ms()}))
        with self.assertRaisesRegex(Blocked, "另一个命令使用"):
            lock_profile(profile_dir, 300)
        threading.Timer(0.3, lambda: os.remove(lock_of(profile_dir))).start()
        release = lock_profile(profile_dir, 5_000)
        self.assertTrue(os.path.exists(lock_of(profile_dir)), "held now by this process")
        lock = read_json(lock_of(profile_dir))
        self.assertEqual(sorted(lock), ["at", "pid", "token"])
        release()
        self.assertFalse(os.path.exists(lock_of(profile_dir)), "and let go of")

    def test_a_lock_left_by_a_command_that_has_ended_or_half_written_long_ago_is_taken_over(self) -> None:
        profile_dir = profile()
        os.makedirs(os.path.dirname(profile_dir), exist_ok=True)
        write(lock_of(profile_dir), json.dumps({"pid": ended_pid(), "at": now_ms()}))
        lock_profile(profile_dir, 1_000)()
        write(lock_of(profile_dir), "")
        old = time.time() - 60
        os.utime(lock_of(profile_dir), (old, old))
        lock_profile(profile_dir, 1_000)()

    def test_within_one_process_the_lock_is_shared_and_let_go_of_only_when_every_holder_has(self) -> None:
        profile_dir = profile()
        first = lock_profile(profile_dir, 1_000)
        second = lock_profile(profile_dir, 1_000)
        first()
        first()
        self.assertTrue(os.path.exists(lock_of(profile_dir)), "still held by the second")
        second()
        self.assertFalse(os.path.exists(lock_of(profile_dir)))

    def test_a_browser_that_is_running_but_slow_to_answer_is_not_taken_for_gone(self) -> None:
        # No second one is started and its port file stays.
        fake = FakeBrowser("/devtools/browser/abc")
        self.addCleanup(fake.stop)
        profile_dir = profile()
        os.makedirs(profile_dir, exist_ok=True)
        port_file = os.path.join(profile_dir, "DevToolsActivePort")
        write(port_file, f"{fake.port}\n/devtools/browser/abc")
        with self.assertRaisesRegex(Blocked, "调试接口没有响应"):
            open_browser(os.path.join(profile_dir, "no-such-browser.exe"), profile_dir)
        self.assertTrue(os.path.exists(port_file), "the running browser can still be found")
        self.assertFalse(os.path.exists(lock_of(profile_dir)), "and the refusal let go of the lock")

    def test_the_port_file_of_a_browser_that_is_gone_is_no_obstacle(self) -> None:
        # The start is tried.
        profile_dir = profile()
        os.makedirs(profile_dir, exist_ok=True)
        fake = FakeBrowser("/x")
        port = fake.port
        fake.stop()
        write(os.path.join(profile_dir, "DevToolsActivePort"), f"{port}\n/devtools/browser/abc")
        with self.assertRaisesRegex(Blocked, "浏览器没有启动成功"):
            open_browser(os.path.join(profile_dir, "no-such-browser.exe"), profile_dir)
        self.assertFalse(os.path.exists(lock_of(profile_dir)))

    def test_a_lock_its_holder_stopped_renewing_is_taken_over_though_its_process_is_alive(self) -> None:
        # Windows gives a dead one's id to others.
        profile_dir = profile()
        os.makedirs(os.path.dirname(profile_dir), exist_ok=True)
        write(lock_of(profile_dir), json.dumps({"pid": os.getpid(), "at": now_ms() - 3 * 60_000}))
        lock_profile(profile_dir, 1_000)()
        self.assertFalse(os.path.exists(lock_of(profile_dir)))

    def test_a_holder_lets_go_of_its_own_lock_only_and_an_unreadable_lock_is_waited_on_in_time(self) -> None:
        profile_dir = profile()
        release = lock_profile(profile_dir, 1_000)
        # Taken over meanwhile as abandoned: the old holder must not remove the new holder's lock.
        write(lock_of(profile_dir), json.dumps({"pid": os.getpid(), "at": now_ms(), "token": "theirs"}))
        release()
        self.assertEqual(read_json(lock_of(profile_dir))["token"], "theirs")
        os.remove(lock_of(profile_dir))
        os.mkdir(lock_of(profile_dir))
        started = time.monotonic()
        with self.assertRaises(Blocked):
            lock_profile(profile_dir, 300)
        self.assertLess(time.monotonic() - started, 3, "waited in time, then refused")
        os.rmdir(lock_of(profile_dir))

    def test_a_port_file_whose_port_another_browser_answers_on_by_now_is_stale(self) -> None:
        # The start is tried.
        fake = FakeBrowser("/devtools/browser/someone-else")
        self.addCleanup(fake.stop)
        profile_dir = profile()
        os.makedirs(profile_dir, exist_ok=True)
        write(os.path.join(profile_dir, "DevToolsActivePort"), f"{fake.port}\n/devtools/browser/abc")
        with self.assertRaisesRegex(Blocked, "浏览器没有启动成功"):
            open_browser(os.path.join(profile_dir, "no-such-browser.exe"), profile_dir)
        self.assertFalse(os.path.exists(lock_of(profile_dir)))


if __name__ == "__main__":
    unittest.main()
