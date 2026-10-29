"""从 A/B/C 离线回测结果导出逐笔模拟记录，未知的真实事件时刻留空。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from hoshi_v11.market_events import HistoricalEventFeed


def trades_from_zip(path: Path, strategy: str) -> list[dict]:
    with zipfile.ZipFile(path) as archive:
        name = next(item for item in archive.namelist()
                    if item.endswith(".json") and not item.endswith("_config.json"))
        data = json.loads(archive.read(name))
    return data["strategy"][strategy]["trades"]


def export(data_dir: Path, files: dict[str, Path], output: Path) -> int:
    feed = HistoricalEventFeed(data_dir)
    rows = []
    for account in "ABC":
        strategy = f"HoshiSpotV11{account}"
        for trade in trades_from_zip(files[account], strategy):
            opened = datetime.fromisoformat(trade["open_date"])
            closed = datetime.fromisoformat(trade["close_date"])
            if opened.tzinfo is None or closed.tzinfo is None:
                raise ValueError("回测交易时间缺少 UTC 时区")
            event = feed.at(opened)
            signal_id = hashlib.sha256(
                f"{account}|{trade['pair']}|{opened.isoformat()}".encode("utf-8")
            ).hexdigest()
            entry = next((order for order in trade["orders"] if order["ft_is_entry"]), None)
            exit_order = next((order for order in trade["orders"] if not order["ft_is_entry"]), None)
            if entry is None or exit_order is None:
                raise ValueError("回测交易缺少模拟入场或出场订单记录")
            filled_at = datetime.fromtimestamp(entry["order_filled_timestamp"] / 1000, tz=timezone.utc)
            exit_filled_at = datetime.fromtimestamp(exit_order["order_filled_timestamp"] / 1000,
                                                    tz=timezone.utc)
            fee_cost = (float(trade["stake_amount"]) * float(trade["fee_open"])
                        + float(trade["amount"]) * float(trade["close_rate"]) * float(trade["fee_close"]))
            rows.append({
                "account_id": account, "strategy_version": strategy,
                "is_simulated": "true", "signal_id": signal_id,
                "market_event_id": event.event_id,
                "signal_candle_close_utc": opened.isoformat(),
                "signal_generated_at_utc": "",  # historical vectorized run has no wall-clock signal time
                "simulated_decision_time_utc": opened.isoformat(),
                "order_submitted_at_utc": "",  # not exported by Freqtrade backtest ZIP
                "entry_filled_at_utc": filled_at.isoformat(),
                "entry_price": trade["open_rate"], "quantity": trade["amount"],
                "stake_usdt": trade["stake_amount"], "fee_cost_usdt_est": round(fee_cost, 8),
                "slippage_budget_per_side": 0.0005, "slippage_model_applied": "false",
                "exit_filled_at_utc": exit_filled_at.isoformat(),
                "exit_price": trade["close_rate"], "exit_reason": trade["exit_reason"],
                "duration_hours": trade["trade_duration"] / 60,
                "net_profit_usdt": trade["profit_abs"], "pair": trade["pair"],
                "timestamp_limit": "历史回测没有真实信号生成或订单提交时刻",
            })
    rows.sort(key=lambda row: (row["entry_filled_at_utc"], row["account_id"], row["pair"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    for account in "abc":
        parser.add_argument(f"--{account}-zip", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = export(args.data_dir, {account: getattr(args, f"{account.lower()}_zip")
                                   for account in "ABC"}, args.output)
    print(f"已导出 {count} 笔离线模拟交易记录")


if __name__ == "__main__":
    main()
