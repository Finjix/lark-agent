# 动态审片规范 (dynamic review spec)

`audit` = `python "<skill-dir>/scripts/audit.py"` (see SKILL.md). It prints progress lines while it works and ends with one line of JSON. Exit code 2 means refused (`{"status":"blocked","reason":…}`): tell the user `reason`.

How it works: once the bot listens to a group, it reads the group's chat (the last year at once, then every three days what is new) and turns the review rulings in it into rules of the project's dynamic spec. Rulings are platform rejections and their reasons (screenshots included), reviewers' 「X 不能用」「Y 可以用了」, and corrections; talk about how one material is made or how it performs (镜头、节奏、模型、3 秒数据) sets no rule. Every review of that project applies the dynamic spec together with the fixed spec; where they disagree, the dynamic one wins, being newer. A rule changes where it came from: a new ruling in the group updates or withdraws it at the next daily reading. The daily reading runs in the background and posts nothing in the group: the bot speaks there only when someone @-mentions it.

The host tells the command line which chat the message came from (`LARK_AGENT_CHAT_ID` / `LARK_AGENT_CHAT_TYPE`); there is no chat argument.

## Start listening: 「监听本群更新审片规范」

1. Only in a group chat. In a private chat, tell the user to add the bot to the project's review group and @ it there with 「监听本群更新审片规范」.
2. A group's rules are for one project. Go on only when the user has said which (GOF / 无尽冬日, or GOT); otherwise ask 「这个群是 GOF 还是 GOT 的审核群？」 and stop.
3. `audit rules watch --project GOF|GOT [--since YYYY-MM-DD]` reads the last year of the group's chat. Pass `--since` when the user says where to start (「从 9 月 1 号开始」, 「最近一个月」: work out the date). A busy group can take several minutes; say nothing while it runs and wait until it ends.
4. Reply once, in a few sentences, with no file and no link: do not run `rules show` here (the user asks for the spec when they want to see it).
   - the bot listens to this group for <P> from now on and reads what is new in it every three days, in the background, without posting in the group;
   - how many messages it read (`read`) and how many rules the dynamic spec has (`rules`, of which `added` are new);
   - `added` 0 although messages were read: there were no review rulings in them (no rejections, no 「X 不能用」); rulings posted in the group from now on are picked up at the next reading (every three days);
   - reviews of <P> use it from now on.
   - `already: true`: the group was already being listened to; say so, with the same facts.
   - `pending: true`: the history was too long to finish now (a reading stops after about 30 minutes); the rest is read at the next readings (every three days).
   - `error`: the group is registered but reading it failed: tell the user `error` (a missing permission is for an administrator to fix). It is tried again at the next reading (every three days).

## Other requests

- Stop listening (「停止监听本群」「取消监听」「别再读本群了」): `audit rules unwatch`, in that group; then say the bot no longer reads it. The rules already collected stay in the dynamic spec (a wrong one can be deleted, below). In a private chat, tell the user to say it in the group to stop.
- Update now (「现在更新一下规范」): `audit rules sync --here` in a group being listened to (`audit rules sync` without `--here` updates every group). For each group in `groups`: what changed (`added` / `updated` / `removed`), or that nothing did; its `error` if it has one (that group was not read: say why); `pending: true` means it is not read to the end yet and the rest follows at the next daily reading. `status: failed` means no group could be read: give the errors. No file: the spec goes to the user only when they ask to see it.
- Show the dynamic spec, only when the user asks to see or get it (「看看 GOF 的动态规范」「把规范发我」): `audit rules show --project <P> --output "动态审片规范-<P>.md"` (written into the working directory, which sends it to the user), mention the `file` it returns, and say how many rules it has and when it was last updated. When the user has not named the project and this group is not listened to for one (`audit rules status` → `here`), ask GOF or GOT. To read the spec yourself (to answer a question about one rule, say), leave out `--output`: the Markdown comes back in `markdown` and no file is written.
- Which groups are listened to: `audit rules status`. `here` is this chat's project (null: not listened to), and the group entry with `here: true` is this chat. Give each group's project, since when, when it was last read, any `error`, and any `skipped` stretches of messages (they could not be turned into rules: give their time range and reason). Call the current chat 「本群」 and the others 「另一个群」; never show chat ids.
- Delete a wrong rule (「删掉 D3」): `audit rules remove --project <P> --id D3 [--id …]`, only for rules the user explicitly asked to delete. If they describe a rule without its id, show the spec first and confirm which one. Add that if the group's chat still says otherwise, a later reading may add it again: the lasting fix is a clear ruling in the group.
- Reviewing material is [gof-audit.md](gof-audit.md)'s and [got-audit.md](got-audit.md)'s.
