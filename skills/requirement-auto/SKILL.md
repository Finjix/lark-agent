---
name: requirement-auto
description: "GOFCN creative-material work between Feishu and CLS. (1) Publish requirements from Feishu requirement documents (发布需求 / 素材需求 / 建单 / 发单 / 占号 / 创意进度表 / CLS 草稿 / 填需求表): work out the rows, append them to the Feishu creative progress sheet and verify them, save an unpublished CLS requirement draft with the scripts imported, hand back screenshots and a copyable group message; also reserve numbers now and the ticket later, or write the requirement into the user's document. (2) Upload finished materials the user sends or names, videos, pictures, folders or zips (上传素材 / 素材上传 / 上传创意 / 传素材到 CLS): register a progress-sheet row for each file without one, upload each file to the CLS 创意列表 under its row's 素材名称 with 创意人 / 制作人 / 素材类型, hand back screenshots. Use it for any GOFCN requirement, progress-sheet or CLS creative task."
compatibility: "Python 3.11+, standard library only. A Chromium-based browser (Edge or Chrome) for CLS and the screenshots. Needs the agent's Feishu module (lark-agent's feishu-sdk/feishu.py, named by FEISHU_CLI) and the agent's Feishu login."
metadata:
  prune: "scripts/gof.py prune"
---

# GOFCN requirements and materials

Reply in the user's language. Keep as they are, in Chinese: sheet columns, CLS fields and labels, names, values written to Feishu or CLS, and the group message.

`gof` below means `python "<skill>/scripts/gof.py"`, `<skill>` being the absolute path of this folder (forward slashes in a POSIX shell). Every command prints one JSON line; a refusal is `status: blocked` with `reason` and exit code 2. Relay a reason in your own words, with the system's codes it names.

## Which workflow

Read the matching reference **in full** before the first command, then follow it:

- **Materials still to be made**: requirement documents, 发布需求 / 建单, numbers reserved first (占号) and the ticket later (发单), writing the requirement into the user's document: [references/requirement.md](references/requirement.md).
- **Finished materials to upload** into CLS (videos, pictures, folders, zips): [references/upload.md](references/upload.md). These get progress-sheet rows, never a ticket.

Both use [references/feishu-commands.md](references/feishu-commands.md) (Feishu commands, append, recovery, document edits), [references/cls.md](references/cls.md) (CLS login and draft commands) and [references/data-contract.md](references/data-contract.md) (request, ticket and batch files, `gof batch`).

## Environment

- **The agent's Feishu module**: `FEISHU_CLI`, the absolute path of its `feishu.py` (lark-agent's `feishu-sdk/feishu.py`; lark-agent sets it). All Feishu access, the Feishu login and the hidden browser are the module's. A command saying it is missing: tell the user Feishu cannot be reached from here and the maintainer has to set it up.
- Optional variables, set by the host:
  - `AGENT_USER_DIR`: absolute per-user data root; this skill keeps its data in `<AGENT_USER_DIR>/requirement-auto/` (`batches/`, `tmp/`, `cls/`), by default in `~/.agent-skills/user/requirement-auto/`. With `AGENT_USER_OPEN_ID` set and no `AGENT_USER_DIR`, every command refuses.
  - `AGENT_USER_OPEN_ID`, `AGENT_CHAT_ID`, `AGENT_CHAT_TYPE` (`p2p` / `group`), `FEISHU_APP_ID` / `FEISHU_APP_SECRET`: who the user is, where the chat is, the Feishu app; read by the Feishu module (another account is rejected, a QR login is refused in a group chat).
  - `FEISHU_BROWSER`: the browser executable, when Edge or Chrome is not found. `REQUIREMENT_AUTO_USE_ENV_PROXY=1`: use `HTTPS_PROXY` / `NO_PROXY`. `REQUIREMENT_AUTO_RETENTION_DAYS`: days untouched batches and tmp files are kept (default 7).
- Housekeeping: `gof prune` (run by a host, or by every other command first) removes batches and tmp files untouched for the retention period, never the CLS login, and prints `{"status":"ok","files":<n>,"bytes":<n>}`.
- Timing: every command adds one line (command name, outcome, time per phase; no arguments, IDs or names) to `<data>/timing.jsonl`. `gof timing [--minutes <n>] [--last <n>]` sums it up: each command's time, the agent's time between commands (`gap_ms`), totals per command and per phase. For maintainers: run it only when asked where the time goes.

## Logins

Run `gof login check` at the start of every task, after approval before the first write, before the CLS stage or the upload, and whenever a command reports a lost login. It checks Feishu first, then CLS. By `status`:

