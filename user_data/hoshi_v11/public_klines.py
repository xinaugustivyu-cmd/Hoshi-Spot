"""Binance 市场数据专用域名的只读 K 线抓取器；无认证和循环服务入口。"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from .live_events import LiveMarketJournal, MarketEventError, PAIRS, TIMEFRAMES, utc
from .event_model import MarketEvent


MARKET_DATA_ORIGIN = "https://data-api.binance.vision"
KLINE_PATH = "/api/v3/klines"
DELTAS = {"1h": timedelta(hours=1), "4h": timedelta(hours=4)}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise MarketEventError("公开行情请求发生重定向")


def _public_get(url: str) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme != "https" or parsed.netloc != "data-api.binance.vision"
            or parsed.path != KLINE_PATH):
        raise MarketEventError("只允许 Binance 市场数据专用域名的 K 线 GET")
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=10) as response:
        return response.read(100_000)


class BinancePublicKlineSource:
    def __init__(self, journal: LiveMarketJournal,
                 transport: Callable[[str], bytes] = _public_get,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.journal = journal
        self.transport = transport
        self.clock = clock

    def _fetch(self, pair: str, timeframe: str, close_at: datetime) -> dict:
        duration = DELTAS[timeframe]
        opened = close_at - duration
        start_ms = int(opened.timestamp() * 1000)
        end_ms = int(close_at.timestamp() * 1000) - 1
        query = urllib.parse.urlencode({
            "symbol": pair.replace("/", ""), "interval": timeframe,
            "startTime": start_ms, "endTime": end_ms, "limit": 1,
        })
        url = MARKET_DATA_ORIGIN + KLINE_PATH + "?" + query
        rows = json.loads(self.transport(url))
        if (not isinstance(rows, list) or len(rows) != 1
                or not isinstance(rows[0], list) or len(rows[0]) < 7
                or int(rows[0][0]) != start_ms or int(rows[0][6]) != end_ms):
            raise MarketEventError(f"{pair} {timeframe} 公开 K 线时间戳不符")
        row = rows[0]
        return {
            "close_time_utc": close_at.isoformat(),
            "open": float(row[1]), "high": float(row[2]),
            "low": float(row[3]), "close": float(row[4]),
            "volume": float(row[5]),
        }

    def run_once(self, *, decision_time: datetime, backfill: bool = False) -> MarketEvent:
        decision = utc(decision_time)
        received = None
        try:
            if decision.minute or decision.second or decision.microsecond:
                raise MarketEventError("公开行情决策时间必须为 UTC 整点")
            candles = {}
            for pair in PAIRS:
                candles[pair] = {}
                for timeframe in TIMEFRAMES:
                    close_at = (decision if timeframe == "1h" else
                                decision.replace(hour=decision.hour - decision.hour % 4))
                    candles[pair][timeframe] = self._fetch(pair, timeframe, close_at)
            received = utc(self.clock())
            return self.journal.ingest(decision_time=decision, received_at=received,
                                       candles=candles, replay=backfill)
        except (MarketEventError, OSError, ValueError, json.JSONDecodeError) as exc:
            # Ingest already records its own validation faults. Fetch failures
            # must also invalidate the last published snapshot.
            if received is None:
                try:
                    self.journal.fault(f"公开行情抓取失败：{exc}", detected_at=utc(self.clock()))
                except (MarketEventError, OSError):
                    pass
            raise MarketEventError(str(exc)) from exc

    def recover_until(self, *, decision_time: datetime, max_backfill: int = 72) -> list[MarketEvent]:
        """断线后逐小时补齐；迟到事件不能成为当前新开仓行情。"""
        target = utc(decision_time)
        latest = self.journal.latest_close()
        if latest is None:
            return [self.run_once(decision_time=target)]
        missing = int((target - latest).total_seconds() // 3600)
        if missing < 0 or missing > max_backfill:
            raise MarketEventError("断线时间倒退或超过自动补齐上限")
        events = []
        for step in range(1, missing + 1):
            close_at = latest + timedelta(hours=step)
            events.append(self.run_once(decision_time=close_at,
                                        backfill=close_at < target))
        return events
