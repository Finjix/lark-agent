# GOT 审片 (review GOT ad material)

`audit` = `python "<skill-dir>/scripts/audit.py"` (see SKILL.md). It prints progress lines while it works and ends with one line of JSON. Exit code 2 means it refused or failed (`{"status":"blocked","reason":…}`).

## 1. Which project

This workflow reviews GOT material only. Go on only when the user has said in this conversation that the material is GOT's. If they have not said which project it is, ask one short question, 「这是 GOF 还是 GOT 的素材？」, and stop there. Do not guess from file names or content. GOF material follows [gof-audit.md](gof-audit.md).

## 2. What to review

The files the user sent: the attachment list in their message gives each path (under `input/` in the working directory). Videos, images, or zips of them. Nothing sent yet: ask for the file. A folder cannot be received: ask for a zip. Files sent earlier in the conversation count when the user refers to them.

## 3. Review

`audit review --project GOT --file "input/<name>" [--file "input/<name>"]… [--note "<text>"]`

- One `--file` per file, in the order the user sent them; quote every path (names often have spaces and Chinese). A zip is unpacked. Videos are reviewed by Gemini; images are not sent, they come back in `images` for you to review (step 4).
- `--note`: only what the user said about these files that helps the review (what kind of material it is, where its B part starts, what to look at), in their words. Leave it out otherwise.
- A video takes about a minute (several are reviewed two at a time). Say nothing while it runs; wait until it ends.
- What the user says about the plan (方案) for this material (how many towers, routes, how long) goes in `--note` too: the fixed spec checks such numbers against the plan only when the review is told it.

## 4. Images: review them yourself

When the output has `images` (`{name, file}` each), review every image yourself, after the command ends:

1. Read the spec: the fixed one in `spec.fixed` ([got-rules.md](got-rules.md)), and the dynamic one from `audit rules show --project GOT` (its `markdown`). Where they disagree, the dynamic rule wins. Apply both.
2. Open each image (`file`) and look at all of it: every word on it (titles, captions, game UI, buttons, comment and chat screenshots, nicknames, small print), people, logos and brands, game footage, icons. Read the words exactly before judging.
3. Report only breaches of the spec, including something it requires that is missing; taste is not a problem and do not invent rules. One entry per problem.
4. 级别: 红线 for the red-line categories (the fixed spec's section titled 红线, or a dynamic rule marked 红线); 要改 for any other rule broken; 需确认 when the image cannot tell you (whether a person is a celebrity, whether the music or a hero is cleared) or the spec leaves it open, saying what to check and with whom (投放 or the review group).
5. The file name counts: if it starts with the project code, check it against the naming rules.

Write each image in the same shape as a video in `markdown`:

```
**<name>**（图片）
<one sentence on what the image is>
结论：改完再送审（红线 n · 要改 n · 需确认 n）

| 级别 | 位置 | 问题（原文） | 怎么改 |
|---|---|---|---|
| 要改 | 画面左上角 | <what is wrong, quoting the exact words>〔<rule broken, short>〕 | <a concrete fix; for words, compliant replacement wording> |
```

结论 is 改完再送审 when anything is 红线 or 要改, 确认后再送审 when there is only 需确认, and with no problem just 「结论：可以送审，没有发现问题」 and no table. Write 位置 as the place on the image, or 文件名.

## 5. Answer

- `status: done`: reply with `markdown` exactly as it is: for each file, the 级别 / 位置 / 问题 / 怎么改 table. Keep it a Markdown table: do not shorten, reorder, merge or reword it, and add no findings of your own. Then add your image reviews (step 4) after it, each separated by a line of `---` (with only images sent, `markdown` is empty: reply with your image reviews alone). If the output you read was cut off, read `result_file` (as UTF-8) and reply with that. No introduction, no closing remarks.
- `status: failed` (no file could be reviewed) or `blocked`: tell the user why (`reason`, or the `markdown`, which says why for each file) in one or two sentences.

Follow-up questions about a finding: answer from the review (`result_file`, and the `.json` next to it, which holds every transcribed line) and from the spec: the fixed one in [got-rules.md](got-rules.md), the dynamic one from `audit rules show --project GOT` (its `markdown`; no file is written). A corrected file sent again is a new review. The review applies GOT's fixed spec (from 《GOT 广告素材审核规则》) together with GOT's dynamic spec (see [dynamic-rules.md](dynamic-rules.md)); when the user says a finding is wrong, the lasting fix is a clear ruling in GOT's review group, which the dynamic spec picks up.
