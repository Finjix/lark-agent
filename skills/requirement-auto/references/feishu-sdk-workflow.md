# Feishu bridge

Everything Feishu goes through `gof feishu …` (`gof` = `python <skill>/scripts/gof.py`), which calls the **feishu skill**'s bridge (`feishu.py serve`, run with the same Python). The feishu skill must be installed next to this one (`skills/feishu/scripts/feishu.py`), or `FEISHU_SKILL` names the absolute path of its `feishu.py`; without it every Feishu command says so. The Feishu login is that skill's and is shared with the user's other skills.

## Runtime

- Python 3.11+ (standard library only). The host sets `LARK_AGENT_USER_DIR` (absolute user dir), optionally `LARK_AGENT_USER_OPEN_ID`, and `FEISHU_APP_ID` / `FEISHU_APP_SECRET`; the feishu skill runs with the same environment. requirement-auto's data is under `<user dir>/requirement-auto/{batches,cls,tmp}`; the Feishu login and the hidden browser under `<user dir>/feishu/`.
- Standalone: requirement-auto's data goes to `~/.requirement-auto/default`; the Feishu login wherever the feishu skill keeps it when run on its own.

```text
gof feishu doctor      # bridge, current user's login and required scopes
```

## Login (device authorization)

```text
gof feishu login start [--probe-scope]   # link and code; the same request while an unexpired one exists
gof feishu login poll [--wait 40]        # after the user confirms: authorized / pending / denied / expired
gof feishu login status                  # local record only, no network
gof feishu login logout                  # log out or switch accounts
```

These and `gof feishu authorize [--qr]` are the feishu skill's `login …` and `authorize …`, run as they are (same output). No output contains a token. The QR path is in [gof-requirement.md](gof-requirement.md) section 0.

If the host declares the user's Open ID, another authorized account is rejected.

## Commands

```text
gof feishu document --url <requirement link> [--sheet <tab id or title>] [--full]
gof feishu document --url <docx link> --outline [--full] | --block <index>
gof feishu doc-edit --url <requirement link> --edit <edit>.json [--check]
gof feishu doc-screenshot --url <requirement link> --output <new>.png [--block <id>] [--sheet <sheet_id> --range <A1>]
gof feishu snapshot --output <new>.xlsx
gof feishu context --snapshot <snapshot>.xlsx [--worksheet <name>]
gof feishu plan --request request.json --snapshot <snapshot>.xlsx [--output <new plan>.json]
gof feishu dropdown-add --field primary|secondary|form|supplier|reviewer --option <value> --sheet <sheet name>
gof feishu prepare --plan <approved plan>.json --snapshot <fresh>.xlsx
gof feishu append --batch <batch>                  # uses the shipped assets/overwrite-receipt.json; --receipt <file> to override
gof feishu reconcile --batch <batch>
```

The target is the test Wiki node `GH7cwIxzZi3gVdk2s3tc1dDbnQc`; the bridge resolves the real spreadsheet.

