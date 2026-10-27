"""Build an auditable Markdown summary from Freqtrade's exported backtest ZIPs."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PAIRS = ("BTC/USDT", "ETH/USDT")


def read_strategy_result(path: Path, strategy_name: str) -> dict | None:
    with zipfile.ZipFile(path) as archive:
        candidates = [
            name for name in archive.namelist()
            if name.endswith(".json") and not name.endswith("_config.json")
        ]
        if not candidates:
            return None
        payload = json.loads(archive.read(candidates[0]))
    return payload.get("strategy", {}).get(strategy_name)


def holding_return(pair: str, start: str, end: str, fee: float) -> float | None:
    filename = pair.replace("/", "_") + "-1h.feather"
    frame = pd.read_feather(ROOT / "user_data" / "data" / "binance" / filename)
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    start_time = pd.Timestamp(start, tz="UTC")
    end_time = pd.Timestamp(end, tz="UTC")
    sample = frame[(frame["date"] >= start_time) & (frame["date"] < end_time)]
    if sample.empty:
        return None
    # Buy at the first available candle open and value/sell at the last close.
    return (sample.iloc[-1]["close"] / sample.iloc[0]["open"]) * (1 - fee) ** 2 - 1


def fees_paid(trades: list[dict]) -> float:
    total = 0.0
    for trade in trades:
        for order in trade.get("orders", []):
            rate = trade.get("fee_open", 0.0) if order.get("ft_is_entry") else trade.get("fee_close", 0.0)
            total += float(order.get("amount", 0.0)) * float(order.get("safe_price", 0.0)) * float(rate)
    return total


def payoff_ratio(trades: list[dict]) -> float | None:
    profits = [float(trade["profit_abs"]) for trade in trades]
    winners = [profit for profit in profits if profit > 0]
    losers = [profit for profit in profits if profit < 0]
    if not winners or not losers:
        return None
    return (sum(winners) / len(winners)) / abs(sum(losers) / len(losers))


def percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def money(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f} USDT"


def make_report(results_dir: Path, strategy_name: str) -> str:
    rows = []
    for archive in sorted(results_dir.glob("*.zip")):
        result = read_strategy_result(archive, strategy_name)
        if not result:
            continue
        trades = result.get("trades", [])
        start = result["backtest_start"]
        end = result["backtest_end"]
        fee_rates = [
            float(rate)
            for trade in trades
            for rate in (trade.get("fee_open"), trade.get("fee_close"))
            if rate is not None
        ]
        fee_rate = sum(fee_rates) / len(fee_rates) if fee_rates else 0.001
        btc_hold = holding_return("BTC/USDT", start, end, fee_rate)
        eth_hold = holding_return("ETH/USDT", start, end, fee_rate)
        benchmark = None if btc_hold is None or eth_hold is None else (btc_hold + eth_hold) / 2
        profit_factor = result.get("profit_factor")
        rows.append(
            {
                "file": archive.name,
                "start": start,
                "end": end,
                "trades": int(result.get("total_trades", len(trades))),
                "net": float(result.get("profit_total_abs", 0.0)),
                "return": float(result.get("profit_total", 0.0)),
                "drawdown": float(result.get("max_drawdown_account", 0.0)),
                "winrate": float(result.get("winrate", 0.0)),
                "payoff": payoff_ratio(trades),
                "profit_factor": float(profit_factor) if profit_factor is not None else None,
                "holding": result.get("holding_avg", "—"),
                "fees": fees_paid(trades),
                "btc_hold": btc_hold,
                "eth_hold": eth_hold,
                "benchmark": benchmark,
                "run_id": result.get("strategy_name", strategy_name),
                "fee_per_side": fee_rate,
                "protections": bool(result.get("enable_protections")),
            }
        )

    lines = [
        "# Hoshi Spot V1 历史回测实测摘要",
        "",
        "生成方式：读取 Freqtrade 导出的 JSON ZIP 和同区间 Binance 1h Feather K 线；未手工改写回测指标。",
        "净收益已扣除回测配置的交易费用；手续费成本按成交名义金额乘以每边费率估算。买入持有基准按 BTC/ETH 各 50% 初始资金、期初买入、期末卖出计算，包含两边费率，不再平衡。",
        "",
        "| 回测区间（UTC） | 交易数 | 净收益 | 收益率 | 最大回撤 | 胜率 | 盈亏比 | Profit Factor | 平均持仓 | 手续费估算 | BTC 持有 | ETH 持有 | 50/50 持有 | 费率/边 | 保护 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for row in sorted(rows, key=lambda item: (item["start"], item["end"], item["fee_per_side"])):
        lines.append(
            "| {start} → {end} | {trades} | {net} | {ret} | {dd} | {win} | {payoff} | {pf} | {holding} | {fees} | {btc} | {eth} | {bench} | {fee:.3%} | {protection} |".format(
                start=row["start"],
                end=row["end"],
                trades=row["trades"],
                net=money(row["net"]),
                ret=percent(row["return"]),
                dd=percent(row["drawdown"]),
                win=percent(row["winrate"]),
                payoff="—" if row["payoff"] is None else f"{row['payoff']:.2f}",
                pf="—" if row["profit_factor"] is None else f"{row['profit_factor']:.2f}",
                holding=row["holding"],
                fees=money(row["fees"]),
                btc=percent(row["btc_hold"]),
                eth=percent(row["eth_hold"]),
                bench=percent(row["benchmark"]),
                fee=row["fee_per_side"],
                protection="是" if row["protections"] else "否",
            )
        )
    lines.extend(
        [
            "",
            "## 解释限制",
            "",
            "- 2021、2022、2023 区间用于不同历史阶段观察；2024-2026 区间为未参与首版规则编写的样本外检查。阶段标签不是对未来行情的判断。",
            "- 2024-2026 样本外区间对每边 0.1%、0.15%、0.2% 做了成本敏感性检查；0.15% 可粗略代表基础费率加 0.05% 每边滑点，0.2% 是更高成本压力情景。它们都不是逐笔盘口滑点仿真。",
            "- Freqtrade MaxDrawdown 在平仓后评估并锁定；若单笔止损、跳空或滑点导致越过 5% 阈值，回撤仍可能略高于 5%。本轮 2021 与样本外回撤分别为 5.13% 和 5.33%，不能把阈值理解为保证上限。",
            "- 回测是基于 OHLCV 的模型结果，不代表未来收益或真实成交；历史买入持有区间比较也不代表风险相同。",
            "- 已有回测参数固定为说明书所要求规则，未运行参数寻优。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, default=ROOT / "user_data" / "backtest_results")
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "backtest_report_20261027.md")
    parser.add_argument("--strategy", default="HoshiSpotV1")
    args = parser.parse_args()
    report = make_report(args.results_dir, args.strategy)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"Wrote {args.output}")
    print(report)


if __name__ == "__main__":
    main()
