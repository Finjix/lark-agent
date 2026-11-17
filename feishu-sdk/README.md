# feishu-sdk: lark-agent's Feishu module

Feishu as the user is part of the agent, not a skill: this module holds each user's own Feishu login (a device authorization, confirmed by
one QR scan done in the user's hidden browser), reads Feishu documents into Markdown, and is the one way skills reach Feishu as the user, so
there is one login, one set of allowlisted calls and one hidden browser, and the user scans once.

- **The agent** uses it through its Feishu tools (`src/agent/feishu.ts`): `feishu_login_check`, `feishu_login` (device authorization and
  the QR picture in the working directory, refused in a group chat), `feishu_login_finish` (after the scan: 【授权】, collect the login,
  check it), `feishu_logout` and `feishu_read_doc`. The tools run `feishu.py` with the machine's Python and the user's environment.
- **Skills** (requirement-auto) use its bridge, below. They never log in: a skill that finds the login missing says so, and the agent logs
  the user in with its tools. (audit-auto reads its review groups with the bot's own app identity and does not use this module.) A caller
  depends only on the contract below: it does not import this module's code and does not read its files.

Tests: `python -m unittest discover -s feishu-sdk/tests -t feishu-sdk`. Dependencies (`requirements.txt`, only `websockets`, for driving
the browser) are installed by `start.cmd` / `start.cmd setup` together with the skills'.

## Finding feishu.py

The host sets `LARK_AGENT_FEISHU` for every command the agent runs and every job: the absolute path of `<project>/feishu-sdk/feishu.py`. Run it
with **the same Python interpreter as the caller** (`sys.executable`). Not set (run outside lark-agent): refuse; never talk to Feishu on your own.

```python
import os

def locate_feishu() -> str:
    named = os.environ.get("LARK_AGENT_FEISHU", "").strip()
    if not named:
        raise RuntimeError("没有找到 lark-agent 的飞书模块（LARK_AGENT_FEISHU 没有设置）")
    if not os.path.isabs(named) or not os.path.isfile(named):
        raise RuntimeError("LARK_AGENT_FEISHU 不是 feishu.py 的绝对路径")
    return named
```

## Environment

The child inherits the caller's environment as it is: `LARK_AGENT_USER_DIR` (user directory; the data is in `<user dir>/feishu/`),
`LARK_AGENT_USER_OPEN_ID`, `FEISHU_APP_ID`, `FEISHU_APP_SECRET`. No command takes a user argument: whom it serves is decided by the
environment the host gave. Pass `env=None` (inherit) or a copy of `os.environ`; set `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8` if the caller
was started without them (the output is UTF-8 either way: it is written as bytes).

Also read: `FEISHU_BROWSER` (absolute path of the browser to use; otherwise Edge, then Chrome, then Chromium; the same profile must always
be opened with the same browser). Requests to `127.0.0.1` (the browser's debugging port) never go through a proxy; Feishu requests use the
environment's proxy settings.

## Commands

| Caller | Command | Output |
|---|---|---|
| Program | `feishu.py serve` | One request per stdin line `{"id":…,"command":"…","input":{…}}`, one reply per stdout line `{"id":…,"reply":{"ok":true,"data":…}}` or `{"id":…,"reply":{"ok":false,"error":{"kind","message","code?"}}}`. Replies come in the order they finish and carry the request's `id` (a line without one, or not a JSON object, is answered with `invalid_request` and no `id`). Exits 0 once stdin ends and every request has been answered. One process per caller process is enough |
| Program | `feishu.py bridge <command>`, input JSON on stdin | One line, the same reply; exit 0 (ok) or 2. Only the commands that take input read stdin; `info`, `auth-status`, `auth-verify` and `auth-logout` do not |
| The agent's tools | `login start/poll/status/logout/check`, `authorize [--qr]`, `doc fetch`, `doctor`, `info` | One line of JSON; refusal `{"status":"blocked","reason":…}` with exit 2 (`login check` not `ok`, `doc fetch` without `saved`, also 2). Not for skills: the login is the agent's |

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
`scope_missing` say that the Feishu login is needed: the agent logs the user in with its Feishu tools, never the skill.

## Browser

`info.browser_profile` is this user's hidden browser profile; it holds the Feishu web session (left by the scan of the agent's `feishu_login`). A skill
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
`login poll` that has to sign the browser out; the agent's login tools run these): both sides would wait for each other.

## Permissions

One login asks for what every caller needs (`REQUIRED_SCOPES` in `feishu_sdk/config.py`, plus `docx:document` and
`offline_access`). A caller that needs another permission adds it there and has it enabled in the Feishu app; callers check the ones they
use with `auth-check-scopes`.

User-identity permissions the app must have, published: `wiki:wiki:readonly sheets:spreadsheet:readonly sheets:spreadsheet:read
sheets:spreadsheet:write_only sheets:spreadsheet drive:export:readonly docx:document:readonly docx:document offline_access`
(requirement-auto's overwrite probe also `sheets:spreadsheet:create`, asked for with `feishu_login`'s `extra_scopes`).

## Data

| Case | Where |
|---|---|
| lark-agent (`LARK_AGENT_USER_DIR` given) | `data/user/<name>/feishu/`: `auth.json` (login), `login.json` (pending authorization), `login-qr.json` (QR waiting to be scanned), `refresh.lock`, `browser-profile/` (hidden browser, with the Feishu web session) and `browser-profile.lock` beside it |
| Used alone | `~/.feishu-plugin/default/`, same files; the app in `app.json` there (`{"appId":"…","appSecret":"…"}`) or `FEISHU_APP_ID` / `FEISHU_APP_SECRET` |

`LARK_AGENT_USER_OPEN_ID` without `LARK_AGENT_USER_DIR` is refused: no fallback to a shared folder. On the consent page only 【授权】 is ever
pressed, never 【拒绝】, and only on a page reached through this user's own device authorization; the authorizing account must be the user
the host names (`LARK_AGENT_USER_OPEN_ID`), otherwise the token is never stored. Tokens and the app secret never appear in any output.
