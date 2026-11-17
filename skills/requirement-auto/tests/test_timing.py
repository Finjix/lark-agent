"""The timing log: phases, the per-command line, the report, and that the command line writes it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from tests.helpers import SCRIPTS, temp_dir

from gof import timing


class TimingTests(unittest.TestCase):
    def setUp(self) -> None:
        timing.reset()

    def test_phases_add_up_per_name(self) -> None:
        with timing.phase("feishu:sheets-export"):
            pass
        with timing.phase("feishu:sheets-export"):
            pass
        timing.add("xlsx:parse", 0.25)
        entry = timing.snapshot("feishu append", "ok", 0)
        self.assertEqual(entry["command"], "feishu append")
        self.assertEqual(entry["phases"]["feishu:sheets-export"]["count"], 2)
        self.assertEqual(entry["phases"]["xlsx:parse"], {"count": 1, "ms": 250, "max_ms": 250})

    def test_a_failing_phase_is_still_timed(self) -> None:
        with self.assertRaises(ValueError):
            with timing.phase("http GET x"):
                raise ValueError("no")
        self.assertIn("http GET x", timing.snapshot("cls run", None, 2)["phases"])

    def test_names_hold_no_ids_or_arguments(self) -> None:
        self.assertEqual(timing.http_name("get", "https://creative.centurygames.cn/api/workflow/script/12345/detail?id=9"),
                         "http GET creative.centurygames.cn/api/workflow/script/:id/detail")
        self.assertEqual(timing.command_name(["cls", "--batch", "/x/y"]), "cls")
        self.assertEqual(timing.command_name(["feishu", "append", "--batch", "/x"]), "feishu append")

    def test_report_gaps_and_totals(self) -> None:
        entries = [
            {"command": "feishu snapshot", "status": "ok", "start": 1000.0, "total_ms": 20_000, "phases": {"feishu:sheets-export": {"count": 1, "ms": 18_000, "max_ms": 18_000}}},
            {"command": "cls run", "status": "ok", "start": 1030.0, "total_ms": 15_000, "phases": {"cls:token_via_browser": {"count": 1, "ms": 12_000, "max_ms": 12_000}}},
            {"command": "cls run", "status": "ok", "start": 5000.0, "total_ms": 14_000, "phases": {"cls:token_via_browser": {"count": 1, "ms": 11_000, "max_ms": 11_000}}},
        ]
        out = timing.report(entries)
        self.assertEqual(out["commands_ms"], 49_000)
        self.assertEqual(out["agent_ms"], 10_000)  # 1020 → 1030; the hour-long pause before the third is left out
        self.assertEqual(out["runs"][1]["gap_ms"], 10_000)
        self.assertNotIn("gap_ms", out["runs"][2])
        self.assertEqual(out["by_command"]["cls run"], {"count": 2, "ms": 29_000, "max_ms": 15_000})
        self.assertEqual(list(out["by_phase"])[0], "cls:token_via_browser")
        self.assertEqual(len(timing.report(entries, last=1)["runs"]), 1)
        self.assertEqual(len(timing.report(entries, since=4000)["runs"]), 1)

    def test_log_is_cut_back_when_large(self) -> None:
        root = temp_dir("timing-")
        with open(timing.log_file(root), "w", encoding="utf-8") as handle:
            line = json.dumps({"command": "x", "start": 1.0, "total_ms": 1, "pad": "p" * 200}) + "\n"
            handle.write(line * (timing.MAX_BYTES // len(line) + 10))
        timing.write(root, {"command": "new", "start": 2.0, "total_ms": 1})
        self.assertLess(os.path.getsize(timing.log_file(root)), timing.MAX_BYTES)
        entries = timing.read(root)
        self.assertEqual(entries[-1]["command"], "new")

    def test_command_line_writes_the_log_and_reports_it(self) -> None:
        user = temp_dir("timing-user-")
        env = {**os.environ, "AGENT_USER_DIR": user}
        env.pop("AGENT_USER_OPEN_ID", None)
        gof = os.path.join(SCRIPTS, "gof.py")
        subprocess.run([sys.executable, gof, "batch", "list"], env=env, capture_output=True, check=False)
        entries = timing.read(os.path.join(user, "requirement-auto"))
        self.assertEqual([e["command"] for e in entries], ["batch list"])
        answer = subprocess.run([sys.executable, gof, "timing", "--last", "5"], env=env, capture_output=True, check=True)
        report = json.loads(answer.stdout)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["runs"][0]["command"], "batch list")
        self.assertEqual(len(timing.read(os.path.join(user, "requirement-auto"))), 1)  # `timing` itself is not logged


if __name__ == "__main__":
    unittest.main()
