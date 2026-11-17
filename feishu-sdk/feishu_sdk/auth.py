"""The per-user login: device authorization, the stored token and its renewal."""

from __future__ import annotations

import base64
import math
import time
from typing import Any, Callable, Iterable

from . import transport
from .config import DOC_EDIT_SCOPE, FEISHU, OFFLINE_SCOPE, REQUIRED_SCOPES, AppCredentials, Endpoints, scope_list
from .errors import BridgeError
from .fsutil import read_json, remove_file, with_lock, write_json_atomic
from .jsutil import as_int_if_whole, get, is_number, js_number, js_string, now_ms, num
from .paths import DataPaths

#: The access token is renewed this long before it runs out, so a call never starts with a token about to die.
SKEW_MS = 5 * 60_000
DEVICE_PATH = "/oauth/v1/device_authorization"
TOKEN_PATH = "/oauth/v3/token"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
MAX_WAIT_MS = 50_000
MAX_INTERVAL_S = 60
NO_APP = "No Feishu app configured: FEISHU_APP_ID and FEISHU_APP_SECRET must be set in the environment"
BAD_APP = "The Feishu app credentials are invalid: check FEISHU_APP_ID / FEISHU_APP_SECRET"

Obj = dict[str, Any]


def _describe(body: Any, fallback: str) -> str:
    for key in ("error_description", "error", "msg"):
        value = get(body, key)
        if isinstance(value, str) and value:
            return value
    return fallback


def _same_set(a: list[str], b: list[str]) -> bool:
    return len(a) == len(b) and all(item in b for item in a)


def _default_sleep(ms: float) -> None:
    time.sleep(max(0.0, ms) / 1000)


def _is_v1(value: Any) -> bool:
    return is_number(value) and value == 1


