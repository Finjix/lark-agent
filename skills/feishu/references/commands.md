# feishu.py: commands, results, data

`python <skill>/scripts/feishu.py <command>` (Python 3.11+; the host's `python`). Every command prints one line of JSON on stdout (`serve`:
one per request). Refused: `{"status":"blocked","reason":…}` and exit code 2. `login check` not `ok`, and `doc fetch` without `saved`, also
exit 2; every other result exits 0 and is read by its `status` / `state`. Unknown options and stray arguments are refused.

| Command | Does |
|---|---|
| `login check` | Asks Feishu with the stored login (one renewal if the token is turned down, which also keeps an idle login alive): `ok` (`name`), `login_required`, `identity_mismatch`, `scopes_missing` (`missing_scopes`), `unreachable`, `blocked`; `todo` when a login is needed |
| `login start [--extra-scope <scope>]…` | Starts a device authorization: `verification_uri`, `verification_uri_complete`, `user_code`, `expires_in_s`, `interval_s`, `reused` (a still valid one was given again) |
| `authorize [--qr <file.png>]` | With `--qr`: opens the authorization in the hidden browser; not signed in to Feishu web there → the sign-in QR is saved as the PNG (`qr_ready`, `qr_file`), signed in → 【授权】 is pressed (`approved`). Without: after the scan, presses 【授权】. Also `no_pending`, `login_required`, `stuck` (`where`) |
| `login poll [--wait <0..50 s>]` | Collects the login: `state` `authorized` (`open_id`, `name`, `scopes`, `refreshable`), `pending` (`retry_after_s`, `expires_in_s`), `denied`, `expired`. An account that is not the user's is refused, and the hidden browser is signed out of it |
| `login status` | Local record only: `logged_in`, `app_configured`, `storage`, `open_id`, `name`, `scopes`, `missing_scopes`, expiry times, `refreshable`, `pending_login` |
| `login logout [--browser]` | Forgets the login and a pending authorization (`--browser`: also signs the hidden browser out of Feishu web: `browser_signed_out`) |
| `doc fetch --url <link> --output <new.md> [--sheet <tab>]` | Document as Markdown in a new file: `saved` with `file`, `kind`, `title`, `chars` (sheets: `tabs`, `more_rows`); `login_required` / `scopes_missing` with `todo` |
| `doctor` | App configured, logged in, every permission: `version`, `python`, `user_name`, `open_id`, `storage`, `root`, `required_scopes` |
| `info` | Bridge `info`: `bridge` (protocol version), `python`, `storage`, `app_configured`, `required_scopes`, `root`, `browser_profile` (no secrets) |
| `bridge <command>` / `serve` | For other skills: see [for-other-skills.md](for-other-skills.md) |

On the consent page only 【授权】 is ever pressed, never 【拒绝】, and only on a page reached through this user's own device authorization;
the authorizing account must be the user the host names (`LARK_AGENT_USER_OPEN_ID`), otherwise the token is never stored.

## Data

| Case | Where |
|---|---|
| lark-agent (`LARK_AGENT_USER_DIR` given) | `user/<name>/feishu/`: `auth.json` (login), `login.json` (pending authorization), `login-qr.json` (QR waiting to be scanned), `refresh.lock`, `browser-profile/` (hidden browser, with the Feishu web session) and `browser-profile.lock` beside it |
| Used alone | `~/.feishu-plugin/default/`, same files; the app in `app.json` there (`{"appId":"…","appSecret":"…"}`) or `FEISHU_APP_ID` / `FEISHU_APP_SECRET` |

`LARK_AGENT_USER_OPEN_ID` without `LARK_AGENT_USER_DIR` is refused: no fallback to a shared folder.

## Environment

- `FEISHU_APP_ID`, `FEISHU_APP_SECRET`: the bot's app (from the host). User-identity permissions the app must have, published:
  `wiki:wiki:readonly sheets:spreadsheet:readonly sheets:spreadsheet:read sheets:spreadsheet:write_only sheets:spreadsheet drive:export:readonly
  docx:document:readonly docx:document offline_access` (requirement-auto's probe also `sheets:spreadsheet:create`).
- `FEISHU_BROWSER`: absolute path of the browser to use; otherwise Edge, then Chrome, then
  Chromium is searched. The same profile must always be opened with the same browser.
- Requests to `127.0.0.1` (the browser's debugging port) never go through a proxy; Feishu requests use the environment's proxy settings.
- Driving the browser needs the Python package `websockets` (`python -m pip install -r <skill>/requirements.txt`); nothing else does.
