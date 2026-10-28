"""只读市场数据网关；仅代理数据专用域名的现货 GET 白名单。"""

from __future__ import annotations

import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .public_paths import PUBLIC_SPOT_PATHS


ORIGIN = "https://data-api.binance.vision"
FORBIDDEN_QUERY_KEYS = {"signature", "timestamp", "recvwindow", "apikey", "secret"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("公开行情网关禁止重定向")


def _upstream_get(path_and_query: str) -> bytes:
    request = urllib.request.Request(ORIGIN + path_and_query, method="GET",
                                     headers={"Accept": "application/json"})
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=10) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("公开行情响应超出容量限制")
    return data


def make_handler(upstream_get: Callable[[str], bytes] = _upstream_get):
    class PublicOnlyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            route = parsed.path.removeprefix("/api/v3/")
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if (parsed.scheme or parsed.netloc or parsed.fragment
                    or not parsed.path.startswith("/api/v3/") or route not in PUBLIC_SPOT_PATHS
                    or FORBIDDEN_QUERY_KEYS & {key.lower() for key in params}
                    or len(self.path) > 2048):
                self.send_error(403, "only public spot market data is allowed")
                return
            try:
                body = upstream_get(self.path)
            except (OSError, ValueError):
                self.send_error(503, "public market data unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.send_error(405, "write methods disabled")

        def do_PUT(self):
            self.send_error(405, "write methods disabled")

        def do_DELETE(self):
            self.send_error(405, "write methods disabled")

        def log_message(self, format, *args):
            # Never log query values, which could contain accidental secrets.
            pass

    return PublicOnlyHandler


def serve_forever(host: str = "0.0.0.0", port: int = 8088) -> None:
    server = ThreadingHTTPServer((host, port), make_handler())
    server.serve_forever(poll_interval=1)