class FeishuAuth:
    """One user's login. `now` and `sleep` (milliseconds) are the tests' clock."""

    def __init__(self, *, paths: DataPaths, app: AppCredentials | None, endpoints: Endpoints | None = None,
                 expected_open_id: str | None = None, missing_app: str | None = None,
                 now: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None) -> None:
        self.paths = paths
        self.app = app
        self.endpoints = endpoints or FEISHU
        #: The Open ID the host says this process serves; a login or a stored token of anybody else is refused.
        self.expected = expected_open_id
        #: What to tell the user when no app is configured (where the host or the standalone user should put it).
        self.missing_app = missing_app or NO_APP
        self.now: Callable[[], float] = now or now_ms
        self.sleep: Callable[[float], None] = sleep or _default_sleep

    def secrets(self) -> list[str]:
        """Every secret this process holds, for scrubbing text before it leaves."""
        token = read_json(self.paths.auth)
        values = [self.app.app_secret if self.app else "", get(token, "access_token"), get(token, "refresh_token")]
        return [value for value in values if isinstance(value, str) and value]

    def safe_secrets(self) -> list[str]:
        try:
            return self.secrets()
        except Exception:  # noqa: BLE001 - a login file that cannot be read has nothing to scrub
            return []

    def credentials(self) -> AppCredentials:
        if not self.app:
            raise BridgeError("config", self.missing_app)
        return self.app

    # ---- stored state ---------------------------------------------------------------------------------------------------

    def _read_token(self) -> Obj | None:
        token = read_json(self.paths.auth)
        if not isinstance(token, dict) or not _is_v1(token.get("v")) or not isinstance(token.get("access_token"), str) or not token.get("open_id"):
            return None
        # A token belongs to the app that issued it and to the user this process serves.
        if self.app and token.get("app_id") != self.app.app_id:
            return None
        if self.expected and token.get("open_id") != self.expected:
            raise BridgeError("identity_mismatch", "The stored Feishu login is not this user's and was refused: log out, then log in with the user's own account")
        return token

    def _read_pending(self) -> Obj | None:
        pending = read_json(self.paths.login)
        if not isinstance(pending, dict) or not _is_v1(pending.get("v")) or not isinstance(pending.get("device_code"), str):
            return None
        if self.app and pending.get("app_id") != self.app.app_id:
            return None
        return pending

    def _logged_in(self, token: Obj) -> bool:
        now = self.now()
        return num(token.get("refresh_expires_at")) > now or num(token.get("access_expires_at")) > now

    # ---- status ---------------------------------------------------------------------------------------------------------

    def status(self, required: Iterable[str] = REQUIRED_SCOPES) -> Obj:
        """Local only: no network, and never a token."""
        required = list(required)
        result: Obj = {"logged_in": False, "app_configured": bool(self.app), "storage": self.paths.mode}
        token = self._read_token()
        if token and self._logged_in(token):
            scopes = scope_list(token.get("scope") if isinstance(token.get("scope"), str) else None)
            result.update({
                "logged_in": True, "open_id": token["open_id"], "scopes": scopes, "missing_scopes": [s for s in required if s not in scopes],
                "access_expires_at": token.get("access_expires_at"), "refresh_expires_at": token.get("refresh_expires_at"),
                "refreshable": bool(token.get("refresh_token")),
            })
            result = {key: value for key, value in result.items() if value is not None}
            if token.get("name"):
                result["name"] = token["name"]
        pending = self._read_pending()
        if pending and num(pending.get("expires_at")) > self.now():
            result["pending_login"] = {
                "user_code": pending.get("user_code"), "verification_uri_complete": pending.get("verification_uri_complete"),
                "expires_at": pending.get("expires_at"),
            }
        return result

    def logout(self) -> None:
        remove_file(self.paths.auth)
        remove_file(self.paths.login)

    def require_scopes(self, required: Iterable[str]) -> None:
        token = self._read_token()
        if not token:
            raise BridgeError("auth_required", "Not logged in to Feishu")
        granted = scope_list(token.get("scope") if isinstance(token.get("scope"), str) else None)
        missing = [item for item in required if item not in granted]
        if missing:
            raise BridgeError("scope_missing", f"The Feishu login lacks permissions: {', '.join(missing)}; enable them in the Feishu app, then log in again")

    # ---- device flow ----------------------------------------------------------------------------------------------------

    def _post_form(self, url: str, form: dict[str, str], headers: dict[str, str] | None = None) -> tuple[int, Any]:
        self.credentials()
        try:
            return 200, transport.request("POST", url, form=form, headers=headers, timeout=20)
        except transport.HttpError as error:
            return error.status, error.body
        except transport.NoResponse:
            raise BridgeError("network", "Cannot reach the Feishu accounts service") from None

    def _prompt(self, pending: Obj, reused: bool) -> Obj:
        return {
            "verification_uri": pending["verification_uri"], "verification_uri_complete": pending["verification_uri_complete"],
            "user_code": pending["user_code"], "expires_in_s": max(0, math.floor((num(pending["expires_at"]) - self.now()) / 1000)),
            "interval_s": pending["interval_s"], "reused": reused,
        }

    def start_login(self, extra_scopes: Iterable[str] = ()) -> Obj:
        """Asks Feishu for a device authorization the user confirms in their own browser. Nothing is stored but the pending request."""
        app = self.credentials()
        scopes = list(dict.fromkeys([*REQUIRED_SCOPES, DOC_EDIT_SCOPE, *extra_scopes, OFFLINE_SCOPE]))
        existing = self._read_pending()
        if existing and num(existing.get("expires_at")) - 10_000 > self.now() and _same_set(scope_list(existing.get("scope") if isinstance(existing.get("scope"), str) else None), scopes):
            return self._prompt(existing, True)

        basic = base64.b64encode(f"{app.app_id}:{app.app_secret}".encode("utf-8")).decode("ascii")
        status, body = self._post_form(f"{self.endpoints.accounts}{DEVICE_PATH}", {"client_id": app.app_id, "scope": " ".join(scopes)},
                                       {"Authorization": f"Basic {basic}"})
        if status >= 400 or get(body, "error") or not isinstance(get(body, "device_code"), str) or not isinstance(get(body, "user_code"), str):
            if get(body, "error") == "invalid_client":
                raise BridgeError("config", BAD_APP)
            raise BridgeError("api", f"Starting the Feishu authorization failed: {_describe(body, f'HTTP {status}')}")
        interval = min(MAX_INTERVAL_S, max(1, js_number(body.get("interval")) or 5))
        interval = as_int_if_whole(interval)
        now = self.now()
        uri = body.get("verification_uri")
        pending: Obj = {
            "v": 1, "app_id": app.app_id, "device_code": body["device_code"], "user_code": body["user_code"],
            "verification_uri": js_string(uri) if uri is not None else "",
            "verification_uri_complete": js_string(body.get("verification_uri_complete") or uri or ""),
            "scope": " ".join(scopes), "expires_at": as_int_if_whole(now + (js_number(body.get("expires_in")) or 240) * 1000),
            "interval_s": interval, "next_poll_at": as_int_if_whole(now + interval * 1000),
        }
        if not pending["verification_uri_complete"]:
            raise BridgeError("api", "Feishu returned no authorization link")
        write_json_atomic(self.paths.login, pending)
        return self._prompt(pending, False)

    def _keep_pending(self, pending: Obj) -> None:
        """Saves how this poll stands, unless the request was replaced meanwhile (a new `login start`): that one is not this poll's to change."""
        current = self._read_pending()
        if current and current.get("device_code") == pending["device_code"]:
            write_json_atomic(self.paths.login, pending)

    def _drop_pending(self, pending: Obj) -> None:
        """This poll's request is over: it goes, unless a new one has taken its place meanwhile."""
        current = self._read_pending()
        if not current or current.get("device_code") == pending["device_code"]:
            remove_file(self.paths.login)

    def poll_login(self, wait_ms: float = 40_000) -> Obj:
        """Waits (at most `wait_ms`, never more than 50 s) for the user to confirm; stores the login when they do."""
        app = self.credentials()
        started = self.now()
        pending = self._read_pending()
        if not pending:
            token = self._read_token()
            if token and self._logged_in(token):
                return self._authorized(token)
            raise BridgeError("auth_required", "No Feishu authorization in progress: start a login first")
        deadline = self.now() + min(max(0.0, wait_ms), MAX_WAIT_MS)

        def expires_in() -> int:
            return max(0, math.floor((num(pending["expires_at"]) - self.now()) / 1000))

        def still_pending() -> Obj:
            return {"state": "pending", "retry_after_s": max(1, math.ceil((num(pending["next_poll_at"]) - self.now()) / 1000)),
                    "expires_in_s": expires_in()}

        interval = js_number(pending.get("interval_s"))
        if not math.isfinite(interval):
            interval = 5
        while True:
            if self.now() >= num(pending.get("expires_at")) or math.isnan(num(pending.get("expires_at"))):
                self._drop_pending(pending)
                return {"state": "expired"}
            wait = num(pending.get("next_poll_at")) - self.now()
            if wait > 0:
                if self.now() + wait > deadline:
                    return still_pending()
                self.sleep(wait)
                continue
            try:
                reply: tuple[int, Any] | None = self._post_form(
                    f"{self.endpoints.accounts}{TOKEN_PATH}",
                    {"grant_type": DEVICE_GRANT, "device_code": pending["device_code"], "client_id": app.app_id, "client_secret": app.app_secret})
            except BridgeError:
                reply = None
            # No answer, or one that is not an answer to the question (a gateway error, a rate limit, a page): the user may well have
            # confirmed already, so the request is kept and asked again a little later, within the allowed wait.
            if reply is None or reply[0] >= 500 or reply[0] == 429 or (reply[0] >= 400 and not isinstance(get(reply[1], "error"), str)):
                interval = min(MAX_INTERVAL_S, interval + 1)
                pending["interval_s"] = as_int_if_whole(interval)
                pending["next_poll_at"] = as_int_if_whole(self.now() + interval * 1000)
                self._keep_pending(pending)
                if self.now() >= deadline:
                    return still_pending()
                continue
            status, body = reply
            access = get(body, "access_token")
            if not get(body, "error") and status < 400 and isinstance(access, str) and access:
                return self._finish_login(body, pending)
            error = get(body, "error") if isinstance(get(body, "error"), str) else ""
            if error in ("authorization_pending", "slow_down"):
                if error == "slow_down":
                    interval = min(MAX_INTERVAL_S, interval + 5)
                    pending["interval_s"] = as_int_if_whole(interval)
                pending["next_poll_at"] = as_int_if_whole(self.now() + interval * 1000)
                self._keep_pending(pending)
                if self.now() + interval * 1000 > deadline:
                    return still_pending()
                continue
            self._drop_pending(pending)
            if error == "access_denied":
                return {"state": "denied"}
            if error in ("expired_token", "invalid_grant"):
                # The code is good for one exchange: another poll of this same request may have just collected the login.
                try:
                    token = self._read_token()
                except BridgeError:
                    token = None
                if token and self._logged_in(token) and num(token.get("granted_at")) >= started:
                    return self._authorized(token)
                return {"state": "expired"}
            if error == "invalid_client":
                raise BridgeError("config", BAD_APP)
            raise BridgeError("api", f"The Feishu authorization failed: {_describe(body, f'HTTP {status}')}")

    def _authorized(self, token: Obj) -> Obj:
        result: Obj = {"state": "authorized", "open_id": token["open_id"],
                       "scopes": scope_list(token.get("scope") if isinstance(token.get("scope"), str) else None),
                       "refreshable": bool(token.get("refresh_token"))}
        if token.get("name"):
            result["name"] = token["name"]
        return result

    def _finish_login(self, body: Obj, pending: Obj) -> Obj:
        app = self.credentials()
        access = js_string(body["access_token"])
        refresh = body.get("refresh_token") if isinstance(body.get("refresh_token"), str) and body.get("refresh_token") else None
        access_life = (js_number(body.get("expires_in")) or 7200) * 1000
        refresh_life = (js_number(body.get("refresh_token_expires_in")) or 604_800) * 1000 if refresh else access_life
        # The tokens exist only in this process until they are written; a hiccup of the network must not cost the user their confirmation.
        attempt = 0
        while True:
            try:
                info = transport.request("GET", f"{self.endpoints.open}/open-apis/authen/v1/user_info", headers={"Authorization": f"Bearer {access}"})
                break
            except (transport.HttpError, transport.NoResponse):
                if attempt >= 3:
                    self._drop_pending(pending)
                    raise BridgeError("api", "The authorization completed but the account cannot be read: start the login again") from None
                self.sleep(500 * (attempt + 1))
                attempt += 1
        user = get(info, "data")
        if user is None:
            user = info
        open_id = get(user, "open_id") if isinstance(get(user, "open_id"), str) else ""
        if not open_id:
            self._drop_pending(pending)
            raise BridgeError("api", "Cannot confirm the authorizing account: start the login again")
        if self.expected and open_id != self.expected:
            # The token is dropped unread: somebody else's account must not end up in this user's directory.
            self._drop_pending(pending)
            raise BridgeError("identity_mismatch", "The Feishu account that authorized is not this user, so the login was refused: the user must log in with their own account")
        now = self.now()
        token: Obj = {
            "v": 1, "app_id": app.app_id, "open_id": open_id, "access_token": access,
            "access_expires_at": as_int_if_whole(now + access_life), "refresh_expires_at": as_int_if_whole(now + refresh_life),
            "scope": body["scope"] if isinstance(body.get("scope"), str) else "", "granted_at": as_int_if_whole(now),
            "refreshed_at": as_int_if_whole(now),
        }
        name = get(user, "name")
        if isinstance(name, str) and name:
            token["name"] = name
        if refresh:
            token["refresh_token"] = refresh
        with_lock(self.paths.lock, lambda: write_json_atomic(self.paths.auth, token))
        self._drop_pending(pending)
        return self._authorized(token)

    # ---- using the login ------------------------------------------------------------------------------------------------

    def access_token(self) -> str:
        """A user access token that is good for a call, renewed first when it is about to expire."""
        token = self._read_token()
        if not token:
            raise BridgeError("auth_required", "Not logged in to Feishu: log in first")
        if num(token.get("access_expires_at")) - SKEW_MS > self.now():
            return token["access_token"]
        return self.refresh(False)

    def refresh(self, force: bool, rejected: str | None = None) -> str:
        """Renews the token.

        The refresh token is single use, so this runs under a lock and checks again once it holds it. `force` renews a token Feishu turned
        down, `rejected`, however long it was meant to last, unless another call has replaced it by now.
        """
        app = self.credentials()
        return with_lock(self.paths.lock, lambda: self._refresh_locked(app, force, rejected))

    def _refresh_locked(self, app: AppCredentials, force: bool, rejected: str | None) -> str:
        current = self._read_token()
        if not current:
            raise BridgeError("auth_required", "Not logged in to Feishu: log in first")
        now = self.now()
        fresh = num(current.get("access_expires_at")) - SKEW_MS > now
        # Somebody else renewed it while this process waited for the lock.
        if not force and fresh:
            return current["access_token"]
        if force and rejected is not None and current["access_token"] != rejected and fresh:
            return current["access_token"]
        refresh_token = current.get("refresh_token")
        if not refresh_token or num(current.get("refresh_expires_at")) <= now or math.isnan(num(current.get("refresh_expires_at"))):
            # A login that cannot be renewed is good until its access token really ends, not until it is nearly there.
            if not force and not refresh_token and num(current.get("access_expires_at")) > now:
                return current["access_token"]
            remove_file(self.paths.auth)
            raise BridgeError("auth_required", "The Feishu login has expired: log in again")
        try:
            reply = transport.refresh_user_token(self.endpoints.accounts, app.app_id, app.app_secret, js_string(refresh_token))
        except transport.AccessTokenError as error:
            if error.status > 0:
                dead = error.error == "invalid_grant" or error.code in (20037, 20064, 20073)
                if dead:
                    remove_file(self.paths.auth)
                    raise BridgeError("auth_required", "The Feishu login is no longer valid (its refresh token expired, was revoked or used): log in again", error.code or None) from None
                if error.error == "invalid_client":
                    raise BridgeError("config", BAD_APP) from None
                raise BridgeError("api", f"Renewing the Feishu login failed: {error}", error.code or None) from None
            # No answer. The login stays: if the server did rotate the token, the next try says so with invalid_grant and clears it.
            raise BridgeError("network", "Cannot reach Feishu to renew the login: try again later") from None
        now = self.now()
        nxt: Obj = dict(current)
        nxt.update({
            "access_token": js_string(reply["access_token"]),
            "access_expires_at": as_int_if_whole(now + (js_number(reply.get("expires_in")) or 7200) * 1000),
            "refreshed_at": as_int_if_whole(now),
            "scope": reply["scope"] if reply.get("scope") is not None else current.get("scope"),
        })
        if reply.get("refresh_token"):
            nxt["refresh_token"] = reply["refresh_token"]
            nxt["refresh_expires_at"] = as_int_if_whole(now + (js_number(reply.get("refresh_token_expires_in")) or 604_800) * 1000)
        # The old refresh token is spent as of the server's answer, so the new one must reach the disk: a failed write is tried again.
        attempt = 0
        while True:
            try:
                write_json_atomic(self.paths.auth, nxt)
                break
            except OSError as error:
                if attempt >= 4:
                    raise BridgeError("api", f"The Feishu login was renewed but could not be saved ({error}); the old refresh token is void: log in again") from None
                self.sleep(200 * (attempt + 1))
                attempt += 1
        return nxt["access_token"]
