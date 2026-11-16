# Data contract and batch commands

`gof batch …` is offline (no network). A refusal prints JSON `status=blocked` with the reason and exits non-zero. New batches come from `gof feishu prepare` (which adds `api-plan.json`), not `gof batch prepare`.

## Request (`request.json`)

Values are placeholders.

```json
{
  "worksheet": "26年1月-GOFCN",
  "groups": [{
    "supplier": "丁杰",
    "reviewer": "张天伟",
    "script_url": "https://centurygames.feishu.cn/wiki/CurrentScript123?sheet=abc123",
    "primary_tag": "AI生成",
    "creative_form": "2D",
    "items": [
      { "quantity": 3, "secondary_tag": "设定3.0" },
      { "quantity": 1, "secondary_tag": "设定2.0" },
      { "quantity": 10, "secondary_tag": "设定3.0" }
    ],
    "cls": { "direction": "<direction>", "task_type": "<task type>" }
  }, {
    "supplier": "杨燃",
    "reviewer": "雷佳威",
    "script_url": "https://centurygames.feishu.cn/docx/CurrentScript456",
    "quantity": 15,
    "primary_tag": "AI迭代",
    "secondary_tag": "题材片头",
    "creative_form": "2D",
    "keyword": "关键词",
    "label": "迭代",
    "cls": { "direction": "<direction>", "task_type": "<task type>", "supplier": "<exact CLS name, only when needed>" }
  }]
}
```

