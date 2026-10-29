"""纸上执行专用：在 Freqtrade 导入前封锁 CCXT 私有/写入请求。"""

from __future__ import annotations

from functools import wraps
from urllib.parse import urlparse

from .public_paths import PUBLIC_SPOT_PATHS


class RealOrderBlocked(RuntimeError):
    pass


_installed = False


def is_installed() -> bool:
    return _installed


def _check_request(path: object, api: object, method: object) -> None:
    if (not isinstance(api, str) or api.lower() != "public"
            or str(method).upper() != "GET" or str(path).lstrip("/") not in PUBLIC_SPOT_PATHS):
        raise RealOrderBlocked("V1.1 只允许公开市场数据 GET；私有订单/账户端点已封锁")


def _check_fetch(url: object, method: object) -> None:
    parsed = urlparse(str(url))
    external = (parsed.scheme == "https" and
                parsed.hostname in {"api.binance.com", "data-api.binance.vision"})
    internal = (parsed.scheme == "http" and parsed.hostname == "hoshi-market-ingest"
                and parsed.port == 8088)
    if (str(method).upper() != "GET" or not (external or internal)
            or not parsed.path.startswith("/api/v3/")
            or parsed.path.removeprefix("/api/v3/") not in PUBLIC_SPOT_PATHS):
        raise RealOrderBlocked("V1.1 禁止非公开现货 GET 的网络请求")


def install() -> None:
    """幂等安装到同步及异步 CCXT 边界，并封锁 Freqtrade 非模拟下单。"""
    global _installed
    if _installed:
        return
    import ccxt
    import ccxt.async_support
    from freqtrade.exchange.exchange import Exchange as FreqtradeExchange

    sync_class = ccxt.Exchange
    async_class = ccxt.async_support.Exchange
    for cls, asynchronous in ((sync_class, False), (async_class, True)):
        for name in ("request", "fetch2", "fetch"):
            original = getattr(cls, name)
            if asynchronous:
                if name == "fetch":
                    @wraps(original)
                    async def wrapper(self, url, method="GET", *args, __original=original, **kwargs):
                        _check_fetch(url, method)
                        return await __original(self, url, method, *args, **kwargs)
                else:
                    @wraps(original)
                    async def wrapper(self, path, api="public", method="GET", *args,
                                      __original=original, **kwargs):
                        _check_request(path, api, method)
                        return await __original(self, path, api, method, *args, **kwargs)
            else:
                if name == "fetch":
                    @wraps(original)
                    def wrapper(self, url, method="GET", *args, __original=original, **kwargs):
                        _check_fetch(url, method)
                        return __original(self, url, method, *args, **kwargs)
                else:
                    @wraps(original)
                    def wrapper(self, path, api="public", method="GET", *args,
                                __original=original, **kwargs):
                        _check_request(path, api, method)
                        return __original(self, path, api, method, *args, **kwargs)
            setattr(cls, name, wrapper)

    original_create = FreqtradeExchange.create_order

    @wraps(original_create)
    def guarded_create(self, *args, **kwargs):
        if self._config.get("dry_run") is not True:
            raise RealOrderBlocked("V1.1 拒绝 Freqtrade 非模拟订单")
        return original_create(self, *args, **kwargs)

    FreqtradeExchange.create_order = guarded_create
    _installed = True
