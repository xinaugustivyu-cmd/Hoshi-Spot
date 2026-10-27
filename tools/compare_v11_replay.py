"""从原始 Freqtrade ZIP 和 V1.1 风险账本生成只读中文对照报告。"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def load_backtest(path: Path, strategy: str) -> dict:
    with zipfile.ZipFile(path) as archive:
        source = next(name for name in archive.namelist()
                      if name.endswith(".json") and not name.endswith("_config.json"))
        result = json.loads(archive.read(source))
    return result["strategy"][strategy]


def pair_stats(trades: list[dict]) -> dict[str, tuple[int, float]]:
    by_pair = defaultdict(list)
    for trade in trades:
        by_pair[trade["pair"]].append(trade)
    return {pair: (len(rows), sum(row["profit_abs"] for row in rows))
            for pair, rows in by_pair.items()}


def audit_state(db_path: Path) -> dict:
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as connection:
        transitions = connection.execute(
            "SELECT observed_at, state, rule, details_json FROM risk_events "
            "WHERE state IN ('COOLDOWN','DATA_PAUSE','WEEKLY_PAUSE',"
            "'HALTED_MANUAL_REVIEW','EMERGENCY_HALT') "
            "AND rule NOT IN ('blocked_candidate','trade_closed') ORDER BY id"
        ).fetchall()
        marks = connection.execute(
            "SELECT MIN(equity), MAX(equity), MAX(stage_drawdown), COUNT(*) FROM equity_marks"
        ).fetchone()
        blocked = connection.execute(
            "SELECT COUNT(*) FROM risk_events WHERE rule='blocked_candidate'"
        ).fetchone()[0]
        ledger = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(net_profit_usdt),0) FROM closed_trades"
        ).fetchone()
        policy_hash = json.loads(connection.execute(
            "SELECT value FROM meta WHERE key='policy_hash'"
        ).fetchone()[0])
    return {"transitions": transitions, "marks": marks, "blocked": blocked,
            "ledger": ledger, "policy_hash": policy_hash}


def build_report(original: dict, candidate: dict, audit: dict,
                 original_path: Path, candidate_path: Path) -> str:
    old = original["trades"]
    new = candidate["trades"]
    old_entries = {(row["pair"], row["open_date"]) for row in old}
    new_entries = {(row["pair"], row["open_date"]) for row in new}
    old_end = max((row["close_date"] for row in old), default="无")
    new_end = max((row["close_date"] for row in new), default="无")
    manual = next(((at, rule, json.loads(details)) for at, state, rule, details
                   in audit["transitions"] if state == "HALTED_MANUAL_REVIEW"), None)
    period_end = datetime.fromisoformat(candidate["backtest_end"])
    if period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=timezone.utc)
    lock_days = ((period_end - datetime.fromisoformat(manual[0])).total_seconds() / 86400
                 if manual else 0)
    lines = [
        "# Hoshi V1 与统一 V1.1 风控 A 组离线回放对照",
        "",
        "本报告只比较相同历史区间、同一公开行情、80 USDT、每边 0.1% 手续费及 V1 原始信号。A 组仅更换风控；不同交易路径的收益差异不能归因于策略改进。所有时间均为 UTC。",
        "",
        f"原始 ZIP：`{original_path.name}`；候选 ZIP：`{candidate_path.name}`。",
        f"新风控参数哈希：`{audit['policy_hash']}`。",
        "",
        "| 指标 | 冻结 V1 | A 组统一新风控 |",
        "|---|---:|---:|",
        f"| 已平仓交易 | {len(old)} | {len(new)} |",
        f"| 净收益 USDT | {sum(row['profit_abs'] for row in old):.6f} | {sum(row['profit_abs'] for row in new):.6f} |",
        f"| Freqtrade 已平仓最大回撤 | {original.get('max_drawdown_account', 0)*100:.3f}% | {candidate.get('max_drawdown_account', 0)*100:.3f}% |",
        f"| 最后平仓 | {old_end} | {new_end} |",
        f"| 共同入场时间 | {len(old_entries & new_entries)} | {len(old_entries & new_entries)} |",
        f"| 风控账本最高采样回撤 | — | {audit['marks'][2]*100:.3f}% |",
        f"| 入场回调可见的被锁候选（下界） | — | {audit['blocked']} |",
        "",
        "## 交易对",
        "",
        "| 交易对 | V1 笔数 | V1 净额 | A 笔数 | A 净额 |",
        "|---|---:|---:|---:|---:|",
    ]
    old_pairs = pair_stats(old)
    new_pairs = pair_stats(new)
    for pair in ("BTC/USDT", "ETH/USDT"):
        oc, op = old_pairs.get(pair, (0, 0.0))
        nc, np = new_pairs.get(pair, (0, 0.0))
        lines.append(f"| {pair} | {oc} | {op:.6f} | {nc} | {np:.6f} |")
    lines += [
        "", "## 风险状态与解释", "",
        f"账本交易数 {audit['ledger'][0]}，累计净额 {audit['ledger'][1]:.6f} USDT；"
        "应与 A 组 ZIP 中逐笔结果一致。",
    ]
    if manual:
        lines += [
            f"首次人工停机：**{manual[0]}**，规则 `{manual[1]}`；至回测结束约 **{lock_days:.3f} 天**。",
            "人工停机后不会因滚动窗口过期而自动恢复，已有仓位仍按 V1 退出。",
        ]
    if "indicator_candidates" in audit:
        lines += [
            f"独立重建的指标层候选：人工停机后、最后持仓平仓后至区间结束共 "
            f"**{audit['indicator_candidates']} 个**。逐项及共享行情事件 ID 见候选 CSV。"
            "该数不等于可执行订单数；唯一持仓、最小金额及其他入口检查可能继续拒绝。",
        ]
    else:
        lines.append("本区间未触发人工停机。")
    lines += [
        "", "完整风险状态事件、权益采样与周初快照保存在独立 SQLite；原始 V1 ZIP 与数据未修改。",
        "历史回放按 1h 收盘估值，不能重建 K 线内峰谷或真实滑点。",
        "Freqtrade 原生保护可能在 `custom_stake_amount` 前跳过候选，故回调可见计数为下界；不能据此断言锁定期没有信号。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--risk-db", type=Path, required=True)
    parser.add_argument("--candidate-audit", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = load_backtest(args.original, "HoshiSpotV1")
    candidate = load_backtest(args.candidate, "HoshiSpotV11A")
    audit = audit_state(args.risk_db)
    if args.candidate_audit:
        with args.candidate_audit.open(encoding="utf-8-sig", newline="") as stream:
            audit["indicator_candidates"] = sum(1 for _ in csv.DictReader(stream))
    expected_net = sum(row["profit_abs"] for row in candidate["trades"])
    if audit["ledger"][0] != len(candidate["trades"]) or abs(audit["ledger"][1] - expected_net) > 1e-5:
        raise SystemExit("A 组风险账本与 Freqtrade 逐笔结果不一致，禁止生成合格报告")
    report = build_report(original, candidate, audit, args.original, args.candidate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
