# Review ad material (GOF or GOT)

`audit` and its output are as in SKILL.md. Go on only when the user has said which project the material is for; `<P>` below is that project:

| Project | Said as | Fixed spec |
|---|---|---|
| GOF | GOF, GOFCN, 无尽冬日, Whiteout Survival | [gof-rules.md](gof-rules.md) |
| GOT | GOT | [got-rules.md](got-rules.md) (from 《GOT 广告素材审核规则》) |

Every review applies the project's fixed spec together with its dynamic spec ([dynamic-rules.md](dynamic-rules.md)); where they disagree, the dynamic rule wins.

## 1. What to review

The files the user sent: videos, images, or zips of them, at the paths your harness gives for the attachments. Files sent earlier in the conversation count when the user refers to them. Nothing sent yet: ask for the file. A folder cannot be sent: ask for a zip.

## 2. Run the review

`audit review --project <P> --file "<path>" [--file "<path>"]… [--note "<text>"]`

- One `--file` per file, in the order the user sent them; quote every path (names often have spaces and Chinese). A zip is unpacked.
- Videos are reviewed by Gemini. Images are not sent: they come back in `images` for you to review (step 3).
- `--note`: only what the user said about these files that helps the review (what kind of material it is, where its B part starts, what to look at), in their words. For GOT, include what they say about the plan (方案) for the material (how many towers, routes, how long): the fixed spec checks such numbers only when the review is told them. Leave `--note` out otherwise.
- A video takes about a minute (two are reviewed at a time). Say nothing while it runs; wait until it ends.

## 3. Review the images yourself

When the output has `images` (`{name, file}` each), review every image after the command ends:

1. Read both specs: the fixed one (`spec.fixed` in the output) and the dynamic one (`audit rules show --project <P>`, its `markdown`). Apply both; where they disagree, the dynamic rule wins.
2. Open each image (`file`) and look at all of it: every word on it (titles, captions, game UI, buttons, comment and chat screenshots, nicknames, small print), people, logos and brands, game footage, icons. Read the words exactly before judging.
3. Report only breaches of the specs, including something they require that is missing. Taste is not a problem; do not invent rules. One entry per problem.
4. 级别 (level): 红线 for the red-line categories (the fixed spec's section titled 红线, or a dynamic rule marked 红线); 要改 for any other rule broken; 需确认 when the image cannot tell you (whether a person is a celebrity, whether music or a hero is cleared) or the spec leaves it open, saying what to check and with whom (投放 or the review group).
5. If the file name starts with the project code, check it against the naming rules.

Write each image in the shape the command uses for a video, in Chinese:

```
**<name>**（图片）
<one sentence on what the image is>
结论：改完再送审（红线 n · 要改 n · 需确认 n）

| 级别 | 位置 | 问题（原文） | 怎么改 |
|---|---|---|---|
| 要改 | 画面左上角 | <what is wrong, quoting the exact words>〔<rule broken, short>〕 | <a concrete fix; for words, compliant replacement wording> |
```

结论 (verdict) is 改完再送审 when anything is 红线 or 要改, 确认后再送审 when there is only 需确认; with no problem, write just 「结论：可以送审，没有发现问题」 and no table. 位置 is the place on the image, or 文件名 (the file name).

## 4. Answer

- `status: done`: reply with `markdown` exactly as it is: for each file, the 级别 / 位置 / 问题 / 怎么改 table. Do not shorten, reorder, merge or reword it, and add no findings of your own; only a reason after 没审成 (not reviewed) or under 没有审的文件 (files not reviewed) may be put in the user's language. Then add your image reviews, each after a line of `---` (with only images sent, `markdown` is empty: reply with the image reviews alone). If the output you read was cut off, read `result_file` (as UTF-8) and reply with that. No introduction, no closing remarks.
- `status: failed` (no file could be reviewed) or `blocked`: tell the user why (`reason`, or the `markdown`, which says why for each file) in one or two sentences.

A corrected file sent again is a new review. When the user says a finding is wrong, the lasting fix is a clear ruling in the project's review group, which the dynamic spec picks up.
