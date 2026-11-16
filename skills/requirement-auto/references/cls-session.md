# CLS login and session

CLS sits behind company SSO (Feishu web QR login), so the user logs in by scanning a QR code with their phone. `gof cls login --qr` opens CLS in the installed Chrome or Edge, headless (override with env `FEISHU_BROWSER` = absolute path, the variable the feishu skill reads too, so both start the same browser), screenshots the Feishu QR page, then takes the `Authorization` header of the page's own CLS API calls and confirms it with `/api/userinfo`. Later work is plain HTTP.

The browser is the feishu skill's (it holds the user's Feishu web session): Feishu is logged in first in it (`gof feishu authorize --qr`, [gof-requirement.md](gof-requirement.md) section 0), and that scan normally covers CLS too.

```text
gof cls login --qr cls-login.png    # QR image into the working directory
gof cls session                     # check; after the scan, take the login and close the browser
```

- Online `cls` commands (`catalog`, `observe`, `run`, `read-draft`, `verify`) get the login themselves; without one they say to run `cls login --qr`.
- CLS out of reach (an outage, a gateway, the network or a proxy) is not a lost login: `gof login check` gives `unknown` with the reason, and commands refuse with `CLS 暂时无法访问（…）`, having sent nothing. Make no QR code and ask for no scan; run it again a little later.
- `cls login --qr`: silent check first (up to 12 s); logged in -> `logged_in`. Otherwise it stops on the Feishu QR page, writes the PNG and returns `qr_ready`; the browser waits in the background. `no_qr`: the page is not on a QR code; run `cls session` first. With CLS out of reach it makes no code.
- The image in the working directory is sent to the user with the reply. It expires in a few minutes; run the command again for a fresh one. After 15 minutes without a scan the waiting browser is closed.
- After the user confirms: `cls session` (~25 s) attaches to the waiting browser, presses 【授权】 on the `dsc_admin` consent page (only that app and, once bound, that account), takes the login and closes the browser so cookies persist.
- Whoever scans is who logs in. The first account is bound in `<user dir>/requirement-auto/cls/account.json`; another account is refused, naming the one to use. The same account under a new name stays bound (the file takes the new name), and the consent page may show the name without the suffix CLS adds (`钟丰骏` for `钟丰骏-实习生`). To change the account, clear that file and the browser profile (`<user dir>/feishu/browser-profile`, which also signs Feishu web out), then log in again.

## Keeping the login

- It lives in the browser profile (`<user dir>/feishu/browser-profile`, the feishu skill's), so later tasks normally pass without a scan. This relies on `--restore-last-session` (SSO cookies are session-only); keep that flag.
- One browser per profile: run one user's `cls` commands one after another. Different users' browsers do not interfere.
- With `LARK_AGENT_USER_OPEN_ID` but no `LARK_AGENT_USER_DIR`, commands refuse rather than share a dir.

## Environment

- Needs Chrome or Edge on the machine; nobody needs to be at it.
- Behind a corporate proxy set `REQUIREMENT_AUTO_USE_ENV_PROXY=1` and `HTTPS_PROXY`, with `NO_PROXY` for hosts that must not go through it; without it the proxy variables are ignored. The hidden browser is driven over a direct connection to `127.0.0.1`, which never goes through a proxy.
