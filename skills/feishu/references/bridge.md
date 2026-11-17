# The feishu skill's bridge (for other skills)

How a skill reaches Feishu as the user through this skill, so there is one login, one set of allowlisted calls and one hidden browser, and
the user scans once. A skill that uses the bridge never logs in: when a reply says the login is missing (`auth_required`,
`scope_missing`), it says so and the agent logs the user in with this skill's `login qr` / `login finish` (see ../SKILL.md). A caller
depends only on this contract: it does not import this skill's code and does not read its files.

Tests: `python -m unittest discover -s skills/feishu/tests -t skills/feishu`. Dependencies: `requirements.txt` (only `websockets`, for
driving the browser).

## Finding feishu.py

Install the feishu skill next to the calling skill (same skills folder), so it is `<skills>/feishu/scripts/feishu.py`; an absolute path in
`FEISHU_CLI` overrides that. Run it with **the same Python interpreter as the caller** (`sys.executable`). Not found: refuse and say the
feishu skill is needed; never talk to Feishu on your own.

```python
import os

def locate_feishu(skill_dir: str) -> str:
    named = os.environ.get("FEISHU_CLI", "").strip()
    path = named or os.path.join(os.path.dirname(skill_dir), "feishu", "scripts", "feishu.py")
    if not os.path.isabs(path) or not os.path.isfile(path):
        raise RuntimeError("the feishu skill is not installed next to this one (or FEISHU_CLI is wrong)")
    return path
```

## Environment

The child inherits the caller's environment as it is: `AGENT_USER_DIR` (user directory; the data is in `<user dir>/feishu/`, default
`~/.agent-skills/user/feishu/`), `AGENT_USER_OPEN_ID`, `FEISHU_APP_ID`, `FEISHU_APP_SECRET`. No command takes a user argument: whom it
serves is decided by the environment. Pass `env=None` (inherit) or a copy of `os.environ`; set `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8` if the caller
was started without them (the output is UTF-8 either way: it is written as bytes).

Also read: `FEISHU_BROWSER` (absolute path of the browser to use; otherwise Edge, then Chrome, then Chromium; the same profile must always
be opened with the same browser). Requests to `127.0.0.1` (the browser's debugging port) never go through a proxy; Feishu requests use the
environment's proxy settings.

## Commands

| Caller | Command | Output |
|---|---|---|
| Program | `feishu.py serve` | One request per stdin line `{"id":…,"command":"…","input":{…}}`, one reply per stdout line `{"id":…,"reply":{"ok":true,"data":…}}` or `{"id":…,"reply":{"ok":false,"error":{"kind","message","code?"}}}`. Replies come in the order they finish and carry the request's `id` (a line without one, or not a JSON object, is answered with `invalid_request` and no `id`). Exits 0 once stdin ends and every request has been answered. One process per caller process is enough |
| Program | `feishu.py bridge <command>`, input JSON on stdin | One line, the same reply; exit 0 (ok) or 2. Only the commands that take input read stdin; `info`, `auth-status`, `auth-verify` and `auth-logout` do not |
| The agent (../SKILL.md) | `login check/qr/finish/status/logout`, `doc fetch`, `doctor`, `info`, `prune` | One line of JSON; refusal `{"status":"blocked","reason":…}` with exit 2 (`login check`/`finish` not `ok`, `doc fetch` without `saved`, also 2). Not for skills: the login is the agent's |

```python
import json, subprocess, sys

def bridge(feishu: str, command: str, payload: object | None = None) -> dict:
    done = subprocess.run([sys.executable, feishu, "bridge", command], input=json.dumps(payload or {}).encode("utf-8"),
                          capture_output=True, timeout=120)
    return json.loads(done.stdout.decode("utf-8").strip().splitlines()[-1])
```

For `serve`, start `subprocess.Popen([sys.executable, feishu, "serve"], stdin=PIPE, stdout=PIPE)`, write one JSON line per request (flush),
read replies line by line in a thread and match them by `id`; close stdin to end it. A request whose process ended before it answered has no
answer: treat it as `network` (for a write: an unknown result, read back, never resend).

Bridge commands (`serve` and `bridge` alike), `COMMANDS` in `feishu_sdk/bridge.py`:

| Command | Input | Data |
|---|---|---|
| `info` | – | `bridge`, `python`, `storage`, `app_configured`, `required_scopes`, `root`, `browser_profile` |
| `auth-status` | – | the local record (see `login status`); no network |
| `auth-verify` | – | asks Feishu: `logged_in`, `open_id`, `name`, `missing_scopes`, `refresh_expires_at` |
| `auth-login` | `{"extra_scopes": [..]}` | a device authorization (see `login start`) |
| `auth-poll` | `{"wait_ms": n}` (≤ 50000) | see `login poll` |
| `auth-logout` | – | `{"logged_in": false}` |
| `auth-check-scopes` | `{"required": [..]}` | `complete`, `granted`, `missing` |
| `api-get` | `{"path", "params"}` | read-only, allowlisted paths only: wiki `get_node`, sheets v3 spreadsheet and `sheets/query`, sheets v2 `dataValidation`/`values_batch_get`/`metainfo`, docx `raw_content` |
| `sheets-revision` | `{"token"}` | `{"revision"}` |
| `sheets-export` | `{"token", "output"}` (absolute new `.xlsx`) | `{"path", "bytes"}` |
| `sheets-cells-set` | `{"token", "writes": [{"sheet_id","range","cells","allow_overwrite": false}]}` | Feishu's answer; only `allow_overwrite:false`, at most 100 ranges |
| `sheets-create` | `{"title"}` | `{"spreadsheet_token", "url"}` |
| `sheets-dropdown-add` | `{"token","sheet_id","column","first_row","last_row","option","color"}` | `requested`, `option`, `updated_ranges`, `ranges` |
| `docx-blocks` | `{"document"}` | `revision`, `title`, `blocks` (all of one revision) |
| `docx-insert` | `{"document","parent","index","children_id","descendants","client_token"}` (UUID) | Feishu's answer |
| `docx-set-texts` | `{"document","updates":[{"block_id","elements"}],"client_token"}` | Feishu's answer |

The protocol version is `info.bridge`, now `feishu-sdk-bridge/1`; a caller that meets a version it does not know must refuse to work. Adding
commands or fields does not change the version; changing what an existing command means does.

`error.kind`: `config`, `auth_required`, `login_denied`, `login_expired`, `identity_mismatch`, `scope_missing`, `invalid_request`, `occupied`
(the server refused to overwrite a non-empty cell), `unknown_result` (a write may have been applied: read back only, never resend),
`network`, `api`. Messages are already scrubbed of tokens and the app secret and may be relayed as they are. On `auth_required` /
`scope_missing` say that the Feishu login is needed: the agent logs the user in with the feishu skill, never the calling skill.

## Browser

`info.browser_profile` is this user's hidden browser profile; it holds the Feishu web session (left by the scan of `login qr`). A skill
whose site signs in through the company's Feishu single sign-on (requirement-auto's CLS) opens its own pages on this profile, so the user does
not scan again. Only one process drives it at a time:

