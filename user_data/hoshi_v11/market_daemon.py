"""未来手动启用的市场数据专用进程；不包含交易所下单依赖。"""

from __future__ import annotations

import logging
import os
import time
from threading import Thread
from datetime import datetime, timezone
from pathlib import Path

from .live_events import LiveMarketJournal, MarketEventError
from .public_klines import BinancePublicKlineSource
from .public_gateway import serve_forever


def run_forever(path: Path) -> None:
    journal = LiveMarketJournal(path)
    if not path.exists():
        journal.create()
    source = BinancePublicKlineSource(journal)
    Thread(target=serve_forever, name="public-market-gateway", daemon=True).start()
    while True:
        now = datetime.now(timezone.utc)
        if now.minute >= 1:
            target = now.replace(minute=0, second=0, microsecond=0)
            try:
                source.recover_until(decision_time=target)
            except MarketEventError as exc:
                logging.error("共享公开行情暂停：%s", exc)
        time.sleep(30)


def main() -> None:
    path = Path(os.environ.get("HOSHI_V11_MARKET_DB", "/market/shared.sqlite"))
    if path.name != "shared.sqlite":
        raise SystemExit("共享行情数据库文件名无效")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_forever(path)


if __name__ == "__main__":
    main()
