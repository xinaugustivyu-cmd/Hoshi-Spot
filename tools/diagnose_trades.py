"""Export per-trade attribution and OHLCV MFE/MAE from Freqtrade result ZIPs."""

from __future__ import annotations

import argparse
import json
import zipfile
from collections import defaultdict
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
STRATEGY = "HoshiSpotV1"


def strategy_payload(path: Path) -> tuple[dict, str]:
    with zipfile.ZipFile(path) as archive:
        name = next(
            item for item in archive.namelist()
            if item.endswith(".json") and not item.endswith("_config.json")
        )
        payload = json.loads(archive.read(name))
    return payload["strategy"][STRATEGY], name


def reason_group(reason: str) -> str:
    reason = reason.lower()
    if reason in {"roi", "minimal_roi", "profit_target"}:
        return "profit_target"
    if reason in {"stop_loss", "stoploss", "trailing_stop_loss"}:
        return "stop_loss"
    if reason.startswith("trend_invalid"):
        return "ema_rsi_early_exit"
    if reason == "max_holding_14d":
        return "max_holding_14d"
    return "other"


def fee_cost(trade: dict) -> float:
    return sum(
        float(order.get("amount", 0)) * float(order.get("safe_price", 0))
        * float(trade.get("fee_open" if order.get("ft_is_entry") else "fee_close", 0))
        for order in trade.get("orders", [])
    )


