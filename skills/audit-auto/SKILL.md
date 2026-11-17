---
name: audit-auto
description: "AI 审片 for GOF (无尽冬日 / Whiteout Survival China) and GOT ad material, and the dynamic review spec (动态审片规范) built from a project's review-group chat. Use when the user sends ad videos, images or zips to review (审片 / 审素材 / 审核视频 / 送审前检查) and replies with the 级别 / 位置 / 问题 / 怎么改 table; asks follow-up questions about a review or the spec; or, in a group, @-mentions the bot with 「监听本群更新审片规范」, 「停止监听本群」 / 「取消监听」, 「现在更新一下规范」, asks to see or send the GOF or GOT dynamic spec, which groups are listened to, or to delete a wrong rule (删掉 D3). Material must be said to be GOF's or GOT's: when the project is unknown, ask first."
compatibility: "Python 3.11+ (standard library only); ffmpeg optional. Needs AUDIT_AUTO_GEMINI_KEY, FEISHU_APP_ID / FEISHU_APP_SECRET and the LARK_AGENT_* environment from lark-agent."
metadata:
  version: "3.0.0"
---

# AI 审片 (audit-auto)

Talk to the user in their language (Chinese unless they write otherwise).

`audit` below means `python "<skill-dir>/scripts/audit.py"`, where `<skill-dir>` is the absolute path of the folder this SKILL.md is in, written with forward slashes (the shell is Git Bash). Run it from the current working directory (the user's workspace). It prints progress lines on stderr while it works and ends with one line of JSON on stdout. Exit code 2 means it refused or failed (`{"status":"blocked","reason":…}`): tell the user `reason`. Never read or print `AUDIT_AUTO_GEMINI_KEY` or `FEISHU_APP_SECRET`.

## Route the request

1. **Review material** (videos, images or zips sent for 审片): which project is it?
   - The user said GOF / GOFCN / 无尽冬日 / Whiteout Survival → read [references/gof-audit.md](references/gof-audit.md) and follow it.
   - The user said GOT → read [references/got-audit.md](references/got-audit.md) and follow it.
   - Not said in this conversation → ask one short question, 「这是 GOF 还是 GOT 的素材？」, and stop. Do not guess from file names or content.
2. **Dynamic spec** (监听本群 / 停止监听 / 现在更新 / 看规范 / 监听了哪些群 / 删掉 D3) → read [references/dynamic-rules.md](references/dynamic-rules.md) and follow it.
3. **Follow-up questions** about a finding or a rule: answer from the last review's `result_file` (and the `.json` next to it, which holds every transcribed line) and from the specs: the fixed ones in [references/gof-rules.md](references/gof-rules.md) and [references/got-rules.md](references/got-rules.md), the dynamic one from `audit rules show --project GOF|GOT` (its `markdown`; no file is written).

Commands at a glance:

```bash
audit review --project GOF|GOT --file "input/<name>" [--file …] [--note "<text>"]
audit rules watch --project GOF|GOT [--since YYYY-MM-DD]
audit rules unwatch
audit rules sync [--here]
audit rules status
audit rules show --project GOF|GOT [--output "动态审片规范-<P>.md"]
audit rules remove --project GOF|GOT --id D3 [--id …]
audit doctor        # self-check: specs, AI gateway, Feishu app, shared folder, ffmpeg, groups
audit info          # configuration (never the key)
```

The background reading every three days is run by lark-agent, not by you: see [references/scheduled-sync.md](references/scheduled-sync.md).
