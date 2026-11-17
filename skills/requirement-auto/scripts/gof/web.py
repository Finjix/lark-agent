"""A small ``fetch`` for the CLS clients: one request, one answer, no redirects followed, no implicit retry.

The clients follow a proxy from the environment only when told to (REQUIREMENT_AUTO_USE_ENV_PROXY=1) and refuse a redirect when asked to
(`redirect: 'error'`). urllib does both by default, so this module turns them off: what goes out is exactly the
one request the client means, to the host it names.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import socket
import urllib.error
import functools
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

from . import timing


class FetchError(Exception):
    """No answer at all: the connection failed, timed out, or a redirect was refused. `timeout` tells the first apart."""

    def __init__(self, message: str, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        """The body as JSON; raises ValueError when it is not JSON (as response.json() rejects)."""
        return json.loads(self.body.decode("utf-8"))

    def header(self, name: str) -> Optional[str]:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib's hook
        if self.mode == "manual":
            return None  # the 3xx comes back as the answer
        raise FetchError("redirect refused")


def _use_env_proxy() -> bool:
    return os.environ.get("REQUIREMENT_AUTO_USE_ENV_PROXY") == "1"


Body = Union[bytes, str, None, "MultipartForm", io.IOBase]


class MultipartForm:
    """``FormData`` with text fields and one or more files, encoded the way a browser (and undici) sends it."""

    def __init__(self) -> None:
        self.parts: list[tuple[str, Any, Optional[str], Optional[str]]] = []
        self.boundary = "----formdata-" + secrets.token_hex(12)

    def append(self, name: str, value: Union[str, bytes], filename: Optional[str] = None, content_type: Optional[str] = None) -> None:
        self.parts.append((name, value, filename, content_type))

    @staticmethod
    def _escape(text: str) -> str:
        return text.replace("\r", "%0D").replace("\n", "%0A").replace('"', "%22")

    def encode(self) -> bytes:
        out = bytearray()
        for name, value, filename, content_type in self.parts:
            out += f"--{self.boundary}\r\n".encode()
            if filename is None:
                out += f'Content-Disposition: form-data; name="{self._escape(name)}"\r\n\r\n'.encode("utf-8")
                out += value.encode("utf-8") if isinstance(value, str) else value
            else:
                out += f'Content-Disposition: form-data; name="{self._escape(name)}"; filename="{self._escape(filename)}"\r\n'.encode("utf-8")
                out += f"Content-Type: {content_type or 'application/octet-stream'}\r\n\r\n".encode()
                out += value if isinstance(value, bytes) else value.encode("utf-8")
            out += b"\r\n"
        out += f"--{self.boundary}--\r\n".encode()
        return bytes(out)

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"


Fetcher = Callable[..., Response]


def fetch(url: str, method: str = "GET", headers: Optional[dict[str, str]] = None, body: Body = None, timeout: float = 30.0,
          redirect: str = "error", length: Optional[int] = None) -> Response:
    """One HTTP request. Any HTTP status is an answer; no answer at all raises FetchError."""
    headers = dict(headers or {})
    data: Any = None
    if isinstance(body, MultipartForm):
        data = body.encode()
        headers["Content-Type"] = body.content_type
    elif isinstance(body, str):
        data = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    elif body is not None:
        data = body  # a file: streamed
        if length is not None:
            headers["Content-Length"] = str(length)
    handlers: list[Any] = [_NoRedirect(redirect)]
    handlers.append(urllib.request.ProxyHandler() if _use_env_proxy() else urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as answer:
            return Response(answer.status, dict(answer.headers.items()), answer.read())
    except urllib.error.HTTPError as error:
        try:
            content = error.read()
        except Exception:
            content = b""
        return Response(error.code, dict(error.headers.items()) if error.headers else {}, content)
    except FetchError:
        raise
    except (socket.timeout, TimeoutError) as error:
        raise FetchError(str(error) or "timeout", timeout=True) from None
    except urllib.error.URLError as error:
        reason = error.reason
        raise FetchError(str(reason), timeout=isinstance(reason, (socket.timeout, TimeoutError))) from None
    except (OSError, ValueError) as error:
        raise FetchError(str(error)) from None


_fetch_untimed = fetch


@functools.wraps(_fetch_untimed)
def fetch(url: str, method: str = "GET", *args: Any, **kwargs: Any) -> Response:
    with timing.phase(timing.http_name(method, url)):
        return _fetch_untimed(url, method, *args, **kwargs)