- Lock: `<browser_profile>.lock` beside the profile, created exclusively, content `{"pid","at","token"}` (`at` in ms). The holder rewrites
  `at` every 30 s. A lock whose process has ended, or that was not renewed for 2 minutes, or that is unparseable and older than 10 s, is
  abandoned and taken over (rename it aside, check it is still the one you read, then delete it). Release deletes the file only if its
  `token` is yours. Wait up to 2 minutes for a held lock.
- Port: the browser writes `DevToolsActivePort` in the profile (port, then `/devtools/browser/<id>`). A port that answers
  `/json/version` with another browser's path is stale.
- Browser: `FEISHU_BROWSER`, else Edge, Chrome, Chromium. The same profile must always be opened
  with the same browser, or the Feishu web session does not carry over. Start flags as here (`--user-data-dir=<profile>
  --restore-last-session --remote-debugging-port=0 --headless=new …`, see `feishu_sdk/browser.py`).

Use the same lock, and while you hold it never run a feishu.py command that opens the browser (`authorize`, `login logout --browser`, a
`login poll` that has to sign the browser out; `login qr` and `login finish` run these): both sides would wait for each other.

## Permissions

One login asks for what every caller needs (`REQUIRED_SCOPES` in `feishu_sdk/config.py`, plus `docx:document` and
`offline_access`). A caller that needs another permission adds it there and has it enabled in the Feishu app; callers check the ones they
use with `auth-check-scopes`.

User-identity permissions the app must have, published: `wiki:wiki:readonly sheets:spreadsheet:readonly sheets:spreadsheet:read
sheets:spreadsheet:write_only sheets:spreadsheet drive:export:readonly docx:document:readonly docx:document offline_access`
(requirement-auto's overwrite probe also `sheets:spreadsheet:create`, asked for with `login qr --extra-scope`).

## Data

| Case | Where |
|---|---|
| `AGENT_USER_DIR` given | `<user dir>/feishu/`: `auth.json` (login), `login.json` (pending authorization), `login-qr.json` (QR waiting to be scanned), `refresh.lock`, `browser-profile/` (hidden browser, with the Feishu web session) and `browser-profile.lock` beside it |
| Not given | `~/.agent-skills/user/feishu/`, same files; the app in `app.json` there (`{"appId":"…","appSecret":"…"}`) or `FEISHU_APP_ID` / `FEISHU_APP_SECRET` |

`AGENT_USER_OPEN_ID` without `AGENT_USER_DIR` is refused: no fallback to a shared folder. On the consent page only 【授权】 is ever
pressed, never 【拒绝】, and only on a page reached through this user's own device authorization; the authorizing account must be the user
the environment names (`AGENT_USER_OPEN_ID`), otherwise the token is never stored. Tokens and the app secret never appear in any output.
