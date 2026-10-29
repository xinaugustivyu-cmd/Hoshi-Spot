"""历史与实时行情共用的无交易权限事件模型。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class MarketEvent:
    event_id: str
    decision_time_utc: datetime
    one_hour_close_utc: datetime
    candles: dict[str, dict[str, dict]]
    received_at_utc: datetime | None

    def price(self, pair: str) -> float:
        return float(self.candles[pair]["1h"]["close"])


def event_digest(decision_time_utc: datetime, candles: dict) -> str:
    raw = json.dumps({"time": decision_time_utc.astimezone(timezone.utc).isoformat(),
                      "candles": candles}, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
