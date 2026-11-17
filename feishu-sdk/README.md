# feishu-sdk: lark-agent's Feishu module

Feishu as the user is part of the agent, not a skill: this module holds each user's own Feishu login (a device authorization, confirmed by
one QR scan done in the user's hidden browser), reads Feishu documents into Markdown, and is the one way skills reach Feishu as the user, so
there is one login, one set of allowlisted calls and one hidden browser, and the user scans once.

- **The agent** uses it through its built-in Feishu tools (`src/agent/feishu.ts`): `feishu_login_check`, `feishu_login` (the sign-in QR
  in the working directory, refused in a group chat), `feishu_login_finish` (after the scan: 【授权】, collect the login, check it),
  `feishu_logout` and `feishu_read_doc`. They run `feishu.py` with the machine's Python and the user's environment. Reading Feishu is
  groundwork for the skills, not a capability offered to the user (see `src/agent/instructions.ts`).
- **Skills** (requirement-auto) use its bridge: [bridge.md](bridge.md). The host gives them its path in `FEISHU_CLI`. They never log in: a
  skill that finds the login missing says so, and the agent logs the user in with its tools. (audit-auto reads its review groups with the
  bot's own app identity and does not use this module.)
- **Housekeeping**: the service runs `feishu.py prune` for every idle user directory with a `feishu/` folder (see `src/service/maintenance.ts`).

Tests: `python -m unittest discover -s feishu-sdk/tests -t feishu-sdk`. Dependencies (`requirements.txt`, only `websockets`, for driving
the browser) are installed by `start.cmd` / `start.cmd setup` together with the skills'.

## Commands

`feishu.py <command>` prints one line of JSON; exit code 2 means refused or not ok, with `reason` (or `message`) saying why. No output ever
holds a token or `FEISHU_APP_SECRET`.

| Command | What it does |
|---|---|
| `login check` | Asks Feishu whether the login is good (renewing it if needed): `ok` (`name`), `login_required`, `scopes_missing` (`missing_scopes`), `identity_mismatch`, `unreachable`, `blocked` |
| `login qr --output <new .png> [--extra-scope <s>]…` | Starts a device authorization and saves Feishu's sign-in QR from the hidden browser: `qr_ready` (`qr_file`), `ok` (already signed in to Feishu web, no scan), `no_browser` (`verification_uri_complete`, `user_code`), `stuck`, `blocked` (always in a group chat, `AGENT_CHAT_TYPE=group`) |
| `login finish` | After the scan: presses 【授权】 in the hidden browser, collects the login and checks it: `ok`, `pending`, `login_required`, `denied`, `expired`, `no_pending`, `blocked` |
| `login status` / `login logout [--browser]` | The local record; forget the login (with `--browser`, sign the hidden browser out of Feishu web too) |
| `doc fetch --url <link> --output <new .md> [--sheet <tab>]` | A docx, a wiki node holding a doc or sheet, or a spreadsheet, as Markdown: `saved` (`file`, `kind`, `title`, `chars`; sheets `tabs`, `more_rows`), `login_required`, `scopes_missing`, `blocked` |
| `info` / `doctor` | What is configured; `doctor` also needs a login with every permission |
| `prune` | Removes stale temporary files (`FEISHU_RETENTION_DAYS`, default 14) and the hidden browser's caches while no browser runs on its profile; the login and the browser's sign-in stay. Prints `{"status":"ok","files":n,"bytes":n}` |
| `serve` / `bridge <command>` | For skills: [bridge.md](bridge.md) |

## Environment

- `AGENT_USER_DIR`: the user's data folder; the login and the hidden browser are in `<AGENT_USER_DIR>/feishu/` (without it,
  `~/.agent-skills/user/feishu/`, where `app.json` `{"appId","appSecret"}` may also hold the app).
- `AGENT_USER_OPEN_ID`: the user's Feishu Open ID; a login by any other account is refused.
- `AGENT_CHAT_TYPE`: `group` makes `login qr` refuse.
- `FEISHU_APP_ID`, `FEISHU_APP_SECRET`: the Feishu app the login belongs to. `FEISHU_BROWSER`: the browser to drive.
