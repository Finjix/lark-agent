# The dynamic review spec (动态审片规范)

`audit` and its output are as in SKILL.md.

How it works: while the bot listens to a review group, it reads the group's chat (the last year at once, then what is new at every sync) and turns the review rulings in it into rules of the project's dynamic spec. Rulings are platform rejections and their reasons (screenshots included), reviewers' 「X 不能用」 (X is not allowed) / 「Y 可以用了」 (Y is allowed now), and corrections; talk about how one material is made or performs sets no rule. Every review of the project applies the dynamic spec together with the fixed spec; where they disagree, the dynamic one wins. A new ruling in the group updates or withdraws a rule at the next sync. Reading posts nothing in the group.

**Which chat.** The commands below act on the chat the host reports (`AGENT_CHAT_ID`, with `AGENT_CHAT_TYPE`). When there is none (your harness is not a chat), pass `--chat <chat_id>`: the review group's Feishu chat id (`oc_…`), which the user or the administrator gives; the bot must be a member of the group. In the replies, call that chat "this group" (本群) and any other "another group"; never show chat ids.

## Start listening: 「监听本群更新审片规范」

1. Only for a group. In a private chat (the host says `p2p`), tell the user to add the bot to the project's review group and say 「监听本群更新审片规范」 there, unless they give the group's chat id for `--chat`.
2. A group's rules are for one project. Go on only when the user has said which (GOF / 无尽冬日, or GOT); otherwise ask 「这个群是 GOF 还是 GOT 的审核群？」 (does this group review GOF or GOT?) and stop.
3. Run `audit rules watch --project <P> [--chat <chat_id>] [--since YYYY-MM-DD]`. It reads the last year of the chat; pass `--since` when the user says where to start (「从 9 月 1 号开始」, 「最近一个月」: work out the date). A busy group can take several minutes; say nothing while it runs.
4. Reply once, in a few sentences, with no file and no link (do not run `rules show` here):
   - the bot listens to this group for `<P>` from now on and reads what is new in it at every sync, without posting in the group;
   - how many messages it read (`read`) and how many rules the dynamic spec has (`rules`, of which `added` are new);
   - `added` 0 although messages were read: there were no review rulings in them; rulings posted from now on are picked up at the next sync;
   - reviews of `<P>` use the dynamic spec from now on;
   - `already: true`: the group was already listened to; say so, with the same facts;
   - `pending: true`: the history was too long to finish now (a reading stops after about 30 minutes); the rest is read at the next syncs;
   - `error`: the group is registered but reading it failed: tell the user `error` (a missing permission is for an administrator to fix); it is tried again at the next sync.

How often a sync runs depends on the host (see [scheduled-sync.md](scheduled-sync.md)); without a host that runs it, say that the spec is updated when someone asks 「现在更新一下规范」.

## Other requests

- **Stop listening** (「停止监听本群」, 「取消监听」, 「别再读本群了」): `audit rules unwatch [--chat <chat_id>]`, then say the bot no longer reads the group. The rules already collected stay (a wrong one can be deleted, below). In a private chat with no `--chat`, tell the user to say it in the group.
- **Update now** (「现在更新一下规范」): `audit rules sync --here` in a group listened to, or `audit rules sync --chat <chat_id>`; `audit rules sync` alone updates every group. For each group in `groups`: what changed (`added` / `updated` / `removed`) or that nothing did; its `error` if it has one (that group was not read: say why); `pending: true`: not read to the end yet, the rest follows at the next sync. `status: failed`: no group could be read: give the errors. `status: busy`: another reading is in progress: try again in a few minutes. No file.
- **Show the dynamic spec**, only when the user asks to see or get it (「看看 GOF 的动态规范」, 「把规范发我」): `audit rules show --project <P> --output "动态审片规范-<P>.md"` writes it into the working directory; give the user the `file` it returns (attach it if your harness does not send new files by itself), and say how many rules it has and when it was last updated. When the user has not named the project and this group is not listened to for one (`audit rules status` → `here`), ask GOF or GOT. To read the spec yourself, leave out `--output`: the Markdown comes back in `markdown` and no file is written.
- **Which groups are listened to**: `audit rules status [--chat <chat_id>]`. `here` is this chat's project (null: not listened to), and the group with `here: true` is this chat. Give each group's project, since when, when it was last read, any `error`, and any `skipped` stretches of messages (they could not be turned into rules: give their time range and reason).
- **Delete a wrong rule** (「删掉 D3」): `audit rules remove --project <P> --id D3 [--id …]`, only for rules the user explicitly asked to delete. If they describe a rule without its id, show the spec first and confirm which one. Add that if the group's chat still says otherwise, a later sync may add it again: the lasting fix is a clear ruling in the group.
