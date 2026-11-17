import os
import unittest

from audit_sdk.config import (Turn, feishu_app, gateway, gateway_key, retention_days, reviews_dir, scratch_root, shared_dir, skill_root, turn_of,
                              user_dir)
from audit_sdk.errors import Blocked
from audit_sdk.projects import PROJECTS, fixed_spec, project_of
from audit_sdk.rules import CATEGORIES


class ConfigTest(unittest.TestCase):
    def test_the_gateway_key_comes_from_the_environment_only(self) -> None:
        self.assertEqual(gateway_key({"AUDIT_AUTO_GEMINI_KEY": " sk-gateway-é "}), "sk-gateway-é")
        # No key: the user is told to ask the administrator.
        with self.assertRaisesRegex(Blocked, "AUDIT_AUTO_GEMINI_KEY is not set.*administrator"):
            gateway_key({"CODEX_HOME": "D:/somewhere/.codex"})
        with self.assertRaisesRegex(Blocked, "administrator"):
            gateway_key({"AUDIT_AUTO_GEMINI_KEY": "  "})

    def test_the_gateway_defaults_to_gemini_flash_thinking_at_medium_overrides_are_checked(self) -> None:
        g = gateway({"AUDIT_AUTO_GEMINI_KEY": "k"})
        self.assertEqual((g.base_url, g.api_version, g.model, g.thinking_level, g.key),
                         ("https://lumos.diandian.info/winky/gemini", "v1", "gemini-3.8-flash", "MEDIUM", "k"))
        custom = gateway({"AUDIT_AUTO_GEMINI_KEY": "k", "AUDIT_AUTO_GEMINI_MODEL": "gemini-x", "AUDIT_AUTO_GEMINI_THINKING": "high",
                          "AUDIT_AUTO_GEMINI_BASE_URL": "http://127.0.0.1:9/g/"})
        self.assertEqual((custom.model, custom.thinking_level, custom.base_url), ("gemini-x", "HIGH", "http://127.0.0.1:9/g"))
        with self.assertRaisesRegex(Blocked, "must be minimal, low, medium or high"):
            gateway({"AUDIT_AUTO_GEMINI_KEY": "k", "AUDIT_AUTO_GEMINI_THINKING": "max"})

    def test_folders_and_the_turn_come_from_the_host_with_defaults_under_agent_skills(self) -> None:
        self.assertEqual(shared_dir({"AGENT_SHARED_DIR": r"D:\agent\shared"}), os.path.join(os.path.abspath(r"D:\agent\shared"), "audit-auto"))
        self.assertEqual(shared_dir({}, r"C:\home"), os.path.join(r"C:\home", ".agent-skills", "shared", "audit-auto"))
        user = os.path.join(os.path.abspath(r"D:\agent\user\李小明"), "audit-auto")
        self.assertEqual(user_dir({"AGENT_USER_DIR": r"D:\agent\user\李小明"}), user)
        self.assertEqual(scratch_root({"AGENT_USER_DIR": r"D:\agent\user\李小明"}), os.path.join(user, "tmp"))
        self.assertEqual(reviews_dir({"AGENT_USER_DIR": r"D:\agent\user\李小明"}), os.path.join(user, "reviews"))
        # Without a user directory, scratch still goes to the default user folder, never to the working directory.
        self.assertEqual(scratch_root({}, r"C:\home"), os.path.join(r"C:\home", ".agent-skills", "user", "audit-auto", "tmp"))
        self.assertEqual(turn_of({"AGENT_CHAT_ID": "oc_1", "AGENT_CHAT_TYPE": "group", "AGENT_USER_OPEN_ID": "ou_1"}),
                         Turn(chat="oc_1", chat_type="group", user="ou_1"))
        self.assertEqual(turn_of({"AGENT_CHAT_ID": " "}), Turn())

    def test_the_retention_is_a_whole_number_of_days_14_by_default(self) -> None:
        self.assertEqual(retention_days({}), 14)
        self.assertEqual(retention_days({"AUDIT_AUTO_RETENTION_DAYS": " 3 "}), 3)
        for wrong in ("0", "-1", "2.5", "a week"):
            with self.assertRaisesRegex(Blocked, "AUDIT_AUTO_RETENTION_DAYS"):
                retention_days({"AUDIT_AUTO_RETENTION_DAYS": wrong})

    def test_the_app_comes_from_the_environment(self) -> None:
        app = feishu_app({"FEISHU_APP_ID": "cli_1", "FEISHU_APP_SECRET": "s"})
        self.assertEqual((app.id, app.secret, app.base), ("cli_1", "s", "https://open.feishu.cn/open-apis"))
        with self.assertRaisesRegex(Blocked, "FEISHU_APP_ID"):
            feishu_app({})

    def test_the_skill_folder_is_found_from_the_scripts_and_both_projects_have_their_fixed_spec(self) -> None:
        root = skill_root({})
        self.assertTrue(os.path.isfile(os.path.join(root, "SKILL.md")))
        gof = fixed_spec(root, PROJECTS["GOF"])
        self.assertFalse(gof.placeholder)
        self.assertRegex(gof.text, r"^# GOF \(无尽冬日\) ad material review spec", "the maintainers' comment is not given to the model")
        self.assertIn("上头", gof.text)
        self.assertIn("广告创意仅供参考", gof.text)
        self.assertIn("## 1. 红线", gof.text, "its red lines are in a section titled 红线, as the review prompt says")
        got = fixed_spec(root, PROJECTS["GOT"])
        self.assertFalse(got.placeholder)
        self.assertRegex(got.text, r"^# GOT ad material review spec")
        self.assertIn("## 2. 红线", got.text, "its red lines are in a section titled 红线, as the review prompt says")
        self.assertIn("1080x1920", got.text)
        # The dynamic spec's categories are the fixed specs' section titles.
        for category in CATEGORIES[:-1]:
            self.assertRegex(gof.text, rf"(?m)^## \d+\. {category}", category)
        self.assertEqual(project_of(" gof ").id, "GOF")
        with self.assertRaisesRegex(Blocked, "only GOF and GOT"):
            project_of("GOX")


if __name__ == "__main__":
    unittest.main()