- `ok`: go on.
- `login_required`: do what `todo` says, one login at a time, Feishu first:
  - **Feishu**: log the user in with your Feishu login (`feishu_login` gives a sign-in QR image for the user to scan with the Feishu mobile app, `feishu_login_finish` once they confirm; without those tools, the module's `login qr` and `login finish`, as `todo` says). The same scan normally logs CLS in too. A login made before this skill needed a permission lacks it (`scopes_missing`, or a command says so): the same QR login with `extra_scopes` (`--extra-scope`) for each one named.
  - **CLS**, only when still needed: `gof cls login --qr <working dir>/cls-login-<time>.png` (`qr_ready`: give the user the picture to scan with the Feishu mobile app and confirm; `logged_in`: no scan needed; `no_qr`: run `gof cls session` first). After the user confirms: `gof cls session` (about 25 s). Details in [references/cls.md](references/cls.md).
  - Then `gof login check` again. A QR code expires in a few minutes: make a new one when it has.
- `blocked`: tell the user `feishu.reason` / `cls.reason`. `expected_account`: the user has to scan with that account.
- `unknown`: Feishu or CLS cannot be reached right now. The login is not lost: no QR, no scan; check again a little later.

A sign-in QR is for this user alone: never show it in a group chat or to anybody else. Never log in to Feishu or CLS any other way.

## Rules

- **Approval.** Plan first: read, work out every field, ask only what is left, show one plan, wait for the go-ahead. A go-ahead counts only for a plan you have already shown. Then run the approved plan to the end without asking again: retry read-only commands, log in again, export again, fix and re-run what a tool refused. Ask only when the plan itself would change (files, rows, numbers, names, people) or for what only the user can do (a scan, a permission). Messages the user sends while you work are corrections or more material.
- **Steps on request.** The flows are the default order for a whole task, not a fence. The user may ask for one step ("只截个进度表的图", "把群消息再发我一下", "只登记进度表", "只上传", "只要 CSV", "把编号写进文档"), skip steps ("不用截图", "先不建草稿"), or have a step done again. Run what is asked and what it needs, nothing after it; a step that needs an earlier step's result (a batch for the CSV, draft and message; a row for an upload) takes it from this session's or an earlier batch (`gof batch list`) and builds it only when there is none, saying so in one line. Steps that only read or rebuild (login check, documents, the sheet, plans, CSV, screenshots, group message, `read-draft`, `verify`, `batch status`) run again at once and need no go-ahead. A step that writes to Feishu or CLS (rows, dropdown options, a draft, uploads, a document edit) gets a short plan of just that step and the go-ahead first (a go-ahead for the whole task covers its steps); run again, it goes through the recovery in the reference, which finishes or confirms what is there instead of writing twice. When a tool refuses a second copy, tell the user why in one line and what can be done instead.
- **Data safety, enforced by the tools whatever the user asks:** rows are appended only into empty rows (`allow_overwrite:false`), each write is sent once and read back, CLS drafts are never published, each creative is created once. When a tool refuses, act on its reason; never work around it. If `append` rejects the overwrite receipt (`assets/overwrite-receipt.json`), tell the user that writing is unavailable for now and to contact the maintainer.
- **One browser per user.** Run everything that uses it one after another, never in parallel: `login check`, the Feishu login, `cls …`, `creative …`, the screenshots. A second command waits up to 2 minutes for the first, then refuses.
- **Files.** Intermediate files (requests, snapshots, plans, catalogs, observations, unpacked zips) go to `<data>/tmp` (`<AGENT_USER_DIR>/requirement-auto/tmp`, or the default above), never the working directory: a host may send the user every new file there. Only sign-in QR images, the screenshots handed back and files the user asked for go to the working directory. Never move, rename or edit the user's files. Remember the batch path for the session; to resume, use the original batch.

## Talking to the user

Short and plain, business language only. While planning say nothing until you have the question or the plan; while executing at most one short line per stage (appending the rows, creating the draft, uploading). Leave out internals (IDs, hashes, revisions, receipts, snapshots, batches, plan files, checks) unless asked. When something stops: what is done, what is not, what the user must do, and the concrete reason with the system's code (CLS: HTTP status, business code and message; Feishu: error kind and code), naming the conflicting value.

Ask only for what is missing, in one short message of plain questions, without conditions, defaults or instructions attached (not "if it is the same person, say so"): work such cases out from the answer (one name for both, "都是 X", is the same person). Never mention this skill, its files or its rules, never describe how you will work, never echo the user's answer back, repeat an earlier message, list what stays empty or report a step that changes nothing for the user. Before sending, cut every sentence the user could do without.

Screenshots go as attachments only: no text link or local path to an image (it does not open). Give the real links (progress sheet, CLS draft, creative list) for whatever exists, also when a run stopped part-way.
