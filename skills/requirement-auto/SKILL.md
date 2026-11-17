---
name: requirement-auto
description: "GOFCN creative materials between Feishu and CLS. (1) Publish requirements (发布需求 / 素材需求 / 建单 / 发单 / 占号 / 创意进度表 / CLS 草稿 / 填需求表) from Feishu requirement documents: work out rows, tags, 创意形式, 提炼词, supplier and reviewer, append the rows to the Feishu creative progress sheet and verify them, build the CSV, save an unpublished CLS requirement draft with the scripts imported, hand back screenshots and one copyable group message; also numbers first and the ticket later, and writing the requirement into the user's document. (2) Upload finished materials (上传素材 / 素材上传 / 上传创意 / 传素材到 CLS) the user sends or names (videos, pictures, folders, zips): register a progress-sheet row per file that has none, then upload each file to CLS 创意列表 named as its row's 素材名称, save 创意人/制作人/素材类型, and hand back screenshots. Use it for any GOFCN requirement, progress-sheet or CLS creative task; it reaches Feishu through lark-agent's own Feishu module (the agent does the Feishu login). Asks only what cannot be worked out and runs everything once the user approves the plan."
---

# GOFCN 需求与素材 (requirement-auto)

Talk to the user in their language (Chinese unless they write otherwise). Field names, sheet columns, CLS fields and group messages stay Chinese.

`gof` = `python <skill>/scripts/gof.py` (`<skill>` = this skill's directory; Python 3.11+, standard library only). Run it from bash with forward slashes, e.g. `python "<skill>/scripts/gof.py" login check`. Every command prints one JSON line; a refusal (a wrong argument included) is `status: blocked` with the reason and exit code 2.

## Which workflow

Read the matching reference **in full** before the first command and follow it:

- **Materials still to be made** (the user gives requirement documents, asks to 发布需求 / 建单, to reserve numbers first (占号) and 发单 later, or to write a requirement into their document): [references/gof-requirement.md](references/gof-requirement.md).
- **Finished materials to upload** (the user sends or names videos, pictures, folders or zips to put into CLS): [references/gof-creative-upload.md](references/gof-creative-upload.md). These get rows, never a ticket.

Both share: [references/feishu-sdk-workflow.md](references/feishu-sdk-workflow.md) (Feishu commands, append safety, recovery, document edits), [references/api-workflow.md](references/api-workflow.md) (CLS draft commands), [references/data-contract.md](references/data-contract.md) (request, plan and batch files), [references/cls-session.md](references/cls-session.md) (the CLS login).

## Steps on request

The flows in the references are the default order for a whole task, not a fence. The user may ask for any single step, skip steps, change the order, or have a step done again, and you do that:

- **One step** ("只截个进度表的图", "把群消息再发我一下", "只登记进度表", "只上传", "只要 CSV", "查一下草稿", "把编号写进文档"): run that step and what it needs, nothing after it. A step that needs something an earlier step makes (a batch for the CSV, draft and message; a row for an upload) takes it from this session's or an earlier batch (`gof batch list`), and builds it only when there is none: say so in one line.
- **Skipping** ("不用截图", "先不建草稿", "群消息不要"): leave that step out and, where it matters, say in one line what is not done.
- **Again**: steps that only read or rebuild run again at once (login check, reading documents and the sheet, plans and previews, CSV, screenshots, group message, `read-draft` / `verify`, `batch status`). Steps that write go to what is there first: a re-run of an append, a draft or an upload goes through the recovery in the reference (reconcile, read back), which finishes or confirms what was there instead of writing it twice. If the user wants a second copy (a new draft, new rows for the same pieces) and the tool refuses, tell them the reason in one line and what can be done instead.
- **Go-ahead**: steps that only read or rebuild need none. A step that writes to Feishu or CLS (rows, dropdown options, a draft, uploads, a document edit) gets a short plan of just that step and the user's go-ahead first; a go-ahead for the whole task covers all its steps.

The data safety the tools enforce stays whatever the user asks: append only into empty rows, each write sent once, drafts never published, each creative created once.

## Shared rules

- Feishu goes only through lark-agent's own Feishu module, which the host names in `LARK_AGENT_FEISHU`; the Feishu login is yours, done with your Feishu tools (`feishu_login`, then `feishu_login_finish`), never by this skill. A command saying the Feishu module is missing: tell the user the bot cannot reach Feishu and to contact the maintainer. Never log in to Feishu any other way.
- `gof login check` at the start of every task, after approval before the first write, before the CLS stage, and whenever a command reports a lost login.
- Run the commands and tools that use the browser (`login check`, `feishu_login`, `feishu_login_finish`, `cls …`, `creative …`, the screenshots) one after another, never in parallel: there is one browser per user.
- Intermediate files go to `<LARK_AGENT_USER_DIR>/requirement-auto/tmp`, never the working directory (lark-agent sends the user every new file there): only QR images and the screenshots handed back go to the working directory.
- The tools enforce the data rules (append only into empty rows, each write sent once, drafts only, each creative created once); when one refuses, act on its reason instead of working around it.
- If `append` rejects the overwrite receipt (`assets/overwrite-receipt.json`), tell the user "写入功能暂时不可用，请联系维护人员".
