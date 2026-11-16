# The daily reading (rules sync)

lark-agent runs this once a day (every 1440 minutes: soon after the service starts, then every 24 hours; not at a fixed hour), from the project root, with the service's environment plus `FEISHU_APP_ID`, `FEISHU_APP_SECRET` and `LARK_AGENT_SHARED_DIR`, and no user or chat:

```bash
python skills/audit-auto/scripts/audit.py rules sync
```

- It reads what is new in every group listened to (and in the threads it follows), longest-unread group first, for at most 15 minutes (no new model call is started after that; one call can take up to 10). The host should end it after 30 minutes.
- Nothing new (only the bot's own replies, stickers and the like): the model is not called.
- Another process holds `shared/audit-auto/lock` (a user's `rules watch` in progress, say): it prints `{"status":"busy",…}` and exits 0; the next run reads on from where the groups stand.
- Every group failing to be read: `status: failed`, exit 2 (the host logs the job as failed). Some failing: their `error`, exit 0.
- The output goes to the host's log only: nothing is sent to anyone, and nothing is posted in any group.

This is not something to run on a user's behalf: in a group, 「现在更新一下规范」 is `audit rules sync --here` (see [dynamic-rules.md](dynamic-rules.md)).
