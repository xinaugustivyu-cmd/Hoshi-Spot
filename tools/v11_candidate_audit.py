"""重建 A 组指标层候选，独立估算长期风险锁覆盖的信号。只读历史数据。"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from hoshi.indicators import add_1h_indicators, add_4h_indicators, entry_mask, merge_closed_informative
from hoshi_v11.market_events import HistoricalEventFeed
from compare_v11_replay import load_backtest


def build_candidates(data_dir: Path, params: dict) -> set[tuple[str, datetime]]:
    candidates = set()
    for pair in ("BTC", "ETH"):
        base = pd.read_feather(data_dir / f"{pair}_USDT-1h.feather")
        higher = pd.read_feather(data_dir / f"{pair}_USDT-4h.feather")
        base["date"] = pd.to_datetime(base["date"], utc=True).astype("datetime64[ns, UTC]")
        higher["date"] = pd.to_datetime(higher["date"], utc=True).astype("datetime64[ns, UTC]")
        base = add_1h_indicators(base, params)
        higher = add_4h_indicators(higher, params)
        # Freqtrade aligns a 4h bar starting 16:00 to the 1h bar starting
        # 19:00: both close at 20:00, then the decision is made. The generic
        # helper shifts by 4h, so move its input back 1h to reproduce the
        # Freqtrade 4h-minus-1h merge offset without changing frozen V1 code.
        higher["date"] -= pd.Timedelta(hours=1)
        merged = merge_closed_informative(base, higher)
        merged = merged.rename(columns={"trend_ok_informative": "trend_ok_4h"})
        valid = merged.loc[entry_mask(merged, params), "date"]
        for candle_start in valid:
            start = pd.Timestamp(candle_start).to_pydatetime().astimezone(timezone.utc)
            candidates.add((f"{pair}/USDT", start + timedelta(hours=1)))
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--risk-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    params = json.loads(args.config.read_text(encoding="utf-8"))["strategy_parameters"]
    all_candidates = build_candidates(args.data_dir, params)
    original = load_backtest(args.original, "HoshiSpotV1")
    candidate = load_backtest(args.candidate, "HoshiSpotV11A")
    original_entries = {(trade["pair"], datetime.fromisoformat(trade["open_date"]))
                        for trade in original["trades"]}
    candidate_entries = {(trade["pair"], datetime.fromisoformat(trade["open_date"]))
                         for trade in candidate["trades"]}
    original_missing = original_entries - all_candidates
    candidate_missing = candidate_entries - all_candidates
    if original_missing or candidate_missing:
        raise SystemExit(
            f"指标候选重建未覆盖实际开仓：V1 {sorted(original_missing)[:5]}，"
            f"A {sorted(candidate_missing)[:5]}"
        )
    with sqlite3.connect(f"file:{args.risk_db.as_posix()}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT observed_at FROM risk_events WHERE state='HALTED_MANUAL_REVIEW' "
            "ORDER BY id LIMIT 1"
        ).fetchone()
    if row is None:
        raise SystemExit("A 组没有长期人工停机，不能估算被锁候选")
    halt_at = datetime.fromisoformat(row[0])
    last_close = max(datetime.fromisoformat(trade["close_date"])
                     for trade in candidate["trades"])
    last_close = last_close.replace(tzinfo=timezone.utc) if last_close.tzinfo is None else last_close
    end = datetime.fromisoformat(candidate["backtest_end"])
    end = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end
    # Only count hours after the final position has closed; before that, a
    # candidate could also be rejected by the one-position rule.
    since = max(halt_at, last_close + timedelta(hours=1))
    feed = HistoricalEventFeed(args.data_dir)
    rows = []
    for pair, at in sorted(all_candidates, key=lambda item: (item[1], item[0])):
        if since <= at < end:
            event = feed.at(at)
            rows.append({
                "pair": pair,
                "signal_candle_close_utc": at.isoformat(),
                "shared_market_event_id": event.event_id,
                "risk_state": "HALTED_MANUAL_REVIEW",
                "interpretation": "指标层候选；原生保护可能在回调前拦截，非可成交订单",
            })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[
            "pair", "signal_candle_close_utc", "shared_market_event_id",
            "risk_state", "interpretation",
        ])
        writer.writeheader()
        writer.writerows(rows)
    pairs = {pair: sum(row["pair"] == pair for row in rows)
             for pair in ("BTC/USDT", "ETH/USDT")}
    print(json.dumps({
        "halt_at": halt_at.isoformat(), "counted_from": since.isoformat(),
        "end_exclusive": end.isoformat(), "blocked_indicator_candidates": len(rows),
        "by_pair": pairs, "baseline_entry_coverage": len(original_entries),
        "candidate_entry_coverage": len(candidate_entries),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
