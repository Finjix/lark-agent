from __future__ import annotations

import json
import os
import unittest

from tests.helpers import temp_dir

from feishu_sdk.config import AppCredentials, load_app, scope_list
from feishu_sdk.errors import BridgeError
from feishu_sdk.paths import resolve_data_paths


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class ConfigTest(unittest.TestCase):
    def test_the_host_passes_the_app_in_the_environment(self) -> None:
        paths = resolve_data_paths({"LARK_AGENT_USER_DIR": temp_dir("fs-cfg-")})
        self.assertEqual(load_app({"FEISHU_APP_ID": " cli_a ", "FEISHU_APP_SECRET": "s3cret-000"}, paths), AppCredentials("cli_a", "s3cret-000"))
        with self.assertRaises(BridgeError) as caught:
            load_app({"FEISHU_APP_ID": "cli_a"}, paths)
        self.assertEqual(caught.exception.kind, "config")
        self.assertIn("宿主传入", caught.exception.message)

    def test_used_alone_the_app_may_sit_in_app_json_but_a_host_served_user_directory_never_supplies_one(self) -> None:
        folder = temp_dir("fs-cfg-")
        alone = resolve_data_paths({}, folder)
        with self.assertRaises(BridgeError) as caught:
            load_app({}, alone)
        self.assertIn(os.path.join(alone.root, "app.json"), caught.exception.message)
        write(os.path.join(alone.root, "app.json"), json.dumps({"appId": "cli_file", "appSecret": "file-secret-1"}))
        self.assertEqual(load_app({}, alone), AppCredentials("cli_file", "file-secret-1"))
        # The environment still wins.
        self.assertEqual(load_app({"FEISHU_APP_ID": "cli_env", "FEISHU_APP_SECRET": "env-secret-1"}, alone).app_id, "cli_env")

        host = resolve_data_paths({"LARK_AGENT_USER_DIR": temp_dir("fs-cfg-")})
        write(os.path.join(host.root, "app.json"), json.dumps({"appId": "cli_planted", "appSecret": "planted-secret"}))
        with self.assertRaises(BridgeError, msg="a file in a user directory is not trusted as the app"):
            load_app({}, host)

    def test_an_app_json_written_with_a_byte_order_mark_is_read(self) -> None:
        folder = temp_dir("fs-app-")
        with open(os.path.join(folder, "app.json"), "w", encoding="utf-8-sig") as handle:
            handle.write('{"appId":"cli_x","appSecret":"s"}')
        self.assertEqual(load_app({}, _standalone(folder)).app_id, "cli_x")

    def test_scopes_are_listed_once_whatever_separates_them(self) -> None:
        self.assertEqual(scope_list("a b,c  a\td"), ["a", "b", "c", "d"])
        self.assertEqual(scope_list(None), [])


def _standalone(root: str):
    from dataclasses import replace

    return replace(resolve_data_paths({}, root), root=root)


if __name__ == "__main__":
    unittest.main()