- `worksheet` (optional): default is the newest year-month GOFCN sheet with valid columns; a tie fails.
- A **group** is one CSV and one CLS ticket: one supplier, reviewer, script link and set of CLS fields (rows for finished materials: neither, see below). Groups are appended in one range, numbers running on in order.
- Rows: either all alike (`quantity` plus the fields on the group) or `items`, runs of rows in order, each with `quantity` and what differs (`primary_tag`, `secondary_tag`, `creative_form`, `keyword`; group values are defaults). With `items`, a group `quantity` is allowed only as their sum.
- Per row: `primary_tag`, `secondary_tag` required, one part of the name each (no `_`); `creative_form` optional, only when stated (no `_`; empty stays empty; all rows of the request or none); `keyword` (提炼词) defaults to `关键词` (the user confirms that default; rows with 提炼词 of their own are `items` of `quantity` 1), may hold `_` but not at its ends or twice in a row, and cannot contain `/` or `\` (the name is also the CLS creative name). The name is `GOFCN<素材编号>_<一级标签>_<二级标签>_<创意形式>_<提炼词>` (`naming`); an empty 创意形式 drops out with its `_`. The plan keeps the formula of the row above when it names rows that way, else writes `=SUBSTITUTE(CONCATENATE("GOFCN",C,"_",H,"_",I,"_",K,"_",D),"__","_")`.
- `material_name` (optional, per item or group): the name the rows must get, without the number (a leading `GOFCN<digits>_` or `GOFCNxxxxx_` is dropped), e.g. a file named for its row; the plan refuses when the formula makes another name, saying which. Older requests give it instead of the tags (`<一级>_<二级>_[<创意形式>_]<提炼词>`), and the tags are read from it.
- `quantity`: positive integer. `reviewer` is required. `script_url` (company `wiki`/`docx`/`sheets` link as given, or `无` when the user says there is no document) is required for a group with a CLS ticket (`cls`) and goes into its CSV; a group without `cls` may leave it out (absent or `""`), and then has no CSV (numbers reserved before the requirement is written, or rows for finished materials, below). `supplier` is required for rows for finished materials (no `script_url`: they were made by someone, who is also the uploads' 制作人); a requirement may leave it out only when its supplier is not decided yet (absent or `""`: the cell stays empty, CLS takes `待定供应商`); a `supplier` equal to the `reviewer` is written as an empty cell (the plan summary says `supplier_cell_empty`), the person still being the CLS supplier, the message's 供应商 and the uploads' 制作人; the cell is filled in every group of a request, or in none.
- `label` (optional, 1-20 characters, no brackets or stars): the requirement's short name in the group message; default the 一级标签. Printed by `plan` as `message_kind`.
- `cls.direction`, `cls.task_type` are required unless the user excluded the ticket (then omit `cls`). `cls.requirement_name` is rejected: `gof cls` builds it as `GOFCN<first>-GOFCN<last>` (one row: `GOFCN<number>`).
- Optional CLS keys: `subject`, `department`, `market_contact`, `currency`, `quotation`, `expected_completion`, `storyboard_time`, `first_draft_time`, `final_cut_time` (dates `YYYY-MM-DD`), and `supplier` / `creator` as exact CLS names when automatic matching fails (without a `supplier` on the group: `待定供应商`).
- `reviewer` is also the CLS creator; `creator` cannot differ from it.
- `client`, `creative_expression`, `creative_batch`, `head_reference`, `creative_reference` must be empty.
- A later ticket for an already appended batch reuses its CSV; do not `prepare` or append again. Rows appended without a ticket get one with `gof batch ticket` (below).

## Ticket for rows appended without one (`ticket.json`)

```json
{ "script_url": "https://centurygames.feishu.cn/wiki/DocOfTheSupplier", "cls": { "direction": "<direction>", "task_type": "<task type>" }, "label": "片头" }
```

Only `script_url` (a document link or `无`), `cls` (as in a request; `direction` and `task_type` required) and `label`. Everything appended (rows, names, tags, supplier, reviewer) stays as approved. `gof batch ticket --preview` checks it against the catalog and the batch and prints the requirement name, quantity, first and last name, `cls_names`, `native_defaults` and `message_kind`, writing nothing; without `--preview` it writes the group's CSV into the batch and records `ticket_attached` (allowed once the rows are verified, for a group approved without `cls`, until its CLS stage starts; attaching again replaces it). From then on `gof cls`, `record`, `message` and `report` take the group with that ticket.

## Rows already in the sheet, in no batch (`gof batch adopt`)

Rows registered by hand or by someone else, or whose batch is gone (a conversation that broke off, a batch removed after 7 days), get a batch with `gof batch adopt --numbers <numbers> --snapshot <fresh export> [--supplier <name>] [--reviewer <name>]`, so that `gof batch ticket` can give them a ticket. Nothing is written to the sheet: the rows are taken as they are (素材名称, tags, 创意形式, 提炼词, and the supplier and reviewer unless given; the rules for naming a new row, such as no `_` in a tag or no `/` in a 提炼词, do not apply to them), one group `01` in number order, recorded as `rows_adopted`, which counts as verified rows. `--numbers` as `gof creative rows` takes them (`68075-68081`, `GOFCN68075-GOFCN68081`, a list), and they must be one run: the ticket is named `GOFCN<first>-GOFCN<last>`, so rows with a gap between them are taken over run by run. Refused only for a number the sheet lacks, numbers that are not one run, a number on rows of different names, rows of different suppliers or reviewers without `--supplier` / `--reviewer`, an empty supplier or reviewer without them, a row without a 一级/二级标签 in its cells or its name, and numbers another batch of this user already holds (it names that batch: continue with it). `append`, `reconcile` and `verify` refuse such a batch; `resumed` needs no snapshot; there is no progress-sheet screenshot.

A batch whose write went out but never verified holds its numbers too. When it can never verify because someone else filled cells of its new rows that the tool leaves empty (`reconcile` says `conflict` with `matched_cells` equal to `expected_cells`), `adopt` takes its rows over once every cell that batch wrote into them is in the sheet exactly as written (otherwise it refuses, naming the cells that differ), and closes it: the old batch records `taken_over` (with the new batch in `by`; `status` and `list` show it) and takes no further step, so the rows cannot get a second ticket there. Its other rows are in no batch from then on and are taken over like any such rows. Appending the same rows again stays refused (`prepare`, as for any write that went out unverified).

## Rows for finished materials

The upload skill (gof-creative-upload) registers materials that are already made: one group, no `script_url` and no `cls`, so the batch has no CSV, `gof cls` refuses to build a ticket for it, the plan summary shows no `requirement_name` / `message_kind`, and `batch message` has nothing to say. Each file is one item of `quantity` 1, in file order (`gof creative files` lists them with their `parts`). For files named `编号_一级_二级_[创意形式_]提炼词`, everything after the 创意形式, or after the 二级标签 when the name has none, is the 提炼词, `_竖版` / `_横版` and versions included, and the file's name goes into `material_name`, so the row's name stays the file's. Such a `material_name` is always `GOFCNxxxxx_…`: a file named with its number has most likely been registered already and is verified with `gof creative files --check`, and `plan` refuses a numbered name for these rows (a requirement's sample name keeps dropping its old number). Two versions of one piece, no 创意形式:

```json
{ "groups": [{ "supplier": "廿十一", "reviewer": "金莉",
  "items": [
    { "quantity": 1, "primary_tag": "AI玩法优化", "secondary_tag": "火车运输御敌", "keyword": "兵临城下塔防_冻鱼_竖版",
      "material_name": "GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_冻鱼_竖版" },
    { "quantity": 1, "primary_tag": "AI玩法优化", "secondary_tag": "火车运输御敌", "keyword": "兵临城下塔防_企鹅_竖版",
      "material_name": "GOFCNxxxxx_AI玩法优化_火车运输御敌_兵临城下塔防_企鹅_竖版" }
  ] }] }
```

With 合并变体名称 (the user turned it on), the variants of one piece are one item: its 提炼词 is the first part after the tags (and the 创意形式), the shorter one when a first part starts with another (`得加钱WM熔炉版` goes with `得加钱WM`), and `material_name` is that shorter name. A merged name that `plan` lists in `variant_rows` (a variant of a row already in the sheet) is not appended again. Each file then goes up as `<素材名称>_<变体>` (the upload request's `"variants": true`, see gof-creative-upload). Five files `GOFCNxxxxx_AI剧情_短剧表达_原生家庭WM_剧情衔接_<…>_竖版`, merged:

```json
{ "groups": [{ "supplier": "自产", "reviewer": "王维明",
  "items": [
    { "quantity": 1, "primary_tag": "AI剧情", "secondary_tag": "短剧表达", "keyword": "原生家庭WM",
      "material_name": "GOFCNxxxxx_AI剧情_短剧表达_原生家庭WM" }
  ] }] }
