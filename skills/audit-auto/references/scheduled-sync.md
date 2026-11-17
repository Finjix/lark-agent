# The reading every three days (rules sync)

While a group is listened to, the skill keeps `jobs.json` in its shared folder (`<LARK_AGENT_SHARED_DIR>/audit-auto/jobs.json`): it is written whenever the groups are saved (a watch, a sync) and removed when the last group stops being listened to. Nothing to do by hand. lark-agent reads it and runs the job every three days (every 4320 minutes: a little after the job appears or the service starts, then every 72 hours; not at a fixed hour), in this skill's folder, with the service's environment plus `FEISHU_APP_ID`, `FEISHU_APP_SECRET` and `LARK_AGENT_SHARED_DIR`, and no user or chat:

```bash
python scripts/audit.py rules sync
```

- It reads what is new in every group listened to (and in the threads it follows), longest-unread group first, for at most 30 minutes (no new model call is started after that; one call can take up to 10). The host ends it after 30 minutes (`timeoutMinutes` in the job): a model call still going then is cut off, what was read before it is kept, and the part it was reading is read again at the next run.
- Nothing new (only the bot's own replies, stickers and the like): the model is not called.
- Another process holds `data/shared/audit-auto/lock` (a user's `rules watch` in progress, say): it prints `{"status":"busy",…}` and exits 0; the next run reads on from where the groups stand.
- Every group failing to be read: `status: failed`, exit 2 (the host logs the job as failed). Some failing: their `error`, exit 0.
- The output goes to the host's log only: nothing is sent to anyone, and nothing is posted in any group.

This is not something to run on a user's behalf: in a group, 「现在更新一下规范」 is `audit rules sync --here` (see [dynamic-rules.md](dynamic-rules.md)).
