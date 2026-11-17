# Upload finished materials (上传素材)

The user gives finished materials. The whole task: append a progress-sheet row for each file that has none yet → verify → upload the files to CLS 创意列表 under their rows' 素材名称 → save the annotation (创意人, 制作人, 素材类型) and read it back → screenshots of the progress sheet and the creative list. A single step ("只登记进度表", "只上传", "再截一张创意列表") is fine; an upload on its own takes the files' rows from the sheet.

Logins, approval, steps on request, files and how to talk to the user are in [SKILL.md](../SKILL.md). Zips are unpacked into `<data>/tmp`.

## 1. Files and their rows

Run `gof feishu doctor` once; a missing permission goes to the user. Commands: [feishu-commands.md](feishu-commands.md); request files: [data-contract.md](data-contract.md#rows-for-finished-materials).

**Files** are what the user attached (the host lists their paths) and local files or folders they name. `gof creative files <path> [<path> …] --check` lists what they hold (zips unpacked into tmp; it only reads): per file `file`, `name`, `kind`, `number` (a GOFCN number in the name); for a name with the number open (`GOFCNxxxxx_…`, any run of x, X, * or none) `name_without_number` and `parts`; for a name with a GOFCN number its `row` in the progress sheet (below; `--check` reads the sheet when a name has GOFCN in it, about a minute; `--progress <link>` for a sheet the user gives).
- Each file is one creative and one row (merged variants: several files to one row), in the order listed.
- `same_content`: one file twice (CLS takes one content once): one row and one upload; the other file gets `items[].skip` (ask which only if their names say they are different pieces).
- `zip_renamed`: names in a zip with characters a Windows file name cannot hold (`? * : " < > |`), read and named with `_` instead.
- A folder is read one level deep. **A zip is a folder** (a chat cannot hand over a folder): its files at the top, or in the one folder it holds; deeper folders are listed as skipped. Accepted: mp4 mov avi m4v, jpg jpeg png gif webp bmp, html mp3 wav; anything else is listed with the reason (rar / 7z, txt, mkv, nested folders …). The user says they sent a folder and nothing arrived: ask them to zip it and send the zip.

**Each file is one row of the progress sheet** (the skill's, or the one the user links), named `GOFCN<素材编号>_<一级标签>_<二级标签>_[<创意形式>_]<提炼词>`, and its creative gets exactly that 素材名称. A file whose row is in the sheet already is only uploaded, never appended again.

**Files named in full with their GOFCN number** (`GOFCN67680_AI生成_设定3.0_3D_企鹅保龄球.mp4`) have most likely been registered already: their rows are verified, not appended. Each file's `row`:
- `registered`: a row is named as the file: only uploaded.
- `variant`: the file's name is its row's 素材名称 with an ending: a variant of that row (merging, below).
- `differs`: the row of its number (`rows`) is named otherwise; `same_name_rows` lists rows named as the file under another number. Compare tags, 提炼词, the user's words and the documents: the same piece whose name changed since (a typo fixed, a 提炼词 reworded) is uploaded against that row (`items[].number`) under the row's name, and the plan says so; otherwise ask, showing both names.
- `not_found`: no row of its number in that sheet: ask (another progress sheet? `same_name_rows` when there are some).

A file asked about is appended only when the user says it is a new piece; its `material_name` is then `GOFCNxxxxx_…`. Files without a `row` are new, unless a case below says otherwise.

Also only uploaded, never appended:
- named `GOFCNxxxxx_…` with `row` `registered`, or listed in the rows plan's `same_name_rows`;
- materials the user says are for a requirement published earlier ("这是 GOFCN67680-67683 的成片"): `gof creative rows --numbers 67680-67683` or `--find 企鹅保龄球,冻鱼飞盘` (text in 素材名称 or 提炼词, newest first; about a minute, read-only) lists the rows; match each file to its row from the user's words and the requirement or delivery documents (`gof feishu document --url <link>`, `--full` for whole scripts): name, order, version, duration, size and kind against each row's 提炼词, tags, script and place. Rows named alike but for the number are taken in the order the requirement lists them; say so in the plan.

A GOFCN number in a name not written the sheet's way (`GOFCN67680_成片.mp4`, or a heading like `GOFCN56890 企鹅保龄球 V15`, a new piece made from GOFCN56890) does not make the file that row, whatever its `row` says: it is new, unless the user or a document says which row it is.

**New rows.** `gof feishu snapshot --output <tmp>/<new>.xlsx`, then `gof feishu context --snapshot <it>`: target sheet, `next_number`, `naming`, the dropdown `options`. Each new file is one item of `quantity` 1, in file order:
- **Named the sheet's way** (`parts`): 一级标签 = `primary_tag`, 二级标签 = `secondary_tag`; `rest` is the 提炼词 (`keyword`), `_竖版` / `_横版` and version words included, unless it starts with a 创意形式: then that first part is `creative_form` and the rest the `keyword`. Judge from the whole batch: a first part that is a 创意形式 option (`3D`, `2D`, `解说` …) or that the user calls one is; a first part several files share with different endings is the piece's name, not a form (`…_兵临城下塔防_冻鱼_竖版`, `…_兵临城下塔防_企鹅_竖版`: 提炼词 `兵临城下塔防_冻鱼_竖版`, `兵临城下塔防_企鹅_竖版`); in doubt it is 提炼词 (the name is the same either way). Some files with a 创意形式 and some without: leave it in the 提炼词 for all. The file's name goes into `material_name`, which the plan checks comes out exactly. `unclear` (fewer than three parts): ask.
- **Named otherwise** (`01.mp4`, `企鹅保龄球_v15.mp4`): the tags, 创意形式 and 提炼词 come from what the user says or the documents they give (requirement, script or delivery doc): prefer existing dropdown options for the tags; take the 提炼词 as the document writes the part that tells the pieces apart (企鹅保龄球, not the number of the piece it builds on, not the version). What cannot be worked out is asked, never made up.
- A tag or 创意形式 is one part of the name and never holds `_`; a 提炼词 may hold `_` (not at its ends or twice in a row) but not `/` or `\`.
- Values the dropdowns lack (tags, 创意形式, supplier, reviewer) are taken as the files or the user name them; the plan lists them in `new_options`, added after approval.

**合并变体名称 (merge variant names; off unless the user turns it on).** Variants are files of one piece: the same tags (and 创意形式) and the same first part after them, different endings (`…_原生家庭WM_剧情衔接_割资源冰块对抗_竖版`, `…_原生家庭WM_剧情衔接_剧情叙事_竖版`); a first part that extends another one is a variant too (`得加钱WM熔炉版` of `得加钱WM`); so is a file `GOFCN<编号>_…` that starts with that row's 素材名称 (`row` `variant`). When the batch has variants and the user has not said, ask once (with the other open points, else in the plan); no answer means not merged. Merged:
- **Rows:** one per piece, 提炼词 = the shorter first part, `material_name` like `GOFCNxxxxx_AI剧情_短剧表达_原生家庭WM`. Not appended when the row is there already: the files carry its number, or the rows plan lists it in `same_name_rows` or `variant_rows`.
- **Upload:** `"variants": true`; each file goes up as its own name with the number filled in (`GOFCN67146_AI剧情_短剧表达_得加钱WM熔炉版_剧情衔接_割资源冰块对抗_竖版`). A file whose name gives no variant (`01.mp4`) takes `items[].number` and `items[].variant`.

**People** (the first source that answers wins: the user's message, this conversation, the documents, the folder or zip name; else ask):
- **设计师供应商** (who made the materials) is also the uploads' **制作人**. Required (never `待定供应商`); spelled as in `options`.
- **创意者脚本审核人** is also the uploads' **创意人**. Required.
- **The same person** (as the user says): `supplier` is still that person and the plan leaves the sheet's 设计师供应商 cell empty, so give the person as top-level `producers` of the upload request. An existing row with an empty 设计师供应商: ask who made it and give it in `items[].producers`.
- The sheet spells people short, CLS in full: `gof creative options --creator <审核人> --producers <供应商>[,<供应商>…]` matches them as the upload will (`people`: each name's CLS name, or the problem). One it cannot match: the name in its `creators` / `producers` lists that is clearly the same (廿十一 → 长沙廿拾一文化传媒有限公司), or ask; two people of one name: their CLS user ID, or ask.

**Upload fields:**
- **上传目录:** `双端共用-视频` unless the user names another; `gof creative options --find <text>` finds a directory by name; a path ending (`常规素材/图片`) picks one of two with the same name.
- **素材类型:** by the file: a picture → 图片, a video → 视频, anything else → 其他. Another type only when the user states it (`items[].type`, e.g. `AI生成`).
- **创意标签:** not filled.

Ask all open points once, in one short message. The merge question, when it is the only one, goes into the plan instead.

## 2. Plan

1. **Rows:** write `<tmp>/request.json` for the new files ([data-contract.md](data-contract.md#rows-for-finished-materials)): one group with `supplier`, `reviewer` and one item per file (merged: per row), no `script_url` and no `cls`. Run `gof feishu plan --request … --snapshot … --output <tmp>/plan-<time>.json`. A refusal: fix the request, or ask when it shows something cannot be worked out. A file in `same_name_rows` is most likely registered: leave it out of the request and upload it against that row, unless the user says it is a new piece.
2. **Files already in the sheet** (if any): check them now with `gof creative plan` (§4 step 1) on a request of only those files; it reports a row whose creative CLS already has.
3. Show the plan, about 5-8 lines: the progress sheet and the rows to append, one per line, each as its **full 素材名称** with the expected number exactly as the plan names it (`GOFCN67746_AI玩法优化_搂冰块抢资源_割资源冰块对抗_竖版`; never split, shortened or put in a table), with `<file name> → ` in front only when the file is named otherwise, and where the tags and 提炼词 came from only when not from the file names; merged rows the same way, with nothing about their variants or upload names; the files already in the sheet (how many, only uploaded); dropdown values to add; then 上传目录, 创意人 / 制作人 (CLS names) and 素材类型 in one line; files that will not be uploaded and why. Variants the user has not decided on are planned unmerged, and the plan asks in one line before its last one whether to merge them. End by saying you will start once they confirm. A correction (merging included): plan again and show it again.

When every file is in the sheet already there are no rows: the plan is the upload part, from `gof creative plan`.

## 3. Execute: append the rows

As in [requirement.md](requirement.md) §2: `gof login check`; `dropdown-add` for each `new_options` entry; a fresh snapshot and `gof feishu prepare` (changed rows, numbers or names: show the new numbers and wait); `gof feishu append`; recovery with `gof batch status` and `gof feishu reconcile` after an interruption, `write_sent` or an unclear result. Complete: continue with the upload. Partial or conflicting: report what exists and upload nothing for those files.

## 4. Execute: upload

1. Write the upload request (paths absolute; `items` only for what differs per file) and run `gof creative plan --request <tmp>/upload-request.json --output <tmp>/upload-plan-<time>.json`:

```json
{
  "files": ["D:/…/input/玩法AI迭代素材.zip", "D:/…/input/01.mp4"],
  "directory": "双端共用-视频",
  "producers": ["长沙廿拾一文化传媒有限公司"],
  "items": [
    { "file": "01.mp4", "number": 67742 },
    { "file": "封面.png", "number": 67690, "type": "图片", "directory": "常规素材/图片" },
    { "file": "说明.mp4", "skip": true }
  ]
}
```

   - Files named `GOFCNxxxxx_…` are found by their name (`row_from: name`) and go up with the number filled in. A file named exactly as its 素材名称 needs nothing. Every other file takes `items[].number`: its new row's number (the batch's rows are in file order) or the existing row you matched. `items[].file` is the file name; for files of one name in two zips, `<zip name>：<file name>` as `creative files` lists them.
   - Merged: `"variants": true`; files are found by the row their name starts with, others take `items[].number` and `items[].variant` (`{ "file": "01.mp4", "number": 67747, "variant": "剧情衔接_扩岛对抗_竖版" }`). Two files of one name, or a file that fits two rows, are refused with what to give.
   - `creator` / `producers` at the top replace every file's people from its row: give them (the CLS names from §1) only when they hold for every file; for one file, `items[].creator` / `items[].producers`. Files without them take their row's 创意者脚本审核人 and 设计师供应商; an existing row with an empty 设计师供应商 makes the plan refuse (制作人 not decided): ask who made the file and give it in `items[].producers`.
   - Rows the sheet cannot name, as the plan says: a number on rows of several tabs that differ takes the tab in `items[].sheet`; a row whose 素材名称 is empty, cannot be worked out or contains `/` or `\` needs the user: they fix the sheet, or confirm a name you then give in `items[].name` (only such a row takes one). Only for materials the user says are on no progress sheet: `"progress": false` (nothing appended; the files keep their names or `items[].name`).
   - It only reads: the files (hashed), what CLS offers, the progress sheet (about a minute), and whether a name already exists in CLS (CLS refuses a whole upload for one taken name). A refusal lists everything unsettled at once: fix what you can, ask the user the rest in one short message. The result gives per file its `name`, `renamed`, `progress_row` and `row_from`. No second approval is needed when it puts up the approved files under the approved names with the approved people, directory and types; otherwise show what differs and wait.
2. `gof login check`, then `gof creative run --plan <upload plan>`. It checks the files are unchanged and the people, directory and types still on offer, uploads each file under its planned name, creates the creatives (= 下一步), saves 创意人 / 制作人 / 素材类型 (= 保存) and reads them back. It never creates twice; run again on the same plan, it goes on from where it stopped. Results:
   - `uploaded`: every file is a creative with its annotation (`uploaded[]`: name, type, 创意人, 制作人, `adsdesk` 同步中 / 已同步).
   - `partial`: some files did not become creatives (`not_created`, each with its reason: CLS has the same file already (MD5), or CLS refused) or an annotation is missing (`annotate_error`, `uploaded[].problems`). Report which.
   - `unknown`: a create's answer was lost and some creatives are not found yet (`missing`). Run the same command again a little later: it only looks, it never creates again. Still missing: tell the user which files, and that they can check the list.
   - `blocked`: relay `reason`. CLS out of reach before anything was created is no refusal of the plan: run the same command again a little later. Otherwise, before anything was created, a fixed request means a new upload plan (and approval if it differs); after a create, never make a second plan for the same files without asking.

## 5. Handover

1. When rows were appended: `gof batch report --batch …` (prints the report with its `links`).
2. **Screenshots**, into the working directory: the progress sheet on the appended rows, `gof feishu screenshot --batch … --output <working dir>/progress-<time>.png` (only when rows were appended), and 我的创意 > 创意列表 of this account, newest first, `gof creative screenshot --output <working dir>/creatives-<time>.png --rows <number uploaded, at most 12>`. Open each picture before it goes out: a sign-in page, a "no permission" page, an empty sheet or one without the new GOFCN numbers, or a list without the new creatives is not handed over. A refused or poor picture never fails the task: say in one line which is missing and why (a lapsed Feishu web login: `gof login check`, then once more; a page that did not open, not a lapsed login: once more a little later), and keep the links.
3. Give the user, in one reply: the appended numbers and the progress-sheet link from the report's `links.progress_sheet` ("创意进度表：<link>"); how many files went up under their 素材名称 (each full name as it went up, one per line), what failed and why; and "创意列表：https://creative.centurygames.cn/creativities".
