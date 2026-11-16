"""The CLS client of the material upload, kept apart from the requirement client (creative-adapter.ts).

cls_adapter.py can never upload. This one speaks the same internal HTTP API the page 我的创意 > 创意列表 > 上传创意 uses, with the
logged-in session's Authorization, and can do exactly what that page does: read the options, ask for upload links, put the files into the
storage those links name, create the creatives from them and save their annotation (创意人, 制作人, 素材类型). Deleting, publishing,
confirming, editing tags or anything else has no method and no allowed path. Creating is never retried: what a create did is found out by
reading back.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from . import js
from .cls_adapter import ClsError, json_or_none
from .web import FetchError, Fetcher, fetch as default_fetch

#: The page's 上传创意 calls, in the order it makes them (see the CLS frontend: views/creativity/materials-upload, components/MaterialUpload).
READ_PATHS = ["/api/creative/games", "/api/userinfo", "/api/permissions", "/api/game/adsdesk/dirs", "/api/game/recommend-filters",
              "/api/material/filters", "/api/creatives"]
SIGN = "/api/qcloud/signed-url"
CREATE = "/api/creative/multiple-upload"
ANNOTATE = "/api/creative/multiple-edit"
LOG = "/api/report/log"
WRITE_PATHS = [SIGN, CREATE, ANNOTATE, LOG]

#: Where an upload link may point: Tencent COS over https, as the links CLS hands out do.
COS_HOST = re.compile(r"[a-z0-9-]+\.cos\.[a-z0-9-]+\.myqcloud\.com")

#: Creatives per page of a name search, as the list page asks for them.
SEARCH_PAGE = 50

#: The Content-Type the page sends with each file. The page puts the file with axios 0.19, whose default for PUT is form-urlencoded and
#: which keeps it for a File body; the storage link only signs the host, so this is about matching what CLS gets from the page.
PAGE_PUT_TYPE = "application/x-www-form-urlencoded"


def _assert(condition: Any, message: str) -> None:
    if not condition:
        raise ClsError(message)


def signature_end(url: str) -> Optional[int]:
    """When the storage signature of a link runs out (seconds since 1970), from its q-sign-time / q-key-time; None when it does not say."""
    try:
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        sign = query.get("sign", [None])[0]
        params = parse_qs(sign, keep_blank_values=True) if sign else query
        time = (params.get("q-sign-time") or params.get("q-key-time") or [None])[0]
        parts = time.split(";") if isinstance(time, str) else []
        end = js.number(parts[1]) if len(parts) > 1 else float("nan")
        return int(end) if js.is_safe_int(end) and end > 0 else None
    except Exception:
        return None


class CreativeAdapter:
    def __init__(self, origin: str, authorization: str, timeout_ms: Optional[int] = None, create_timeout_ms: Optional[int] = None,
                 upload_timeout_ms: Optional[int] = None, fetch: Optional[Fetcher] = None, upload_host: Optional[re.Pattern] = None,
                 allow_http_upload: bool = False, before_write: Optional[Callable[[str], None]] = None) -> None:
        if not origin or not authorization:
            raise ClsError("必须使用 CLS 当前登录会话")
        self._origin = origin
        self._authorization = authorization
        self._timeout_ms = timeout_ms if timeout_ms is not None else 30_000
        #: Creating many creatives takes the server a while: the page waits up to 10 minutes.
        self._create_timeout_ms = create_timeout_ms if create_timeout_ms is not None else 10 * 60_000
        #: A file goes up in one request; big videos need long.
        self._upload_timeout_ms = upload_timeout_ms if upload_timeout_ms is not None else 60 * 60_000
        self._fetch = fetch or default_fetch
        self._upload_host = upload_host or COS_HOST
        self._allow_http_upload = allow_http_upload
        self._before_write = before_write
        self._calls: list[dict] = []
        #: The upload links CLS gave this process: only these are ever put to.
        self._signed: dict[str, str] = {}

    def _request(self, method: str, path: str, query: Optional[list[tuple[str, str]]] = None, body: Any = None, timeout_ms: Optional[int] = None) -> Any:
        allowed = (method == "GET" and (path in READ_PATHS or re.fullmatch(r"/api/creative/[1-9][0-9]*", path))) or (method == "POST" and path in WRITE_PATHS)
        _assert(allowed, "删除、发布、确认和未知路径均被禁止")
        write = method == "POST" and path in (CREATE, ANNOTATE)
        start = js.now_ms()
        headers = {"Authorization": self._authorization}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = js.stringify(body)
        url = f"{self._origin}{path}{'?' + urlencode(query) if query else ''}"
        # Before the request and outside the try: a refusal here means nothing was sent.
        if write and self._before_write:
            self._before_write(path)
        try:
            response = self._fetch(url, method=method, headers=headers, body=payload,
                                   timeout=(timeout_ms if timeout_ms is not None else self._timeout_ms) / 1000, redirect="error")
        except FetchError as error:
            reason = "请求超时" if error.timeout else "连接失败"
            raise ClsError(f"写入结果未知（{reason}）；先回读，不重试" if write else f"CLS 请求失败（{reason}）", "unknown" if write else "blocked") from None
        data = json_or_none(response)
        code = data.get("code", js.UNDEFINED) if isinstance(data, dict) else js.UNDEFINED
        self._calls.append({"method": method, "path": path, "http_status": response.status, "business_code": code, "elapsed_ms": js.now_ms() - start})
        server_message = next((x for x in ((data.get("message"), data.get("msg")) if isinstance(data, dict) else ()) if isinstance(x, str) and js.trim(x)), None)
        detail = f"HTTP {response.status}{f'，业务码 {js.to_str(code)}' if code is not js.UNDEFINED else ''}{'，' + js.u16slice(js.trim(server_message), 0, 300) if server_message else ''}"
        if write and (response.status >= 500 or response.status in (408, 429)):
            raise ClsError(f"写入结果未知（服务端或网关错误，{detail}）；先回读，不重试", "unknown")
        if write and 200 <= response.status < 300 and not js.is_num(code):
            raise ClsError(f"写入结果未知（{detail}，响应没有业务码）；先回读，不重试", "unknown")
        _assert(200 <= response.status < 300 and js.is_num(code) and code == 0, f"CLS 请求失败（{detail}）")
        return data.get("data")

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        query: list[tuple[str, str]] = []
        # Lists go the way the page's axios sends them: key[]=a&key[]=b.
        for key, value in (params or {}).items():
            if isinstance(value, list):
                query.extend((f"{key}[]", js.to_str(item)) for item in value)
            else:
                query.append((key, js.to_str(value)))
        return self._request("GET", path, query)

    def games(self) -> Any:
        return self._get("/api/creative/games")

    def account(self) -> Any:
        return self._get("/api/userinfo")

    def permissions(self) -> Any:
        return self._get("/api/permissions")

    def directories(self, game_id: int) -> Any:
        return self._get("/api/game/adsdesk/dirs", {"id": game_id})

    def people(self, game_id: int) -> Any:
        return self._get("/api/game/recommend-filters", {"id": game_id})

    def types(self, game_id: int) -> Any:
        return self._get("/api/material/filters", {"confirmed": 1, "fields": "types", "game_id": game_id})

    def search(self, game_id: int, text: str, confirmed: bool = True, page: int = 1) -> Any:
        """One page of the creatives of the game whose name or ID contains the text (confirmed ones, or the 待确认 ones)."""
        params: dict[str, Any] = {"page": page, "page_size": SEARCH_PAGE, "game_id": game_id, "keywords": text, "keyword_type": "creative"}
        if not confirmed:
            params["confirmed"] = 0
        return self._get("/api/creatives", params)

    def mine(self, game_id: int, user_id: int, page_size: int = 50) -> Any:
        """The newest creatives this user uploaded, as the list shows them."""
        return self._get("/api/creatives", {"page": 1, "page_size": page_size, "game_id": game_id, "upload_users": [user_id],
                                            "sort_field": "updated_at", "sort_order": "desc"})

    def creative(self, ident: int) -> Any:
        _assert(js.is_safe_int(ident) and ident > 0, "创意 ID 无效")
        return self._request("GET", f"/api/creative/{int(ident)}")

    def sign(self, game_id: int, keys: list[str]) -> dict:
        _assert(len(keys) > 0 and len(set(keys)) == len(keys), "上传文件名为空或重复")
        data = self._request("POST", SIGN, None, {"files": keys, "game_id": game_id})
        urls = data.get("urls") if isinstance(data, dict) else None
        mapping = data.get("mapping") if isinstance(data, dict) and data.get("mapping") is not None else {}
        _assert(isinstance(urls, dict) and isinstance(mapping, dict), "CLS 上传链接返回结构变化，停止")
        files: list[dict] = []
        for key, raw in urls.items():
            _assert(key in keys and isinstance(raw, str), "CLS 返回了未申请文件的上传链接，停止")
            try:
                url = urlsplit(raw)
                host = url.hostname or ""
                _ = url.port
            except ValueError:
                raise ClsError("CLS 返回的上传链接无效，停止") from None
            _assert(url.scheme in ("http", "https") and host, "CLS 返回的上传链接无效，停止")
            _assert((url.scheme == "https" or (self._allow_http_upload and url.scheme == "http")) and self._upload_host.fullmatch(host)
                    and not url.username and not url.password, f"CLS 返回的上传地址不是对象存储（{host}），停止")
            target = mapping.get(key)
            _assert(isinstance(target, str) and target.endswith(f"/{key[key.find('/') + 1:]}"), "CLS 上传文件名映射缺失或不符，停止")
            self._signed[key] = raw
            netloc = host + (f":{url.port}" if url.port else "")
            files.append({"key": key, "url": raw, "object": f"{netloc}{unquote(url.path)}", "expires_at": signature_end(raw)})
        return {"files": files, "mapping": {k: js.to_str(v) for k, v in mapping.items()}}

    def put(self, key: str, file: str, size: int) -> dict:
        """Puts one file to the link CLS gave for it. No CLS credentials go with it: the link carries its own signature."""
        url = self._signed.get(key)
        _assert(url, "没有这个文件的上传链接")
        _assert(os.path.getsize(file) == size, f"文件大小变化：{file}")
        start = js.now_ms()
        try:
            with open(file, "rb") as handle:
                response = self._fetch(url, method="PUT", headers={"Content-Type": PAGE_PUT_TYPE}, body=handle, length=size,
                                       timeout=self._upload_timeout_ms / 1000, redirect="error")
        except FetchError as error:
            raise ClsError(f"文件上传失败（{'超时' if error.timeout else '连接失败'}）：{file}") from None
        self._calls.append({"method": "PUT", "path": "<upload link>", "http_status": response.status, "elapsed_ms": js.now_ms() - start})
        _assert(200 <= response.status < 300, f"文件上传失败（HTTP {response.status}）：{file}")
        etag = response.header("etag")
        return {"etag": etag.replace('"', "") if etag is not None else None, "bytes": size}

    def log(self, message: str) -> None:
        """The page writes these two lines to the CLS log around an upload; failing to log never stops the upload."""
        try:
            self._request("POST", LOG, None, {"msg": message})
        except Exception:
            pass

    def create_creatives(self, game_id: int, names: list[str], directories: dict[str, int]) -> dict:
        _assert(len(names) > 0 and len(set(names)) == len(names) and all(js.is_safe_int(directories.get(n)) and directories[n] > 0 for n in names),
                "创意名称/上传目录不完整")
        data = self._request("POST", CREATE, [("confirmed", "1")], {"game_id": game_id, "names": names, "adsdesk_dir_ids": directories}, self._create_timeout_ms)
        succ = data.get("succ") if isinstance(data, dict) else None
        fail = (data.get("fail") if data.get("fail") is not None else []) if isinstance(data, dict) else []
        ok = (isinstance(succ, list) and isinstance(fail, list)
              and all(isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"] in names and js.is_safe_int(s.get("id")) and s["id"] > 0 for s in succ)
              and all(isinstance(f, str) for f in fail))
        if not ok:
            raise ClsError("创建结果结构不符；创意可能已创建，先回读核对，不重试", "unknown")
        return {"succ": [{"name": s["name"], "id": s["id"]} for s in succ], "fail": [js.to_str(f) for f in fail]}

    def annotate(self, game_id: int, creatives: list[dict]) -> None:
        _assert(len(creatives) > 0 and all(js.is_safe_int(c["id"]) and c["id"] > 0 and js.is_safe_int(c["sponsor"]) and c["sponsor"] > 0
                                           and len(c["producers"]) > 0 and all(js.is_safe_int(p) and p > 0 for p in c["producers"]) and len(c["types"]) > 0
                                           for c in creatives), "标注不完整")
        # The page's 保存 sends exactly these fields; custom_tags (创意标签) stays empty.
        self._request("POST", ANNOTATE, None, {"game_id": game_id, "creatives": [{"id": c["id"], "sponsor": c["sponsor"], "producers": c["producers"],
                                                                                   "types": c["types"], "custom_tags": []} for c in creatives]})

    def calls(self) -> list[dict]:
        return list(self._calls)


def create_creative_adapter(origin: str, authorization: str, **options: Any) -> CreativeAdapter:
    return CreativeAdapter(origin, authorization, **options)
