"""纸上执行层与公开行情网关共用的现货只读端点清单。"""

PUBLIC_SPOT_PATHS = frozenset({
    "exchangeInfo", "time", "klines", "uiKlines", "ticker/price",
    "ticker/bookTicker", "ticker/24hr", "depth", "trades", "aggTrades",
})
