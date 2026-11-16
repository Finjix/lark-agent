# 飞书登录 (Feishu login)

`feishu` = `python "<skill>/scripts/feishu.py"` (see SKILL.md). Each command prints one JSON line.

## Check

`feishu login check` asks Feishu with the stored login (renewing it when Feishu turns the token down). `status`:

- `ok`: logged in (`name` is who).
- `login_required` / `scopes_missing`: log in (below), then check again. Still `scopes_missing` after a fresh login: the app itself lacks the
  permission (`missing_scopes`): tell the user the administrator has to enable it for user identity and publish a new app version, and stop.
- `identity_mismatch`: the stored login is somebody else's: `feishu login logout --browser` (the hidden browser may be signed in as that
  person too), then log in with the user's own account.
- `unreachable`: Feishu is temporarily out of reach; check again shortly. The login is not lost.
- `blocked`: relay `reason` (an app configuration problem is for the administrator).

## Log in

The QR goes to this user only. When `LARK_AGENT_CHAT_TYPE` is `group`, do not post it in the group: tell the user to message the bot
privately and log in there.

1. `feishu login start` returns a device authorization (link and code). Keep the link to yourself: the QR below is what the user gets.
   (Another skill may ask for an extra permission: `feishu login start --extra-scope sheets:spreadsheet:create`.)
2. `feishu authorize --qr feishu-login.png` (in the working directory, so the picture goes to the user with your reply):
   - `qr_ready`: send the image and tell the user to scan it with the Feishu app and confirm on the phone; it expires in a few minutes. This
     one scan also signs the user's hidden browser in to Feishu web, which other skills use (requirement-auto's CLS), so they do not ask
     for another scan.
   - `approved`: the browser was signed in already; no scan needed, go to step 4.
   - No browser on the computer (the reason says Chrome or Edge is missing): send the `verification_uri_complete` link and `user_code` from
     step 1 instead, to open and confirm in Feishu.
   - `no_pending`: the authorization of step 1 has run out: start again at step 1.
   - `stuck`: run `feishu login poll`; when it is not `authorized`, run step 2 again.
   - `blocked`: relay `reason`.
3. When the user says they have scanned: `feishu authorize` (no `--qr`): `approved` = done; `login_required` = the QR expired or nobody
   scanned: run step 2 again for a fresh one; `no_pending` / `stuck`: as in step 2.
4. `feishu login poll` collects the login (`state: authorized`; `pending` = run it again; `denied` / `expired` = start over at step 1),
   then `feishu login check`. `blocked` saying the account is not the user's own: somebody else scanned the code; the hidden browser has
   been signed out of their account, so start over at step 1 and send the new code to the user alone.

`login poll` waits up to 40 s by default (`--wait <0..50>`).

## Other commands

- `feishu login status`: the local record only (no network): logged in, account, permissions, a pending authorization.
- `feishu login logout`: forget the login (the browser's Feishu web session stays; with `--browser` it is signed out too, and the next
  login asks for a scan).
- `feishu doctor`: app configured, logged in, every permission granted.
