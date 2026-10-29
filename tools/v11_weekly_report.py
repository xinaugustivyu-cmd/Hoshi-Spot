"""从三个离线回测 ZIP 与独立风险账本生成中文周报；不连接交易所。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


UTC = timezone.utc
HKT = timezone(timedelta(hours=8))


def read_backtest(path: Path, strategy: str) -> dict:
    with zipfile.ZipFile(path) as archive:
        name = next(name for name in archive.namelist()
                    if name.endswith(".json") and not name.endswith("_config.json"))
        return json.loads(archive.read(name))["strategy"][strategy]


def fmt(value: datetime) -> str:
    return value.astimezone(HKT).strftime("%Y-%m-%d %H:%M UTC+8")


def metrics(trades: list[dict]) -> dict:
    gains = sum(trade["profit_abs"] for trade in trades if trade["profit_abs"] > 0)
    losses = -sum(trade["profit_abs"] for trade in trades if trade["profit_abs"] < 0)
    fees = sum(
        float(trade["stake_amount"]) * float(trade["fee_open"])
        + float(trade["amount"]) * float(trade["close_rate"]) * float(trade["fee_close"])
        for trade in trades
    )
    return {
        "count": len(trades), "net": sum(trade["profit_abs"] for trade in trades),
        "win": sum(trade["profit_abs"] > 0 for trade in trades) / len(trades) if trades else None,
        "pf": gains / losses if losses else (None if not gains else float("inf")),
        "hold_hours": sum(trade["trade_duration"] for trade in trades) / 60 / len(trades) if trades else None,
        "fees": fees,
    }


def inspect_risk(path: Path, start: datetime, end: datetime) -> dict:
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT observed_at, state, rule FROM risk_events "
            "WHERE rule NOT IN ('trade_closed','week_opening_snapshot','blocked_candidate',"
            "'approval_rejected','manual_resume') ORDER BY id"
        ).fetchall()
        marks = connection.execute(
            "SELECT observed_at, equity, stage_drawdown FROM equity_marks "
            "WHERE observed_at <= ? ORDER BY id", (end.isoformat(),)
        ).fetchall()
        blocked = connection.execute(
            "SELECT COUNT(*) FROM risk_events WHERE rule='blocked_candidate' "
            "AND observed_at >= ? AND observed_at < ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()[0]
        faults = connection.execute(
            "SELECT COUNT(*) FROM risk_events WHERE rule='invalid_market_data' "
            "AND observed_at >= ? AND observed_at < ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()[0]
        closed = connection.execute(
            "SELECT COALESCE(SUM(net_profit_usdt),0) FROM closed_trades WHERE closed_at <= ?",
            (end.isoformat(),),
        ).fetchone()[0]
        policy_hash = json.loads(connection.execute(
            "SELECT value FROM meta WHERE key='policy_hash'"
        ).fetchone()[0])
    transitions = []
    last_state = "ACTIVE"
    for time_text, state, rule in rows:
        if state != last_state:
            transitions.append((datetime.fromisoformat(time_text), state, rule))
            last_state = state
    locks = Counter()
    counts = Counter()
    for index, (time, state, _) in enumerate(transitions):
        if state == "ACTIVE":
            continue
        until = transitions[index + 1][0] if index + 1 < len(transitions) else end
        overlap = max(0, (min(until, end) - max(time, start)).total_seconds())
        if overlap > 0:
            locks[state] += overlap / 3600
            counts[state] += 1
    in_week_marks = [(at, equity, dd) for at, equity, dd in marks
                     if datetime.fromisoformat(at) >= start]
    equity = marks[-1][1] if marks else None
    unrealized = (equity - 80 - closed) if equity is not None else None
    return {
        "counts": counts, "hours": locks, "faults": faults, "blocked": blocked,
        "equity": equity, "unrealized": unrealized,
        "max_dd": max((row[2] for row in in_week_marks), default=None),
        "policy_hash": policy_hash,
    }


def render(start: datetime, end: datetime, data: dict[str, tuple[dict, dict]]) -> str:
    lines = [
        "# Hoshi V1.1 三组离线研究周报示例",
        "",
        f"报告窗口：{fmt(start)} 至 {fmt(end)}。原始 UTC：`{start.isoformat()}` 至 `{end.isoformat()}`。",
        "历史 K 线回测生成，**不是正在运行的三个 Dry Run 账户**；历史记录没有真实网络接收/成交时间。",
        "",
        "| 账户 | 交易数 | 净收益 USDT | 胜率 | Profit Factor | 平均持仓 h | 手续费估算 USDT | 最大采样回撤 | 未实现盈亏 USDT | 期末模拟权益 USDT |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    policy_hashes = set()
    for account in "ABC":
        trades, risk = data[account]
        metric = metrics(trades)
        policy_hashes.add(risk["policy_hash"])
        win = f"{metric['win']*100:.1f}%" if metric["win"] is not None else "—"
        pf = f"{metric['pf']:.2f}" if metric["pf"] is not None else "—"
        hold = f"{metric['hold_hours']:.1f}" if metric["hold_hours"] is not None else "—"
        drawdown = f"{risk['max_dd']*100:.2f}%" if risk["max_dd"] is not None else "—"
        unrealized = f"{risk['unrealized']:.3f}" if risk["unrealized"] is not None else "—"
        equity = f"{risk['equity']:.3f}" if risk["equity"] is not None else "—"
        lines.append(
            f"| {account} | {metric['count']} | {metric['net']:.3f} | {win} | {pf} | "
            f"{hold} | {metric['fees']:.3f} | {drawdown} | {unrealized} | {equity} |"
        )
    lines += ["", "## 每组 BTC / ETH 与风险事件", ""]
    for account in "ABC":
        trades, risk = data[account]
        lines += [f"### {account} 组", "", "| 交易对 | 笔数 | 净额 USDT |", "|---|---:|---:|"]
        for pair in ("BTC/USDT", "ETH/USDT"):
            subset = [trade for trade in trades if trade["pair"] == pair]
            lines.append(f"| {pair} | {len(subset)} | {sum(t['profit_abs'] for t in subset):.3f} |")
        reasons = Counter(trade["exit_reason"] for trade in trades)
        exits = "、".join(f"{key} {count} 笔" for key, count in sorted(reasons.items())) or "本周无平仓"
        lines += ["", f"出场原因：{exits}。",
                  f"风险停机次数：{sum(risk['counts'].values())}；"
                  f"累计暂停/停机 {sum(risk['hours'].values()):.1f} 小时；"
                  f"数据故障 {risk['faults']} 次；入场回调可见的被锁候选信号 {risk['blocked']} 个（仅下界）。", ""]
    lines += [
        f"统一风险参数哈希：`{next(iter(policy_hashes)) if len(policy_hashes)==1 else '不一致，报告无效'}`。",
        "", "**样本不足：**单周交易数很少，胜率和 Profit Factor 波动很大；不得宣称任何组已获成功验证。",
        "权益和未实现损益为 1h 收盘采样估计，不代表真实成交；手续费为逐笔名义金额估算，滑点未由 OHLCV 还原。",
        "Freqtrade 原生保护可能在仓位回调前跳过候选信号，因此被锁候选计数不能解释为全部原始指标候选；完整数量需离线重建信号。",
        "",
    ]
    if len(policy_hashes) != 1:
        raise ValueError("A/B/C 使用了不同风控参数哈希")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--week-start-utc", required=True)
    for account in "abc":
        parser.add_argument(f"--{account}-zip", type=Path, required=True)
        parser.add_argument(f"--{account}-risk-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    start = datetime.fromisoformat(args.week_start_utc.replace("Z", "+00:00"))
    if start.tzinfo is None or start.utcoffset() is None or start.astimezone(UTC).weekday() != 0:
        raise SystemExit("周报起点必须为 UTC 周一 00:00")
    start = start.astimezone(UTC)
    if start.hour or start.minute or start.second:
        raise SystemExit("周报起点必须为 UTC 周一 00:00")
    end = start + timedelta(days=7)
    data = {}
    for account in "ABC":
        zip_path = getattr(args, f"{account.lower()}_zip")
        db_path = getattr(args, f"{account.lower()}_risk_db")
        result = read_backtest(zip_path, f"HoshiSpotV11{account}")
        trades = [trade for trade in result["trades"]
                  if start <= datetime.fromisoformat(trade["close_date"]) < end]
        data[account] = (trades, inspect_risk(db_path, start, end))
    report = render(start, end, data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
