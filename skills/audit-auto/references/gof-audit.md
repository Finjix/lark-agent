# GOF 审片 (review GOF ad material)

`audit` = `python "<skill-dir>/scripts/audit.py"` (see SKILL.md). It prints progress lines while it works and ends with one line of JSON. Exit code 2 means it refused or failed (`{"status":"blocked","reason":…}`).

## 1. Which project

This workflow reviews GOF material only. Go on only when the user has said in this conversation that the material is GOF's (GOF, GOFCN, 无尽冬日, Whiteout Survival). If they have not said which project it is, ask one short question, 「这是 GOF 还是 GOT 的素材？」, and stop there. Do not guess from file names or content. GOT material follows [got-audit.md](got-audit.md).

## 2. What to review

The files the user sent: the attachment list in their message gives each path (under `input/` in the working directory). Videos, images, or zips of them. Nothing sent yet: ask for the file. A folder cannot be received: ask for a zip. Files sent earlier in the conversation count when the user refers to them.

## 3. Review

`audit review --project GOF --file "input/<name>" [--file "input/<name>"]… [--note "<text>"]`

- One `--file` per file, in the order the user sent them; quote every path (names often have spaces and Chinese). A zip is unpacked and every video and image in it is reviewed.
- `--note`: only what the user said about these files that helps the review (what kind of material it is, where its B part starts, what to look at), in their words. Leave it out otherwise.
- A video takes about a minute (several are reviewed two at a time). Say nothing while it runs; wait until it ends.

## 4. Answer

- `status: done`: reply with `markdown` exactly as it is: for each file, the 级别 / 位置 / 问题 / 怎么改 table. Keep it a Markdown table: do not shorten, reorder, merge or reword it, and add no findings of your own. If the output you read was cut off, read `result_file` (as UTF-8) and reply with that. No introduction, no closing remarks.
- `status: failed` (no file could be reviewed) or `blocked`: tell the user why (`reason`, or the `markdown`, which says why for each file) in one or two sentences.

Follow-up questions about a finding: answer from the review (`result_file`, and the `.json` next to it, which holds every transcribed line) and from the spec: the fixed one in [gof-rules.md](gof-rules.md), the dynamic one from `audit rules show --project GOF` (its `markdown`; no file is written). A corrected file sent again is a new review. The review applies GOF's fixed spec together with GOF's dynamic spec (see [dynamic-rules.md](dynamic-rules.md)); when the user says a finding is wrong, the lasting fix is a clear ruling in GOF's review group, which the dynamic spec picks up.
