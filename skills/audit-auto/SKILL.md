---
name: audit-auto
description: "Reviews GOF (无尽冬日 / Whiteout Survival China) and GOT ad material (videos, images, zips of them) with Gemini against the project's review spec, and keeps each project's dynamic review spec (动态审片规范), built from its Feishu review-group chat. Use when the user sends ad material to review (审片 / 审素材 / 审核视频 / 送审前检查) or asks about a review or a rule; or asks the bot to listen to a review group (「监听本群更新审片规范」), to stop (「停止监听本群」 / 「取消监听」), to update now (「现在更新一下规范」), to show or send the GOF or GOT dynamic spec, which groups are listened to, or to delete a wrong rule (「删掉 D3」). The material must be said to be GOF's or GOT's: when the project is unknown, ask first."
compatibility: "Python 3.11+ (standard library only); ffmpeg optional. Needs AUDIT_AUTO_GEMINI_KEY (the internal AI gateway) and, for the dynamic spec, FEISHU_APP_ID / FEISHU_APP_SECRET of a Feishu bot that is a member of the review groups."
metadata:
  version: "3.1.0"
  prune: "scripts/audit.py prune"
---

# audit-auto: ad material review

Reply in the user's language. Send as they are, without translating: the review tables the command prints (they are in Chinese, for Chinese ad platforms) and the Chinese phrases these instructions give the user to say.

`audit` below means `python "<skill-dir>/scripts/audit.py"`, where `<skill-dir>` is the absolute path of the folder this SKILL.md is in. Run it from the current working directory. It prints progress on stderr and ends with one line of JSON on stdout. Exit code 2 means it refused or failed (`{"status":"blocked","reason":…}`): tell the user `reason`, in their language. Never read or print `AUDIT_AUTO_GEMINI_KEY` or `FEISHU_APP_SECRET`.

## Route the request

1. **Review material** (videos, images or zips): go on only when the user has said in this conversation which project it is for. GOF (also GOFCN, 无尽冬日, Whiteout Survival) or GOT: follow [references/review.md](references/review.md). Not said: ask one short question, 「这是 GOF 还是 GOT 的素材？」 (GOF or GOT material?), and stop. Never guess from file names or content.
2. **Dynamic spec** (listen to a group, stop, update now, show the spec, which groups, delete a rule): follow [references/dynamic-rules.md](references/dynamic-rules.md).
3. **Follow-up questions** about a finding or a rule: answer from the last review's `result_file` (and the `.json` next to it, which holds every transcribed line), from the fixed spec ([references/gof-rules.md](references/gof-rules.md), [references/got-rules.md](references/got-rules.md)) and from the dynamic spec (`audit rules show --project GOF|GOT`, its `markdown`; no file is written).

The periodic sync and housekeeping are for the host or the administrator, not for a user's request: see [references/scheduled-sync.md](references/scheduled-sync.md).

## Commands

```bash
audit review --project GOF|GOT --file "<path>" [--file "<path>"]… [--note "<text>"]
audit rules watch --project GOF|GOT [--chat <chat_id>] [--since YYYY-MM-DD]
audit rules unwatch [--chat <chat_id>]
audit rules sync [--here | --chat <chat_id>]
audit rules status [--chat <chat_id>]
audit rules show --project GOF|GOT [--output "<file>.md"]
audit rules remove --project GOF|GOT --id D3 [--id …]
audit prune         # housekeeping: delete stale scratch and kept review results
audit doctor        # self-check: specs, AI gateway, Feishu app, shared folder, ffmpeg, groups
audit info          # configuration (never the key)
```

`--chat` is a Feishu chat id (`oc_…`) of a group the bot is a member of; without it, the commands use the chat the host reports in `AGENT_CHAT_ID`.

## Environment

All optional; the host sets what it knows.

- `AGENT_USER_DIR`: the user's data root (absolute). Per-user data goes to `<AGENT_USER_DIR>/audit-auto/`: `tmp/` (scratch) and `reviews/` (kept review results). Default `~/.agent-skills/user`.
- `AGENT_SHARED_DIR`: data shared by all users (absolute). Review groups and dynamic specs go to `<AGENT_SHARED_DIR>/audit-auto/`. Default `~/.agent-skills/shared`.
- `AGENT_CHAT_ID`, `AGENT_CHAT_TYPE` (`p2p` or `group`): the chat of this turn. `AGENT_USER_OPEN_ID`: who asked.
- `AUDIT_AUTO_GEMINI_KEY`: the AI gateway key. `FEISHU_APP_ID` / `FEISHU_APP_SECRET`: the bot's Feishu app, which reads the review groups.
- `AUDIT_AUTO_RETENTION_DAYS`: how long `prune` keeps scratch and review results (default 14).
