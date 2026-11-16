# CLS API operations

Login is in [cls-session.md](cls-session.md). After login everything is HTTP sent by `gof cls …` to `creative.centurygames.cn`.

## Commands

`gof` = `python <skill>/scripts/gof.py` (`<skill>` = the requirement-auto skill directory).

| Command | Purpose |
|---|---|
| `cls login --qr <png>` / `cls session` | log in / check or take the session |
| `cls catalog --output catalog.json` | projects, people, directions, task types, currencies; prints `choices` (GOFCN directions, creative task types, active suppliers and people by name) |
| `cls observe --action save_draft\|import_scripts [--draft ID] --output observations.json` | what CLS says now: account, create/import permission, draft unpublished + editable + no scripts |
| `cls --batch … --group … --action … --catalog … --observations … --output …` | offline: build this group's save/import operation file (new file in the batch dir) |
| `cls --preview --request request.json --catalog catalog.json` | offline: each group's planned fields, IDs, creator, defaults and `cls_names` as matched |
| `cls run --batch <batch> --operation <op> --output <new receipt>` | send the operation, read back, write the receipt |
| `cls read-draft --id N` | read a draft |
| `cls verify --batch … --operation … --draft N --output …` | read back an existing draft into a receipt (after an unknown result) |

Matching: project GOFCN; direction by project and name; task type from the real options. Supplier (active, outsource role) and creator (active) are the sheet's supplier and reviewer: exact CLS name, else the one whose bracket or dash part is that name ("北京柒曦文化传媒有限公司（丁杰）", "…（个体工商户）-杨燃"), else the one containing it ("冯彦蓉-实习生"). Otherwise give the exact CLS name in `cls.supplier` / `cls.creator` ("廿十一" → "长沙廿拾一文化传媒有限公司"). Quote/currency default to 0 / cny.

## Per group

1. `gof batch record --batch B --step cls_started --group 01`.
2. `cls observe --action save_draft --output …`, then build the save operation to `B/save-operation.json`.
3. `gof batch record --step cls_api_attempt --group 01 --evidence attempt.json` (`action`, `operation_id`, `observations`); `cls run` needs it.
4. `cls run --batch B --operation B/save-operation.json --output B/save-receipt.json`: POST `/api/workflow/script/save` with `status=-2`, read back, receipt with the real draft URL.
5. `record --step draft_saved` with evidence: `confirmed`, `unpublished`, `transport=cls_api`, `draft_id`, `url`, operation and receipt paths, and `requirement_number` if returned.
6. `cls observe --action import_scripts --draft ID`, build the import operation (same CSV, recorded draft ID), new `cls_api_attempt`, `cls run` (multipart `script_id`/`file`/`user_id`).
7. Check the read-back (count, order, names, creator, link on the first row and "同上" after, fixed tag, `parent.status=-2`), then `record --step scripts_imported` with `quantity`, `names` and the script IDs.

A business response needs `code=0` even with HTTP 200; relay `code` and message on failure.

## Recovery

Each operation is sent once (`cls_api_attempt` plus a `cls-sent-*.json` marker). An unknown result comes back as `unknown_result:true` with `draft_id` / `operation_id` when known: read back with `cls read-draft --id N` or `cls verify`. If creation timed out with no ID, ask the user for the draft link or ID rather than creating another. A partial import is reported as is. A refusal saying `CLS 暂时无法访问` came before anything was sent: run the same command again later; it is not a lost login.
