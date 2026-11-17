# Feishu commands

Everything Feishu goes through `gof feishu …`, which runs the agent's Feishu module's bridge (`feishu.py serve` of `FEISHU_CLI`, with the same Python and environment; the contract is the module's `bridge.md`). The login is the agent's and is shared with the user's other skills: one done before counts here; this skill never logs in. No output contains a token. When the host names the user (`AGENT_USER_OPEN_ID`), another account's login is rejected.

```text
gof feishu doctor                                   # bridge, the user's login, the scopes this skill needs
gof feishu document --url <link> [--sheet <tab id or title>] [--full]
gof feishu document --url <docx link> --outline [--full] | --block <index>
gof feishu doc-edit --url <link> --edit <edit>.json [--check]
gof feishu doc-screenshot --url <link> --output <new>.png [--block <id>] [--sheet <sheet_id> --range <A1>]
gof feishu snapshot --output <new>.xlsx
gof feishu context --snapshot <snapshot>.xlsx [--worksheet <name>]
gof feishu plan --request <request>.json --snapshot <snapshot>.xlsx [--output <new plan>.json]
gof feishu dropdown-add --field primary|secondary|form|supplier|reviewer --option <value> --sheet <sheet name>
gof feishu prepare --plan <approved plan>.json --snapshot <fresh>.xlsx
gof feishu append --batch <batch>                   # the shipped assets/overwrite-receipt.json; --receipt <file> to override
gof feishu reconcile --batch <batch>
gof feishu screenshot --batch <batch> --output <new>.png
```

The progress sheet is the Wiki node `GH7cwIxzZi3gVdk2s3tc1dDbnQc`; the bridge resolves the real spreadsheet.

- `document` reads a company `wiki`, `docx` or `sheets` link: `title`, wiki `path` (the folders above, outermost first); for a sheet the `?sheet=` tab (else the last visible one, `chosen_by: last`; `--sheet` picks another), `tabs`, `columns`, `header` and the non-empty `rows` (`row` = sheet row; attachments `[attachment:<name>]`, images `[image]`), cells clipped at 300 characters unless `--full`, up to 300 rows (`more_rows`: the grid is taller than what was read; `row_count` counts rows with something in them). A docx: `text`, clipped at 30,000 characters unless `--full`.
- `context`: the target sheet and its ID, `next_number`, `naming` (how the next row is named, in header words: `GOFCN<素材编号>_<一级标签>_<二级标签>_<创意形式>_<提炼词>`, an empty 创意形式 dropping out with its `_`), and the current dropdown `options` of the five fields (read from the API rules: the XLSX's lists are unreliable).
- `snapshot` exports the full XLSX (formulas included) and checks the revision did not move during the export.
- `plan` builds `batch_plan` and `api_plan`. Each new row gets the naming formula of the row above when that one names rows `GOFCN<编号>_<一级>_<二级>_<创意形式>_<提炼词>` (kept as colleagues spell it, wrapped in `SUBSTITUTE(…,"__","_")` when the rows have no 创意形式), else `=SUBSTITUTE(CONCATENATE("GOFCN",C,"_",H,"_",I,"_",K,"_",D),"__","_")`; the names are what it gives. With `--output` it saves both and prints a summary: sheet, range, numbers, `naming`; `same_name_rows` (rows already in the sheet under a `material_name` the request gives), `variant_rows` (rows a given `material_name` may be a variant of: named as its start, cut at a `_` or within its first word); per group the requirement name, supplier, reviewer, link, first and last item name, `message_kind`, `default_keyword_rows` (rows with the default 提炼词 `关键词`); and `new_options` (`field`, `option`) the dropdowns lack. Keys without content are left out.
- `dropdown-add` adds the option to every dropdown rule of that column (each rule keeps its options, colors and mode; cell values untouched; ranges that have it already are skipped). Run again it changes nothing: when every rule of the column has the option it is refused with "no range needs this option", which after an earlier run of the same command means the option is in. A column without any dropdown is refused at `prepare` as well.
- `prepare` makes the approved plan again on the fresh snapshot and creates the batch; it refuses if rows, numbers, values, names or groups differ from the approved plan (revision changes elsewhere are fine), and refuses a request whose earlier batch was sent unverified and has rows in the sheet.
- `append` runs the preflight (receipt, user, revision, fresh XLSX, hidden columns, formulas, merges, numbers, rules), then one write with `allow_overwrite:false`; the batch's rows get the dropdown of the rows above. Then a full export and a read-back of the rules; success records `append_verified`. Writes are never retried by the tool: an unknown result is settled by `reconcile`.
- `screenshot` pictures the progress sheet on the batch's appended rows, in the user's hidden browser (the Feishu login covers the web page too).

## Interruption recovery

`gof batch status`, then `gof feishu reconcile --batch …`: read-only; it exports again, reads back all rules and builds a new rule proof and verification report against the original `api_plan`. Its `snapshot` and `validation_evidence` feed `gof batch verify` and `gof batch record --step resumed`.

- Complete: reuse the original CSV; if `append_verified` is not recorded yet, record it with the reconcile evidence, then continue (check existing drafts first if the CLS stage had started).
- An attempted write is never sent again (the tool refuses), even when everything reads back empty. Partial, occupied or conflicting results go to the user. When the new rows lack their planned dropdowns, reconcile still gives the values' verdict (`not_applied`, `partial`, `conflict`) with the rule problem in `rule_issue`.
- A `conflict` with `matched_cells` equal to `expected_cells` is someone else's edit in the new rows (every cell the batch wrote is in place): the batch can never verify, and `gof batch adopt` takes its rows over for their ticket ([data-contract.md](data-contract.md#rows-already-in-the-sheet-in-no-batch-gof-batch-adopt)), closing it.
- Every refusal of `append` after the write went out carries `write_sent: true`.
- Verification of an API batch is strict on the appended rows (every column) and on the appended columns' dropdown rules. Colleagues' edits elsewhere in the shared workbook while it runs (other cells, tabs, filters, sizes, hidden rows, rules of other columns) are listed in `foreign_changes` and do not fail it.
- A batch never attempted can continue if the baseline, the rules and the approved revision are unchanged.
- Batches from the lark-cli era (`cli_version` in `api-plan`) cannot be read back.

## Editing a requirement document

`doc-edit` writes into the user's requirement document as the user (never into the progress sheet, which only `append` writes). One edit per file, either:

```json
{ "insert": { "at": { "before": 0, "expect": "1023——AI片头衔接需求" }, "blocks": [
  { "heading1": "1027-AI片头需求" },
  { "text": "需求数量：15条" },
  { "table": [["命名", "方向"], ["GOFCN67867_AI片头衔接_模拟经营_3D_巨手", "巨手"]], "widths": [300, 100] }
] } }
```

```json
{ "fill": { "table": 2, "expect": "命名 | 方向", "cells": [{ "row": 1, "col": 0, "text": "GOFCN67867_AI片头衔接_模拟经营_3D_巨手" }] } }
```

```json
{ "fill": { "sheet": "2026.10.27", "cells": [{ "cell": "B5", "text": "GOFCN67867_AI片头衔接_模拟经营_3D_巨手" }] } }
```

- Docx places come from `--outline`: `index` (top-level blocks from 0) and `expect` = that block's `text` as the outline showed it (a table: its first row, cells joined by ` | `). If the block there shows something else, the one block with that text is taken; none or several: refused (read the outline again). `at` may also be `"start"` or `"end"`; `{ "after": <index>, "expect": … }` puts the blocks below it.
- Blocks: `text`, `heading1`-`heading3`, `bullet`, `ordered` (a string, or runs `[{ "text", "bold"?, "link"? }]`), `divider` (`true`), `table` (rows of strings, all as wide; optional `widths` per column, `header`). Up to 50 blocks, 400 cells a table.
- `fill.table`: `row` / `col` from 0 as in the outline's `cells`, header row included. A cell must be empty (one empty text block); a cell with anything else, or under a merged cell (`null` in the outline), is refused. Sheets: A1 cells of one tab (`sheet` = title or sheet_id; the link's `?sheet=` otherwise), written with `allow_overwrite:false`; text cannot start with `=`. Up to 100 cells.
- `--check` resolves everything against the document as it is now and writes nothing (`edit_ready`). Without it: the write and a read-back, `doc_updated`. The same edit again finds it in place: `already_present`, nothing written. A lost answer: run the same edit again (it reads first; the request also carries an ID Feishu takes as the same write).
- `doc_updated` and `already_present` carry `focus`: `block` (docx: the first new block, or the table filled) or `sheet_id` + `range` (the cells filled). `doc-screenshot` opens the document there in the user's hidden browser and saves a PNG; it changes nothing. The progress sheet is refused (its picture is `feishu screenshot --batch`).
- Needs the `docx:document` permission in the Feishu login; an older login lacks it: log the user in again (`feishu_login` with `extra_scopes` `docx:document`). Feishu refusing the edit (`HTTP 403`, `1770032`) means the user has no edit access to that document.

## Overwrite probe (maintainers)

The receipt of the 2026-10-20 probe ships as `assets/overwrite-receipt.json`, bound to bridge `feishu-sdk-bridge/1`. After a bridge version change: log in again with `feishu_login` and `extra_scopes` `sheets:spreadsheet:create`, run `gof feishu probe --receipt <new>.json` (creates a new `GOFCN_API_SAFETY_*` workbook and tries writes into occupied cells), replace the shipped file, and record the server's "non-empty write refused" code in `OCCUPIED` of the Feishu module's `feishu_sdk/api.py`.