- `document` reads a company `wiki`, `docx` or `sheets` link: `title`, wiki `path` (folders above, outermost first), and for a sheet the `?sheet=` tab (else the last visible tab, `chosen_by: last`; `--sheet` picks another), `tabs`, `columns`, `header`, non-empty `rows` (`row` = sheet row; attachments `[附件:<name>]`, images `[图片]`), cells clipped at 300 characters unless `--full`, up to 300 rows (`more_rows`: the tab's grid is taller than what was read; its rows may still be empty, `row_count` counts the rows with something in them). For a docx: `text` (clipped at 30,000 characters unless `--full`).
- `context`: target sheet and its ID, `next_number`, `naming` (how the next row is named, in header words: `GOFCN<素材编号>_<一级标签>_<二级标签>_<创意形式>_<提炼词>`), current dropdown `options` of the five fields (read from the API rules; the XLSX's lists are unreliable).
- `snapshot` exports the full XLSX (formulas included) and checks the revision did not move during export.
- `plan` builds `batch_plan` and `api_plan`; each new row gets the naming formula of the row above when that one names rows `GOFCN<编号>_<一级>_<二级>_<创意形式>_<提炼词>` (kept as colleagues spell it, wrapped in `SUBSTITUTE(…,"__","_")` when the rows have no 创意形式), else `=SUBSTITUTE(CONCATENATE("GOFCN",C,"_",H,"_",I,"_",K,"_",D),"__","_")`; the names are what it gives. With `--output` it saves both and prints a summary: sheet, range, numbers, `naming`, `same_name_rows` (rows already in the sheet under a `material_name` the request gives; absent when none), `variant_rows` (rows a given `material_name` may be a variant of: named as its start, cut at a `_` or within its first word; absent when none), per group requirement name, supplier, reviewer, link, first and last item name, `message_kind`, `default_keyword_rows` (how many of the group's rows have the default 提炼词 `关键词`; absent when none), and `new_options` (`field`, `option`) the dropdowns lack.
- `dropdown-add` adds the option to every dropdown rule of that column (each rule keeps its options, colors and mode; cell values untouched; ranges that already have it are skipped). Re-running changes nothing: when every rule of the column has the option already it is refused with "没有任何范围需要新增该选项（已存在，或该列没有下拉）", which after an earlier run of the same command means the option is in (a column without any dropdown is refused at `prepare` as well).
- `prepare` re-makes the approved plan on the fresh snapshot and creates the batch; it refuses if rows, numbers, values, names or groups differ from the approved plan. Revision changes elsewhere are fine.
- `append` runs the preflight (receipt, user, revision, fresh XLSX, hidden columns, formulas, merges, numbers, rules), then one `invoke_write` with `allow_overwrite:false`. This batch's rows get the dropdown of the rows above. Then a full export and rule read-back; success records `append_verified`. Writes are not retried by the tool: an unknown result is resolved by `reconcile`.

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

- Docx places come from `--outline`: `index` (top-level blocks from 0) and `expect` = that block's `text` as the outline showed it (a table: its first row, cells joined by ` | `). If the block there shows something else, the one block with that text is taken; none or several: refused (read the outline again). `at` is also `"start"` or `"end"`; `{ "after": <index>, "expect": … }` puts the blocks below it.
- Blocks: `text`, `heading1`-`heading3`, `bullet`, `ordered` (a string, or runs `[{ "text", "bold"?, "link"? }]`), `divider` (`true`), `table` (rows of strings, all as wide; optional `widths` per column, `header`). Up to 50 blocks and 400 cells a table.
- `fill.table`: `row` / `col` from 0 as in the outline's `cells`, header row included. A cell must be empty (one empty text block); a cell with anything else, or under a merged cell (`null` in the outline), is refused. Sheets: A1 cells of one tab (`sheet` = title or sheet_id; the link's `?sheet=` otherwise), written with `allow_overwrite:false`; text cannot start with `=`. Up to 100 cells.
- `--check` resolves everything against the document as it is now and writes nothing (`edit_ready`). Then the write and a read-back: `doc_updated`. The same edit again finds it in place: `already_present`, nothing written. A lost answer: run the same edit again (it reads first; the request also carries an ID Feishu takes as the same write).
- `doc_updated` and `already_present` carry `focus`: `block` (docx: the first new block, or the table filled) or `sheet_id` + `range` (the cells filled). `doc-screenshot` opens the document there in the user's hidden browser (a docx link with `#<block>`, a sheet on that tab and range) and saves a PNG; it changes nothing. The progress sheet is refused (its picture is `feishu screenshot --batch`).
- Needs the `docx:document` permission in the Feishu login, which current logins ask for; an older login is told to authorize once more. Feishu refusing the edit (`HTTP 403`, `1770032`) means the user has no edit access to that document.

## Interruption recovery

`gof batch status`, then `gof feishu reconcile --batch …`: read-only; re-exports, reads back all rules and builds a new rule proof and verification report against the original `api_plan`. Its `snapshot` and `validation_evidence` feed `batch verify` and `record resumed`.

- Complete: reuse the original CSV; if `append_verified` is not yet recorded, record it with the reconcile evidence, then continue to CLS (check existing drafts first if CLS had started).
- An attempted write is never re-sent (the tool refuses), even when everything reads back empty. Partial, occupied or conflicting results are reported to the user. When the new rows lack their planned dropdowns, reconcile still gives the values' verdict (`not_applied`, `partial`, `conflict`) with the rule problem in `rule_issue`.
- A `conflict` with `matched_cells` equal to `expected_cells` is someone else's edit in the new rows (every cell the batch wrote is in place): the batch can never verify, and `gof batch adopt` takes its rows over for their ticket (see data-contract.md), closing it.
- Every refusal of `append` after the write went out carries `write_sent: true`. A request whose earlier batch was sent but never verified is planned again only when none of that batch's rows are in the sheet (`prepare` refuses otherwise).
- Verification of an API batch is strict on the appended rows (every column) and on the appended columns' dropdown rules. Edits colleagues make elsewhere in the shared workbook while it runs (other cells, other tabs, filters, sizes, hidden rows, rules of other columns) are listed in `foreign_changes` and do not fail it.
- A never-attempted batch can continue if baseline, rules and approved revision are unchanged.
- Batches from the lark-cli era (`cli_version` in `api-plan`) cannot be read back with this bridge.

## Overwrite probe (maintainers)

The receipt of the 2026-10-20 probe ships as `assets/overwrite-receipt.json`, bound to `feishu-sdk-bridge/1`. After a bridge version change: `gof feishu login start --probe-scope`, `login poll`, `gof feishu probe --receipt <new>.json` (creates a new `GOFCN_API_SAFETY_*` workbook and tries occupied writes), then replace the shipped file and record the server's "non-empty write refused" code in `OCCUPIED` in the feishu skill's `scripts/feishu_sdk/api.py`.
