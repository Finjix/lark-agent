"""A local HTTP server that answers from a script, standing in for the AI gateway and for Feishu's open API: no real network."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8") or "{}")


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json"
    #: Seconds to wait before answering (a gateway that is silent), or close the connection without an answer.
    delay: float = 0
    drop: bool = False


def json_reply(body: Any, status: int = 200, content_type: str = "application/json; charset=utf-8") -> Reply:
    return Reply(status=status, body=json.dumps(body, ensure_ascii=False).encode("utf-8"), content_type=content_type)


@dataclass
class FakeServer:
    """`handle` gets every request and returns the reply; `requests` keeps them all."""
    handle: Callable[[Request], Reply]
    requests: list[Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                parts = urlsplit(self.path)
                request = Request(self.command, parts.path, {k: v[-1] for k, v in parse_qs(parts.query).items()},
                                  {k.lower(): v for k, v in self.headers.items()}, self.rfile.read(length) if length else b"")
                outer.requests.append(request)
                reply = outer.handle(request)
                if reply.delay:
                    threading.Event().wait(reply.delay)
                if reply.drop:
                    self.close_connection = True
                    return
                try:
                    self.send_response(reply.status)
                    self.send_header("content-type", reply.content_type)
                    self.send_header("content-length", str(len(reply.body)))
                    self.end_headers()
                    self.wfile.write(reply.body)
                except OSError:
                    pass  # the client gave up waiting

            do_GET = _serve
            do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self) -> "FakeServer":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
