# Periodic sync and housekeeping (for the host or the administrator)

Neither is run on a user's behalf: in a group, 「现在更新一下规范」 is `audit rules sync --here` (see [dynamic-rules.md](dynamic-rules.md)).

## The periodic sync: `rules sync`

While a group is listened to, the skill keeps `jobs.json` in its shared folder (`<AGENT_SHARED_DIR>/audit-auto/jobs.json`): written whenever the groups are saved (a watch, a sync), removed when the last group stops being listened to. It declares one job, every 4320 minutes (three days), at most 30 minutes, run in the skill's folder:

```bash
python scripts/audit.py rules sync
```

A host that runs `jobs.json` files does the periodic sync with `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `AUDIT_AUTO_GEMINI_KEY` and `AGENT_SHARED_DIR` set, and no user or chat. In any other harness, run `audit rules sync` when asked, or schedule it yourself (cron, Task Scheduler) with the same environment.

- It reads what is new in every group listened to (and in the threads it follows), the group read longest ago first, for at most 30 minutes: no model call is started after that (one can take up to 10). A host that ends it at 30 minutes (`timeoutMinutes`) cuts off a call still going: what was read before it is kept, and that part is read again at the next run.
- Nothing new (only the bot's own replies, stickers and the like): the model is not called.
- Another process holds `<AGENT_SHARED_DIR>/audit-auto/lock` (a user's `rules watch`, say): it prints `{"status":"busy",…}` and exits 0; the next run reads on from where the groups stand.
- Every group failing: `status: failed`, exit 2. Some failing: their `error`, exit 0.
- Nothing is sent to anyone, and nothing is posted in any group.

## Housekeeping: `prune`

SKILL.md declares `metadata.prune: "scripts/audit.py prune"`. A host that supports the hook runs it periodically in the skill's folder, once for each user directory where `<AGENT_USER_DIR>/audit-auto/` exists, with that user's `AGENT_USER_DIR` set and no chat. Elsewhere, schedule it yourself; without `AGENT_USER_DIR` it prunes the default user folder.

- It deletes the files in `<user>/audit-auto/tmp/` and `<user>/audit-auto/reviews/` that were not created or modified for `AUDIT_AUTO_RETENTION_DAYS` days (default 14), and the folders that leaves empty. Links and junctions are never followed (a stale link is deleted itself); what cannot be deleted is skipped. It also sweeps stale `run-*` folders that older versions left in `<system temp>/audit-auto/`.
- It never touches the shared folder (groups, dynamic specs).
- Output: one line, `{"status":"ok","files":<deleted>,"bytes":<freed>}`, exit 0; `{"status":"blocked","reason":…}`, exit 2, when it refuses (a retention that is not a whole number ≥ 1, an `AGENT_USER_DIR` that is not absolute).
