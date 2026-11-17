# CLS: login and draft commands

CLS (`https://creative.centurygames.cn`) sits behind the company's Feishu single sign-on. The login lives in this user's hidden browser, the feishu skill's (its profile holds the user's Feishu web session), so the Feishu QR login normally logs CLS in too. `gof cls …` takes the `Authorization` header of the page's own API calls in that browser, confirms it with `/api/userinfo`, and does all further work over plain HTTP. The token is never written down or printed.

## Login

```text
gof cls login --qr <working dir>/cls-login-<time>.png   # a sign-in QR for the user to scan
gof cls session                                          # check; after the scan, take the login and close the browser
```

- Only when `gof login check` still says CLS is missing after the Feishu login.
- `cls login --qr`: a silent check first (up to 12 s); logged in: `logged_in`. Otherwise it stops on the Feishu QR page, writes the PNG and returns `qr_ready`; the browser waits in the background (closed after 15 minutes without a scan). `no_qr`: the page is not on a QR code: run `cls session` first. With CLS out of reach it makes no code.
- The picture goes to the user, to scan with the Feishu mobile app and confirm on the phone. It expires in a few minutes: run the command again for a new one.
- After the user confirms: `cls session` (about 25 s) joins the waiting browser, presses 【授权】 on the `dsc_admin` consent page (only that application and, once an account is bound, only for that account), takes the login and closes the browser so its cookies persist.
- Whoever scans is who logs in. The first account is bound in `<data>/cls/account.json`; another account is refused, naming the one to use (`expected_account`). The same account under a new name stays bound, and the consent page may show the name without the suffix CLS adds (`钟丰骏` for `钟丰骏-实习生`). Changing the bound account is an administrator's job: delete that file and the feishu skill's browser profile (which also signs Feishu web out), then log in again.
- Online `cls` commands (`catalog`, `observe`, `run`, `read-draft`, `verify`, `screenshot`) take the login themselves; without one they say to run `cls login --qr`.
- CLS out of reach (an outage, a gateway, the network, a proxy) is not a lost login: `gof login check` says `unknown`, commands refuse saying CLS cannot be reached, having sent nothing. Make no QR, ask for no scan; run it again a little later.
- The login persists in the browser profile, so later tasks normally pass without a scan. One browser per profile: one user's `cls` commands run one after another; different users' browsers do not interfere.
- Behind a corporate proxy set `REQUIREMENT_AUTO_USE_ENV_PROXY=1` and `HTTPS_PROXY` (with `NO_PROXY` for hosts that must not go through it); otherwise proxy variables are ignored. The hidden browser is driven over `127.0.0.1`, never through a proxy.

## Commands

| Command | Purpose |
|---|---|
| `cls catalog --output <tmp>/catalog.json` | projects, people, directions, task types, currencies; prints `choices` (GOFCN directions, creative task types, active suppliers and people by name) |
| `cls observe --action save_draft\|import_scripts [--draft ID] --output <file>` | what CLS says now: account, create / import permission, draft unpublished, editable, without scripts |
| `cls --batch B --group G --action A --catalog <file> --observations <file> --output <new file in B>` | offline: build the group's save or import operation |
| `cls --preview --request <request>.json --catalog <file>` | offline: each group's planned fields, IDs, creator, defaults and `cls_names` as matched |
| `cls run --batch B --operation <op> --output <new receipt>` | send the operation, read back, write the receipt |
| `cls read-draft --id N` | read a draft |
| `cls verify --batch B --operation <op> --draft N --output <new receipt>` | read an existing draft back into a receipt (after an unknown result) |
| `cls screenshot --batch B [--group G] --output <new>.png` | picture of the group's draft page |

Matching: project GOFCN; direction by project and name; task type from the real options. Supplier (active, outsource role) and creator (active) are the sheet's supplier and reviewer: the exact CLS name, else the one whose bracket or dash part is that name ("北京柒曦文化传媒有限公司（丁杰）", "…（个体工商户）-杨燃"), else the one containing it ("冯彦蓉-实习生"). Otherwise give the exact CLS name in `cls.supplier` / `cls.creator` ("廿十一" → "长沙廿拾一文化传媒有限公司"). Quote and currency default to 0 / cny.

## Draft per group

1. `gof batch record --batch B --step cls_started --group 01`.
2. `gof cls observe --action save_draft --output <B>/save-observations.json`, then build the save operation into `B/save-operation.json` (`gof cls --batch B --group 01 --action save_draft --catalog … --observations … --output B/save-operation.json`).
3. `gof batch record --batch B --step cls_api_attempt --group 01 --evidence <attempt>.json` (`action`, `operation_id`, `observations`); `cls run` needs it.
4. `gof cls run --batch B --operation B/save-operation.json --output B/save-receipt.json`: POST `/api/workflow/script/save` with `status=-2`, read back, a receipt with the real draft URL.
5. `gof batch record --step draft_saved` with evidence: `confirmed`, `unpublished`, `transport=cls_api`, `draft_id`, `url`, the operation and receipt paths, and `requirement_number` if returned.
6. `gof cls observe --action import_scripts --draft ID`, build the import operation (the same CSV, the recorded draft ID), a new `cls_api_attempt`, `gof cls run` (multipart `script_id` / `file` / `user_id`).
7. Check the read-back (count, order, names, creator, the link on the first row and `同上` after it, the fixed tag, `parent.status=-2`), then `gof batch record --step scripts_imported` with `quantity`, `names` and the script IDs.

A business answer needs `code=0` even with HTTP 200; on failure relay the HTTP status, the business code and CLS's message.

## Recovery

Each operation is sent once (`cls_api_attempt` plus a `cls-sent-*.json` marker). An unknown result comes back as `unknown_result: true` with `draft_id` / `operation_id` when known: read back with `cls read-draft --id N` or `cls verify`. If the save timed out with no ID, ask the user for the draft link or ID rather than creating another. A partial import is reported as it is. A refusal because CLS cannot be reached came before anything was sent: run the same command again later; it is not a lost login.