```

Batches are told apart by their script links; groups without one by their rows' names without the number, so `prepare` refuses files whose earlier batch was sent unverified and has rows in the sheet.

## CSV

UTF-8 with BOM, CRLF, header `脚本名称,脚本内容,创意标签`. First row of each group has the script link (or `无`), the rest `同上`. Tag is always `玩法卖点（单选，必选）/展示向/展示向`. `assets/csv-template.csv` is a reference only.

## Commands

```text
gof batch plan --request request.json --snapshot downloaded.xlsx --observed-url <real URL>
gof batch prepare --plan approved-plan.json --snapshot fresh.xlsx --observed-url <real URL> [--output-root <dir>]
gof batch preflight --batch <batch> --snapshot fresh.xlsx --observed-url <real URL>
gof batch verify --batch <batch> --snapshot after.xlsx --observed-url <real URL>
gof batch record --batch <batch> --step <step> [--group 01] [--evidence file.json] [--snapshot fresh.xlsx --observed-url <real URL>]
gof batch cls-check --batch <batch> --group 01 --action save_draft|import_scripts --observations observations.json
gof batch status --batch <batch>
gof batch list [--number <material number>]
gof batch adopt --numbers <numbers> --snapshot fresh.xlsx [--supplier <name>] [--reviewer <name>]
gof batch ticket --batch <batch> --group 01 --ticket ticket.json --catalog catalog.json [--preview]
gof batch message --batch <batch>
gof batch report --batch <batch> [--output <batch>/<new report>.json]
```

- `plan` prints the plan JSON (plan_id, sheet/column mapping, append range, values, formulas, names, dropdown options). Historical duplicate numbers become `history_warnings`; an unreadable existing number that could clash stops it.
- `verify` returns `complete`, `not_applied`, `partial` or `conflict` (history changed, wrong field or unplanned addition). Feishu may extend an existing filter to the new last row; other filter changes are `conflict`. For a batch written through the API (`gof feishu append`) only the appended rows and the written columns' dropdowns can make it `conflict`; colleagues' edits elsewhere come back as `foreign_changes`. Its new rows' dropdowns are judged by the rules read back through the API (the rule proof), not by the lists the XLSX export shows, which can differ.
- `report` prints the report (with `links`); `--output` saves a copy, which must lie inside the batch directory.
- `list`: the user's batches, newest first: `batch` (its directory), sheet, `first_number` / `last_number`, `append_verified`, `blocked`, and per group quantity, first and last name, supplier, reviewer, `ticket` (whether it has a CLS ticket, approved or attached) and `steps`; `adopted` for a batch made by `adopt`, `taken_over` (the batch that took them) for one whose rows `adopt` took over. `--number` keeps the batches holding that number among their rows (`67867` or `GOFCN67867`).
- `record` steps: `append_started`, `append_verified`, `rows_adopted` and `taken_over` (written by `batch adopt` only), `ticket_attached` (written by `batch ticket` only), `cls_started`, `cls_api_attempt`, `draft_saved`, `scripts_imported`, `handoff`, `blocked`, `resumed`, `requirement_number`. Events are append-only; after `taken_over` a batch takes no step. Evidence containing credentials is rejected.
  - `append_verified` / `resumed`: `evidence.validations` = `{ "transport": "feishu_api", "proof": "<absolute path of this batch's rule proof>" }`.
  - `cls_api_attempt`: `action` (`save_draft` / `import_scripts`), `operation_id`, `observations`. One attempt per action per group.
  - `draft_saved`: `confirmed=true`, `unpublished=true`, `url`, `transport=cls_api`, `draft_id`, absolute operation and receipt paths; optional `requirement_number` (official `GOFCN` number actually read). `scripts_imported` adds `quantity` and `names`.
  - `handoff` and `blocked` need a `reason`.
  - `requirement_number`: `confirmed=true` and the official number (from the user or a read-back); then regenerate the message.
  - `resumed` (`resume.json`): `confirmed=true`, `reason`, plus `cls_reconciled=true` if CLS had started; run with `--snapshot fresh.xlsx`.
- `observations.json` comes from `cls observe`: `logged_in`, `can_create`, `unpublished` true; saving also `required_complete`; import also `draft_saved`, `import_enabled`, `import_would_publish=false`.
- `report` writes a local JSON report (sheet, range, stage states, CLS receipts, message, `links`).
- `message`: `老师好 刚刚发了新的工单`, per group `需求编号：<number>（<label>*<quantity>）` and `供应商：<supplier>` (`待定供应商` when empty), then `创意人：<reviewers, each once, "/">` and `辛苦安排一下~`. Only groups with a ticket appear (`skipped_groups` lists the rest; none at all: `status` `message_not_ready`). Without an official number: `【发布后补填需求编号】`, listed in `missing_numbers`.
