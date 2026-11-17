import json
import os
import subprocess
import sys
import time
import unittest

from audit_sdk.cli import Context, main
from audit_sdk.config import skill_root
from audit_sdk.errors import Blocked
from audit_sdk.jsutil import now_ms
from audit_sdk.prune import prune
from audit_sdk.rules import dump

from tests import SKILL
from tests.helpers.fakes import FakeLark, FakeModel
from tests.helpers.tmp import temp_dir

SCRIPT = os.path.join(SKILL, "scripts", "audit.py")
DAY = 24 * 60 * 60


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_", "AUDIT_AUTO_", "FEISHU_"))}


def run_script(args: list[str], env: dict[str, str], cwd: str | None = None) -> tuple[dict, int, str, str]:
    done = subprocess.run([sys.executable, SCRIPT, *args], env={**clean_env(), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **env}, cwd=cwd,
                          capture_output=True, timeout=120)
    out = done.stdout.decode("utf-8")
    return json.loads(out.strip().splitlines()[-1]), done.returncode, done.stderr.decode("utf-8"), out


def write(path: str, data: str = "x", ahead_days: float = 0) -> str:
    """A file; `ahead_days` dates its modification into the future (a creation time cannot be set back, and prune counts it)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(data)
    if ahead_days:
        at = time.time() + ahead_days * DAY
        os.utime(path, (at, at))
    return path


def link_dir(target: str, link: str) -> bool:
    """A directory link (a junction on Windows, a symbolic link elsewhere); False when this system does not allow one."""
    try:
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(target, link)
        else:
            os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, ImportError, AttributeError):
        return False


class CliTest(unittest.TestCase):
    def test_doctor_checks_specs_gateway_feishu_app_shared_folder_ffmpeg_and_groups_and_says_which_fails(self) -> None:
        env = {"AGENT_SHARED_DIR": temp_dir("host-"), "AUDIT_AUTO_GEMINI_KEY": "k"}
        healthy, code = main(["doctor"], Context(env=env, model=FakeModel(lambda _r, _n: {"ok": True}), lark=FakeLark(), ffmpeg=None))
        self.assertEqual(code, 0)
        self.assertEqual([(c["check"], c["ok"]) for c in healthy["checks"]],
                         [("fixed_specs", True), ("ai_gateway", True), ("feishu_app", True), ("shared_folder", True), ("ffmpeg", True), ("groups", True)])
        self.assertEqual(healthy["checks"][0]["detail"], "GOF: yes, GOT: yes")
        lark = FakeLark()
        lark.fail_with = Blocked("The Feishu app's credentials are not valid (code 10003): ask the administrator")
        refusing = FakeModel(lambda _r, _n: Blocked("The AI gateway does not accept the key (HTTP 401): ask the administrator"))
        sick, code = main(["doctor"], Context(env=env, model=refusing, lark=lark, ffmpeg=None))
        self.assertEqual(code, 2)
        self.assertEqual(sick["status"], "problems")
        self.assertEqual([(c["check"], c.get("detail")) for c in sick["checks"] if not c["ok"]],
                         [("ai_gateway", "The AI gateway does not accept the key (HTTP 401): ask the administrator"),
                          ("feishu_app", "The Feishu app's credentials are not valid (code 10003): ask the administrator")])
        # Without the key in the environment the gateway check says so, for the administrator.
        keyless, _ = main(["doctor"], Context(env={"AGENT_SHARED_DIR": env["AGENT_SHARED_DIR"]}, lark=FakeLark(), ffmpeg=None))
        gateway = next(c for c in keyless["checks"] if c["check"] == "ai_gateway")
        self.assertFalse(gateway["ok"])
        self.assertIn("AUDIT_AUTO_GEMINI_KEY", gateway["detail"])

    def test_info_tells_what_is_configured_never_the_key(self) -> None:
        shared, user = temp_dir("host-"), temp_dir("user-")
        result, code = main(["info"], Context(env={"AGENT_SHARED_DIR": shared, "AGENT_USER_DIR": user, "AUDIT_AUTO_GEMINI_KEY": "sk-secret-key"}))
        self.assertEqual(code, 0)
        self.assertEqual(result["model"], "gemini-3.8-flash (thinking medium) @ https://lumos.diandian.info/winky/gemini/v1")
        self.assertEqual(result["shared"], os.path.join(os.path.abspath(shared), "audit-auto"))
        self.assertEqual(result["user"], os.path.join(os.path.abspath(user), "audit-auto"))
        self.assertEqual(result["version"], "3.1.0")
        self.assertEqual(result["projects"], [{"id": "GOF", "fixed_spec": True, "dynamic_rules": 0}, {"id": "GOT", "fixed_spec": True, "dynamic_rules": 0}])
        self.assertNotIn("sk-secret-key", json.dumps(result))
        missing, _ = main(["info"], Context(env={"AGENT_SHARED_DIR": shared}, home=temp_dir("home-")))
        self.assertEqual((missing["model"], missing["gateway_key"]), (None, False))

    def test_the_script_prints_one_line_of_json_and_exits_0_or_2(self) -> None:
        shared = temp_dir("host-")
        listed, code, _, _ = run_script(["rules", "status"], {"AGENT_SHARED_DIR": shared})
        self.assertEqual((code, listed["status"], listed["rules"]), (0, "ok", {"GOF": 0, "GOT": 0}))
        refused, code, _, _ = run_script(["review", "--file", "a.mp4"], {"AGENT_SHARED_DIR": shared})
        self.assertEqual((code, refused), (2, {"status": "blocked", "reason": "--project is missing (GOF or GOT): ask the user which project the material is for"}))
        # A review without the gateway key: refused with what to do, before anything is read.
        user = temp_dir("user-")
        keyless, code, _, _ = run_script(["review", "--project", "GOF", "--file", "input/a.mp4"], {"AGENT_SHARED_DIR": shared, "AGENT_USER_DIR": user})
        self.assertEqual((code, keyless["status"]), (2, "blocked"))
        self.assertIn("ask the administrator", keyless["reason"])

    def test_the_periodic_job_as_a_host_runs_it_gives_way_to_a_reading_in_progress_with_exit_0(self) -> None:
        host = temp_dir("host-")
        shared = os.path.join(host, "audit-auto")
        os.makedirs(shared)
        # This (living) process holds the lock, as a user's watch would.
        with open(os.path.join(shared, "lock"), "w", encoding="utf-8") as handle:
            handle.write(dump({"pid": os.getpid(), "token": "watch", "at": now_ms()}, pretty=False))
        env = {"AGENT_SHARED_DIR": host, "FEISHU_APP_ID": "cli_test", "FEISHU_APP_SECRET": "secret", "AUDIT_AUTO_GEMINI_KEY": "k",
               "AUDIT_AUTO_FEISHU_BASE": "http://127.0.0.1:9", "AUDIT_AUTO_GEMINI_BASE_URL": "http://127.0.0.1:9"}
        # As jobs.json says: `python scripts/audit.py rules sync` in the skill's folder.
        done = subprocess.run([sys.executable, "scripts/audit.py", "rules", "sync"], cwd=SKILL, env={**clean_env(), **env, "PYTHONIOENCODING": "utf-8"},
                              capture_output=True, timeout=120)
        result = json.loads(done.stdout.decode("utf-8"))
        self.assertEqual((done.returncode, result["status"]), (0, "busy"))
        self.assertIn("being updated", result["reason"])
        with open(os.path.join(shared, "lock"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["token"], "watch", "the holder's lock is left alone")

    def test_prune_deletes_stale_scratch_and_reviews_and_the_folders_left_empty_never_the_shared_folder(self) -> None:
        user = temp_dir("user-")
        shared = temp_dir("shared-")
        legacy = temp_dir("legacy-")
        data = os.path.join(user, "audit-auto")
        # Pruned as if 20 days from now: what is touched now is stale, what is modified 30 days ahead is fresh.
        later = time.time() + 20 * DAY
        stale = [write(os.path.join(data, "tmp", "run-old", "zip-1", "01-a.mp4"), "12345"), write(os.path.join(data, "reviews", "old.json"), "{}"),
                 write(os.path.join(data, "reviews", "old-images", "a.png"), "png"), write(os.path.join(data, "reviews", "mixed", "old.png")),
                 write(os.path.join(legacy, "run-left", "x.mp4"), "xx")]
        fresh = [write(os.path.join(data, "tmp", "run-now", "01-b.mp4"), "x", 30), write(os.path.join(data, "reviews", "new.md"), "# new", 30),
                 write(os.path.join(data, "reviews", "mixed", "new.png"), "x", 30), write(os.path.join(data, "reviews", "aged.md"), "x", 9),
                 write(os.path.join(legacy, "not-ours", "y")), write(os.path.join(shared, "audit-auto", "groups.json"), "{}"),
                 write(os.path.join(data, "groups.json"), "{}")]
        os.makedirs(os.path.join(data, "tmp", "run-empty"))
        # A link to a folder outside: never followed, the stale link itself goes.
        outside = temp_dir("outside-")
        precious = write(os.path.join(outside, "precious.txt"), "keep")
        link = os.path.join(data, "tmp", "link")
        linked = link_dir(outside, link)

        result = prune({"AGENT_USER_DIR": user, "AGENT_SHARED_DIR": shared}, now=later, legacy=legacy)
        self.assertEqual(result, {"status": "ok", "files": len(stale) + (1 if linked else 0), "bytes": 5 + 2 + 3 + 1 + 2})
        for path in stale:
            self.assertFalse(os.path.exists(path), path)
        for path in fresh:
            self.assertTrue(os.path.exists(path), path)
        for gone in (os.path.join(data, "tmp", "run-old"), os.path.join(data, "reviews", "old-images"), os.path.join(data, "tmp", "run-empty"),
                     os.path.join(legacy, "run-left")):
            self.assertFalse(os.path.exists(gone), f"{gone}: a folder left empty, or empty and stale, goes")
        self.assertTrue(os.path.isdir(os.path.join(data, "reviews", "mixed")), "a folder with something fresh in it stays")
        self.assertTrue(os.path.isdir(os.path.join(data, "tmp")) and os.path.isdir(os.path.join(data, "reviews")), "the roots stay")
        self.assertTrue(os.path.exists(precious), "a link is never followed")
        self.assertFalse(os.path.lexists(link))
        # Nothing stale left: nothing deleted. A shorter retention takes the one modified 9 days ahead too.
        self.assertEqual(prune({"AGENT_USER_DIR": user}, now=later, legacy=legacy), {"status": "ok", "files": 0, "bytes": 0})
        self.assertEqual(prune({"AGENT_USER_DIR": user, "AUDIT_AUTO_RETENTION_DAYS": "10"}, now=later, legacy=legacy)["files"], 1)
        self.assertFalse(os.path.exists(os.path.join(data, "reviews", "aged.md")))

    def test_prune_prints_one_line_of_json_as_a_host_runs_it_and_refuses_a_bad_setting(self) -> None:
        user = temp_dir("user-")
        write(os.path.join(user, "audit-auto", "tmp", "run-1", "a.mp4"), "abc")
        env = {"AGENT_USER_DIR": user, "TEMP": temp_dir("temp-"), "TMP": temp_dir("temp-"), "TMPDIR": temp_dir("temp-")}
        result, code, _, out = run_script(["prune"], env, cwd=SKILL)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), '{"status":"ok","files":0,"bytes":0}', "one line, compact; a file just written is not stale")
        self.assertTrue(os.path.exists(os.path.join(user, "audit-auto", "tmp", "run-1", "a.mp4")))
        refused, code, _, _ = run_script(["prune"], {**env, "AUDIT_AUTO_RETENTION_DAYS": "0"}, cwd=SKILL)
        self.assertEqual((code, refused["status"]), (2, "blocked"))
        self.assertIn("AUDIT_AUTO_RETENTION_DAYS", refused["reason"])
        relative, code, _, _ = run_script(["prune"], {**env, "AGENT_USER_DIR": "data/user/x"}, cwd=SKILL)
        self.assertEqual((code, relative["status"]), (2, "blocked"))
        self.assertIn("absolute", relative["reason"])
        self.assertIn("Unknown option", main(["prune", "--all"])[0]["reason"])

    def test_the_skill_md_is_a_valid_agent_skill_with_the_prune_hook(self) -> None:
        with open(os.path.join(skill_root({}), "SKILL.md"), encoding="utf-8") as handle:
            head = handle.read().split("---")[1]
        lines = head.splitlines()
        name = next(line.split(":", 1)[1].strip() for line in lines if line.startswith("name:"))
        description = json.loads(next(line.split(":", 1)[1].strip() for line in lines if line.startswith("description:")))
        self.assertEqual(name, "audit-auto")
        self.assertLessEqual(len(description), 1024)
        self.assertIn("「监听本群更新审片规范」", description)
        self.assertIn('  prune: "scripts/audit.py prune"', lines)
        self.assertTrue(os.path.isfile(os.path.join(SKILL, "scripts", "audit.py")))
        for reference in ("review.md", "dynamic-rules.md", "scheduled-sync.md", "gof-rules.md", "got-rules.md"):
            self.assertTrue(os.path.isfile(os.path.join(SKILL, "references", reference)), reference)


if __name__ == "__main__":
    unittest.main()