def candle_excursions(pair: str, opened: pd.Timestamp, closed: pd.Timestamp, rate: float) -> tuple[float | None, float | None, int]:
    filename = pair.replace("/", "_") + "-1h.feather"
    frame = pd.read_feather(ROOT / "user_data" / "data" / "binance" / filename)
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    # Freqtrade timestamps OHLCV candles at their UTC opening time. Include both
    # entry and exit candles; these are approximate excursions, not tick path data.
    view = frame[(frame["date"] >= opened.floor("h")) & (frame["date"] <= closed.floor("h"))]
    if view.empty:
        return None, None, 0
    return float(view["high"].max() / rate - 1), float(view["low"].min() / rate - 1), len(view)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, default=ROOT / "user_data" / "backtest_results")
    parser.add_argument("--output", type=Path, default=ROOT / "trade_breakdown.csv")
    args = parser.parse_args()

    rows: list[dict] = []
    runs: list[dict] = []
    for archive in sorted(args.results_dir.glob("backtest-result-*.zip")):
        result, _ = strategy_payload(archive)
        trades = result.get("trades", [])
        rates = [float(t.get("fee_open", 0)) for t in trades]
        fee_rate = sum(rates) / len(rates) if rates else 0.001
        run = {
            "source_zip": archive.name,
            "start_utc": result["backtest_start"],
            "end_utc": result["backtest_end"],
            "fee_rate_each_side": fee_rate,
            "protections_enabled": bool(result.get("enable_protections")),
            "trades": trades,
            "result": result,
        }
        runs.append(run)
        for index, trade in enumerate(trades, start=1):
            opened = pd.Timestamp(trade["open_date"])
            closed = pd.Timestamp(trade["close_date"])
            if opened.tzinfo is None:
                opened = opened.tz_localize("UTC")
            else:
                opened = opened.tz_convert("UTC")
            if closed.tzinfo is None:
                closed = closed.tz_localize("UTC")
            else:
                closed = closed.tz_convert("UTC")
            mfe, mae, candle_count = candle_excursions(
                trade["pair"], opened, closed, float(trade["open_rate"])
            )
            orders = trade.get("orders", [])
            entry_notional = sum(
                float(order.get("amount", 0)) * float(order.get("safe_price", 0))
                for order in orders if order.get("ft_is_entry")
            )
            exit_notional = sum(
                float(order.get("amount", 0)) * float(order.get("safe_price", 0))
                for order in orders if not order.get("ft_is_entry")
            )
            rows.append({
                "source_zip": archive.name,
                "period_start_utc": run["start_utc"],
                "period_end_utc": run["end_utc"],
                "fee_rate_each_side": fee_rate,
                "protections_enabled": run["protections_enabled"],
                "trade_no_in_result": index,
                "pair": trade["pair"],
                "open_time_utc": opened.isoformat(),
                "close_time_utc": closed.isoformat(),
                "holding_hours": (closed - opened).total_seconds() / 3600,
                "entry_rate": float(trade["open_rate"]),
                "exit_rate": float(trade["close_rate"]),
                "stake_usdt": float(trade.get("stake_amount", 0)),
                "profit_net_usdt": float(trade["profit_abs"]),
                "profit_net_pct": float(trade["profit_ratio"]),
                "fees_usdt": fee_cost(trade),
                "gross_mfe_pct_ohlcv_approx": mfe,
                "gross_mae_pct_ohlcv_approx": mae,
                "excursion_candle_count": candle_count,
                "raw_exit_reason": trade.get("exit_reason", ""),
                "exit_group": reason_group(trade.get("exit_reason", "")),
                "entry_tag": trade.get("enter_tag", ""),
                "entry_notional_usdt": entry_notional,
                "exit_notional_usdt": exit_notional,
            })

    columns = list(rows[0]) if rows else []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=columns).to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"Wrote {args.output} ({len(rows)} trades; {len(runs)} result ZIPs)")

    # Primary attribution excludes overlapping fee-sensitivity reruns: use only
    # the earliest available 0.1%/side canonical runs for each distinct period.
    primary: list[dict] = []
    seen_periods: set[tuple[str, str]] = set()
    for run in runs:
        period = (run["start_utc"], run["end_utc"])
        if abs(run["fee_rate_each_side"] - 0.001) < 1e-9 and period not in seen_periods:
            primary.extend(
                row for row in rows
                if row["source_zip"] == run["source_zip"]
            )
            seen_periods.add(period)

    def stats(items: list[dict]) -> dict:
        pnl = [float(row["profit_net_usdt"]) for row in items]
        wins = [x for x in pnl if x > 0]
        losses = [x for x in pnl if x < 0]
        return {
            "trades": len(items),
            "win_rate_pct": 100 * len(wins) / len(items) if items else 0,
            "net_usdt": sum(pnl),
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "avg_hold_all_h": sum(float(row["holding_hours"]) for row in items) / len(items) if items else None,
            "avg_hold_winners_h": sum(float(row["holding_hours"]) for row in items if float(row["profit_net_usdt"]) > 0) / len(wins) if wins else None,
            "avg_hold_losers_h": sum(float(row["holding_hours"]) for row in items if float(row["profit_net_usdt"]) < 0) / len(losses) if losses else None,
        }

    print("PRIMARY_PAIR_STATS (0.1% per side, non-overlapping periods)")
    for pair in ("BTC/USDT", "ETH/USDT"):
        selected = [row for row in primary if row["pair"] == pair]
        print(pair, json.dumps(stats(selected), ensure_ascii=False))
        reasons: dict[str, list[dict]] = defaultdict(list)
        for row in selected:
            reasons[row["exit_group"]].append(row)
        for group, group_rows in sorted(reasons.items()):
            print("  EXIT", group, json.dumps({
                **stats(group_rows),
                "positive_net_pnl_subtotal_usdt": sum(float(r["profit_net_usdt"]) for r in group_rows if float(r["profit_net_usdt"]) > 0),
                "negative_net_pnl_subtotal_usdt": sum(float(r["profit_net_usdt"]) for r in group_rows if float(r["profit_net_usdt"]) < 0),
                "avg_mfe_pct_ohlcv_approx": sum(float(r["gross_mfe_pct_ohlcv_approx"]) for r in group_rows if r["gross_mfe_pct_ohlcv_approx"] is not None) / sum(r["gross_mfe_pct_ohlcv_approx"] is not None for r in group_rows),
                "avg_mae_pct_ohlcv_approx": sum(float(r["gross_mae_pct_ohlcv_approx"]) for r in group_rows if r["gross_mae_pct_ohlcv_approx"] is not None) / sum(r["gross_mae_pct_ohlcv_approx"] is not None for r in group_rows),
            }, ensure_ascii=False))

    early = [r for r in primary if r["exit_group"] == "ema_rsi_early_exit"]
    for threshold in (0.01, 0.02, 0.04, 0.06):
        selected = [r for r in early if r["gross_mfe_pct_ohlcv_approx"] is not None and float(r["gross_mfe_pct_ohlcv_approx"]) >= threshold]
        print("EARLY_EXIT_MFE", threshold, json.dumps({
            "trades_with_mfe_ge_threshold": len(selected),
            "closed_net_loss": sum(float(r["profit_net_usdt"]) < 0 for r in selected),
            "net_pnl_usdt": sum(float(r["profit_net_usdt"]) for r in selected),
        }, ensure_ascii=False))

    repeats = []
    churn = {"within_24h_any_exit": 0, "within_48h_any_exit": 0, "within_48h_after_loss": 0}
    for pair in ("BTC/USDT", "ETH/USDT"):
        ordered = sorted((r for r in primary if r["pair"] == pair), key=lambda r: r["open_time_utc"])
        for previous, current in zip(ordered, ordered[1:]):
            elapsed_h = (pd.Timestamp(current["open_time_utc"]) - pd.Timestamp(previous["close_time_utc"])).total_seconds() / 3600
            if 0 <= elapsed_h <= 24:
                churn["within_24h_any_exit"] += 1
            if 0 <= elapsed_h <= 48:
                churn["within_48h_any_exit"] += 1
                if float(previous["profit_net_usdt"]) < 0:
                    churn["within_48h_after_loss"] += 1
            if previous["exit_group"] == "stop_loss" and current["exit_group"] == "stop_loss" and 0 <= elapsed_h <= 48:
                repeats.append((pair, previous["close_time_utc"], current["open_time_utc"], elapsed_h))
    print("REENTRY_CHURN", json.dumps(churn, ensure_ascii=False))
    print("REPEATED_STOP_LOSSES_WITHIN_48H", json.dumps(repeats, ensure_ascii=False))

    print("OHLCV_DATA_QUALITY")
    for filename in sorted((ROOT / "user_data" / "data" / "binance").glob("*.feather")):
        frame = pd.read_feather(filename)
        frame["date"] = pd.to_datetime(frame["date"], utc=True)
        expected_hours = 1 if filename.stem.endswith("-1h") else 4
        deltas = frame["date"].diff().dropna()
        gaps = deltas[deltas > pd.Timedelta(hours=expected_hours)]
        print(filename.name, json.dumps({
            "rows": len(frame), "first_utc": frame["date"].min().isoformat(),
            "last_utc": frame["date"].max().isoformat(),
            "duplicate_timestamps": int(frame["date"].duplicated().sum()),
            "non_monotonic": not frame["date"].is_monotonic_increasing,
            "missing_ohlcv_cells": int(frame[["open", "high", "low", "close", "volume"]].isna().sum().sum()),
            "gap_count": int(len(gaps)),
            "missing_candles_estimate": int(sum(max(0, int(delta / pd.Timedelta(hours=expected_hours)) - 1) for delta in gaps)),
            "largest_gap_hours": float(deltas.max() / pd.Timedelta(hours=1)) if len(deltas) else 0,
            "gaps_utc": [
                {
                    "previous_candle_open": frame.iloc[index - 1]["date"].isoformat(),
                    "next_candle_open": frame.iloc[index]["date"].isoformat(),
                    "missing_candles": int((frame.iloc[index]["date"] - frame.iloc[index - 1]["date"]) / pd.Timedelta(hours=expected_hours)) - 1,
                }
                for index in range(1, len(frame))
                if frame.iloc[index]["date"] - frame.iloc[index - 1]["date"] > pd.Timedelta(hours=expected_hours)
            ],
        }, ensure_ascii=False))

    print("ALL_RUNS")
    for run in runs:
        print(run["source_zip"], json.dumps({
            "start": run["start_utc"], "end": run["end_utc"],
            "fee": run["fee_rate_each_side"], "protections": run["protections_enabled"],
            "reported_trades": run["result"].get("total_trades"),
            "net_usdt": run["result"].get("profit_total_abs"),
            "max_drawdown_account": run["result"].get("max_drawdown_account"),
        }, ensure_ascii=False))

    # Frozen-path fee counterfactual on the 0.1% OOS run: same pair, timestamps,
    # order quantities, and exit reasons; only the fee rate is changed.
    base_oos = next((r for r in runs if r["start_utc"].startswith("2024-01-01") and abs(r["fee_rate_each_side"] - 0.001) < 1e-9), None)
    if base_oos:
        base_trades = base_oos["trades"]
        base_net = sum(float(t["profit_abs"]) for t in base_trades)
        base_fees = sum(fee_cost(t) for t in base_trades)
        turnover = sum(
            sum(float(o.get("amount", 0)) * float(o.get("safe_price", 0)) for o in t.get("orders", []))
            for t in base_trades
        )
        print("FROZEN_OOS_COST_PATH", json.dumps({
            "trades": len(base_trades), "base_net_usdt": base_net,
            "actual_fee_usdt_at_0.1pct": base_fees,
            "two_way_notional_usdt": turnover,
            "net_if_fee_0.15pct_same_trades_fixed_size": base_net - turnover * 0.0005,
            "net_if_fee_0.20pct_same_trades_fixed_size": base_net - turnover * 0.001,
        }, ensure_ascii=False))
        def signature(trade: dict) -> tuple[str, int]:
            return trade["pair"], int(trade["open_timestamp"])

        base_signatures = {signature(t): t for t in base_trades}
        print("OOS_EXIT_PATHS_BY_FEE")
        for run in runs:
            if not run["start_utc"].startswith("2024-01-01"):
                continue
            current = {signature(t): t for t in run["trades"]}
            common = set(base_signatures) & set(current)
            matching_exit = sum(
                base_signatures[key].get("exit_reason") == current[key].get("exit_reason")
                and base_signatures[key].get("close_timestamp") == current[key].get("close_timestamp")
                for key in common
            )
            changed_exits = [
                {
                    "pair": key[0],
                    "open_utc": base_signatures[key].get("open_date"),
                    "base_exit": base_signatures[key].get("exit_reason"),
                    "base_close_utc": base_signatures[key].get("close_date"),
                    "fee_run_exit": current[key].get("exit_reason"),
                    "fee_run_close_utc": current[key].get("close_date"),
                }
                for key in common
                if base_signatures[key].get("exit_reason") != current[key].get("exit_reason")
                or base_signatures[key].get("close_timestamp") != current[key].get("close_timestamp")
            ]
            reason_counts: dict[str, int] = defaultdict(int)
            for trade in run["trades"]:
                reason_counts[reason_group(trade.get("exit_reason", ""))] += 1
            print(run["source_zip"], json.dumps({
                "fee_each_side": run["fee_rate_each_side"],
                "trade_count": len(run["trades"]),
                "common_entries_with_0.1pct": len(common),
                "common_entries_same_exit_and_time": matching_exit,
                "entries_only_in_this_run": len(set(current) - set(base_signatures)),
                "entries_missing_vs_0.1pct": len(set(base_signatures) - set(current)),
                "exit_reason_counts": dict(reason_counts),
                "changed_exits": changed_exits,
            }, ensure_ascii=False))


if __name__ == "__main__":
    main()
