"""Child process of the concurrency test: asks for an access token the way a separate bridge or requirement-auto process would."""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from feishu_sdk.auth import FeishuAuth  # noqa: E402
from feishu_sdk.config import AppCredentials, Endpoints  # noqa: E402
from feishu_sdk.errors import BridgeError  # noqa: E402
from feishu_sdk.paths import resolve_data_paths  # noqa: E402

env = os.environ
offset = float(env.get("FAKE_NOW") or 0)
auth = FeishuAuth(
    paths=resolve_data_paths({"AGENT_USER_DIR": env["AGENT_USER_DIR"]}), app=AppCredentials(env["MOCK_APP_ID"], env["MOCK_APP_SECRET"]),
    endpoints=Endpoints(open=env["MOCK_URL"], accounts=env["MOCK_URL"]), now=lambda: time.time() * 1000 + offset,
)
try:
    print(json.dumps({"ok": True, "token": auth.access_token()}))
except BridgeError as error:
    print(json.dumps({"ok": False, "kind": error.kind, "message": error.message}))
