import json
import os
import shutil
import subprocess
import sys
import unittest

from audit_sdk.cli import Context, main
from audit_sdk.config import Turn, skill_root
from audit_sdk.errors import Blocked
from audit_sdk.jsutil import now_ms
from audit_sdk.rules import dump, load_book, render_book, status

from tests import SKILL
from tests.helpers.fakes import FakeLark, FakeModel
from tests.helpers.tmp import temp_dir

SCRIPT = os.path.join(SKILL, "scripts", "audit.py")
#: lark-agent's own shared folder, when this skill sits in lark-agent/skills/: read (copied), never written.
REAL_SHARED = os.path.normpath(os.path.join(SKILL, "..", "..", "data", "shared", "audit-auto"))


def run_script(args: list[str], env: dict[str, str], cwd: str | None = None) -> tuple[dict, int, str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("LARK_AGENT_", "AUDIT_AUTO_", "FEISHU_"))}
    done = subprocess.run([sys.executable, SCRIPT, *args], env={**clean, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **env}, cwd=cwd,
                          capture_output=True, timeout=120)
    lines = done.stdout.decode("utf-8").strip().splitlines()
    return json.loads(lines[-1]), done.returncode, done.stderr.decode("utf-8")


class CliTest(unittest.TestCase):
    def test_doctor_checks_specs_gateway_feishu_app_shared_folder_ffmpeg_and_groups_and_says_which_fails(self) -> None:
        env = {"LARK_AGENT_SHARED_DIR": temp_dir("host-"), "AUDIT_AUTO_GEMINI_KEY": "k"}
        healthy, code = main(["doctor"], Context(env=env, model=FakeModel(lambda _r, _n: {"ok": True}), lark=FakeLark(), ffmpeg=None))
        self.assertEqual(code, 0)
        self.assertEqual([(c["check"], c["ok"]) for c in healthy["checks"]],
                         [("fixed_specs", True), ("ai_gateway", True), ("feishu_app", True), ("shared_folder", True), ("ffmpeg", True), ("groups", True)])
        lark = FakeLark()
        lark.fail_with = Blocked("飞书应用凭证无效（错误码 10003），请联系管理员")
        sick, code = main(["doctor"], Context(env=env, model=FakeModel(lambda _r, _n: Blocked("AI 网关不接受这个 key（HTTP 401），请联系管理员")), lark=lark, ffmpeg=None))
        self.assertEqual(code, 2)
        self.assertEqual(sick["status"], "problems")
        self.assertEqual([(c["check"], c.get("detail")) for c in sick["checks"] if not c["ok"]],
                         [("ai_gateway", "AI 网关不接受这个 key（HTTP 401），请联系管理员"), ("feishu_app", "飞书应用凭证无效（错误码 10003），请联系管理员")])
        # Without the key in the environment the gateway check says so, for the administrator.
        keyless, _ = main(["doctor"], Context(env={"LARK_AGENT_SHARED_DIR": env["LARK_AGENT_SHARED_DIR"]}, lark=FakeLark(), ffmpeg=None))
        gateway = next(c for c in keyless["checks"] if c["check"] == "ai_gateway")
        self.assertFalse(gateway["ok"])
        self.assertIn("AUDIT_AUTO_GEMINI_KEY", gateway["detail"])

    def test_info_tells_what_is_configured_never_the_key(self) -> None:
        shared = temp_dir("host-")
        result, code = main(["info"], Context(env={"LARK_AGENT_SHARED_DIR": shared, "AUDIT_AUTO_GEMINI_KEY": "sk-secret-key"}))
        self.assertEqual(code, 0)
        self.assertEqual(result["model"], "gemini-3.8-flash (thinking medium) @ https://lumos.diandian.info/winky/gemini/v1")
        self.assertEqual(result["shared"], os.path.join(os.path.abspath(shared), "audit-auto"))
        self.assertEqual(result["version"], "3.0.0")
        self.assertEqual(result["projects"], [{"id": "GOF", "fixed_spec": True, "dynamic_rules": 0}, {"id": "GOT", "fixed_spec": True, "dynamic_rules": 0}])
        self.assertNotIn("sk-secret-key", json.dumps(result))
        missing, _ = main(["info"], Context(env={"LARK_AGENT_SHARED_DIR": shared}))
        self.assertEqual((missing["model"], missing["gateway_key"]), (None, False))

    def test_the_script_prints_one_line_of_json_and_exits_0_or_2(self) -> None:
        shared = temp_dir("host-")
        listed, code, _ = run_script(["rules", "status"], {"LARK_AGENT_SHARED_DIR": shared})
        self.assertEqual((code, listed["status"], listed["rules"]), (0, "ok", {"GOF": 0, "GOT": 0}))
        refused, code, _ = run_script(["review", "--file", "a.mp4"], {"LARK_AGENT_SHARED_DIR": shared})
        self.assertEqual((code, refused), (2, {"status": "blocked", "reason": "缺少 --project（GOF 或 GOT）：先问用户这是哪个项目的素材"}))
        # A review without the gateway key: refused with what to do, before anything is read.
        user = temp_dir("user-")
        keyless, code, _ = run_script(["review", "--project", "GOF", "--file", "input/a.mp4"], {"LARK_AGENT_SHARED_DIR": shared, "LARK_AGENT_USER_DIR": user})
        self.assertEqual((code, keyless["status"]), (2, "blocked"))
        self.assertIn("请联系管理员", keyless["reason"])

    def test_the_daily_job_as_lark_agent_runs_it_gives_way_to_a_reading_in_progress_with_exit_0(self) -> None:
        host = temp_dir("host-")
        shared = os.path.join(host, "audit-auto")
        os.makedirs(shared)
        # This (living) process holds the lock, as a user's watch would.
        with open(os.path.join(shared, "lock"), "w", encoding="utf-8") as handle:
            handle.write(dump({"pid": os.getpid(), "token": "watch", "at": now_ms()}, pretty=False))
        project = os.path.dirname(os.path.dirname(SKILL))
        relative = os.path.relpath(SCRIPT, project).replace(os.sep, "/")
        env = {"LARK_AGENT_SHARED_DIR": host, "FEISHU_APP_ID": "cli_test", "FEISHU_APP_SECRET": "secret", "AUDIT_AUTO_GEMINI_KEY": "k",
               "AUDIT_AUTO_FEISHU_BASE": "http://127.0.0.1:9", "AUDIT_AUTO_GEMINI_BASE_URL": "http://127.0.0.1:9"}
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("LARK_AGENT_", "AUDIT_AUTO_", "FEISHU_"))}
        done = subprocess.run([sys.executable, relative, "rules", "sync"], cwd=project, env={**clean, **env, "PYTHONIOENCODING": "utf-8"}, capture_output=True, timeout=120)
        result = json.loads(done.stdout.decode("utf-8"))
        self.assertEqual((done.returncode, result["status"]), (0, "busy"))
        self.assertIn("正在更新", result["reason"])
        with open(os.path.join(shared, "lock"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["token"], "watch", "the holder's lock is left alone")

    @unittest.skipUnless(os.path.isdir(REAL_SHARED), "no lark-agent data/shared/audit-auto next to this skill")
    def test_the_data_lark_agent_already_has_reads_and_renders_exactly_as_the_typescript_version_wrote_it(self) -> None:
        copy = os.path.join(temp_dir("real-"), "audit-auto")
        shutil.copytree(REAL_SHARED, copy, ignore=shutil.ignore_patterns("lock", "*.tmp", "*.stale"))
        rules = os.path.join(copy, "rules")
        for name in sorted(os.listdir(rules) if os.path.isdir(rules) else []):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(rules, name), encoding="utf-8", newline="") as handle:
                text = handle.read()
            book = load_book(copy, name[:-5])
            self.assertEqual(f"{dump(book)}\n", text, f"{name} is written back byte for byte")
            markdown = os.path.join(rules, name[:-5] + ".md")
            if os.path.exists(markdown):
                with open(markdown, encoding="utf-8", newline="") as handle:
                    self.assertEqual(render_book(book), handle.read(), f"{name[:-5]}.md renders the same")
        groups = os.path.join(copy, "groups.json")
        if os.path.exists(groups):
            with open(groups, encoding="utf-8", newline="") as handle:
                text = handle.read()
            self.assertEqual(f"{dump(json.loads(text))}\n", text, "groups.json is written back byte for byte")
            listed = status(copy, Turn())
            self.assertEqual(listed["status"], "ok")
            self.assertEqual(len(listed["groups"]), len(json.loads(text)["groups"]))
        names = os.path.join(copy, "names.json")
        if os.path.exists(names):
            with open(names, encoding="utf-8", newline="") as handle:
                text = handle.read()
            self.assertEqual(f"{dump(json.loads(text), pretty=False)}\n", text, "names.json is written back byte for byte")

    def test_the_skill_md_is_a_valid_agent_skill(self) -> None:
        with open(os.path.join(skill_root({}), "SKILL.md"), encoding="utf-8") as handle:
            head = handle.read().split("---")[1]
        name = next(line.split(":", 1)[1].strip() for line in head.splitlines() if line.startswith("name:"))
        description = json.loads(next(line.split(":", 1)[1].strip() for line in head.splitlines() if line.startswith("description:")))
        self.assertEqual(name, "audit-auto")
        self.assertLessEqual(len(description), 1024)
        for reference in ("gof-audit.md", "got-audit.md", "dynamic-rules.md", "scheduled-sync.md", "gof-rules.md", "got-rules.md"):
            self.assertTrue(os.path.isfile(os.path.join(SKILL, "references", reference)), reference)


if __name__ == "__main__":
    unittest.main()
