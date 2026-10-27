"""V1.1 容器入口安全检查。只允许固定的模拟配置和非交易所密钥环境变量。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping


ALLOWED_FREQTRADE_ENV = {
    "FREQTRADE__DRY_RUN",
    "FREQTRADE__API_SERVER__USERNAME",
    "FREQTRADE__API_SERVER__PASSWORD",
    "FREQTRADE__API_SERVER__JWT_SECRET_KEY",
}
DEFAULT_PASSWORDS = {"", "password", "changeme", "replace-with-a-unique-local-password"}


class UnsafeConfiguration(ValueError):
    pass


def validate_configuration(config: Mapping, environment: Mapping[str, str], account_id: str) -> None:
    """读取本地模拟设置；不读取或记录任何交易所密钥的值。"""
    if account_id not in {"A", "B", "C"}:
        raise UnsafeConfiguration("实验账户身份无效")
    expected_strategy = f"HoshiSpotV11{account_id}"
    exchange = config.get("exchange", {})
    if (config.get("dry_run") is not True or config.get("trading_mode") != "spot"
            or config.get("margin_mode") or config.get("max_open_trades") != 1
            or config.get("dry_run_wallet") != 80
            or config.get("stake_currency") != "USDT"
            or config.get("timeframe") != "1h"
            or config.get("stoploss") != -0.04
            or config.get("minimal_roi") != {"0": 0.06}
            or config.get("leverage", 1) != 1
            or config.get("can_short", False) is not False
            or config.get("strategy") != expected_strategy
            or exchange.get("name") != "binance"
            or exchange.get("key") or exchange.get("secret")
            or exchange.get("ccxt_config", {}) != {}
            or exchange.get("ccxt_async_config", {}) != {}
            or exchange.get("pair_whitelist") != ["BTC/USDT", "ETH/USDT"]):
        raise UnsafeConfiguration("仅允许无密钥 Binance 现货 80 USDT Dry Run")
    params = config.get("strategy_parameters", {})
    if any(params.get(key) != value for key, value in {
        "max_position_fraction": 0.2, "max_loss_fraction": 0.01,
        "stoploss_fraction": 0.04, "weekly_loss_limit": 0.03,
        "stage_loss_limit_fraction": 0.05,
    }.items()):
        raise UnsafeConfiguration("风险参数与统一 V1.1 规则不一致")
    if any(key.upper().endswith(("API_KEY", "API_SECRET", "EXCHANGE__KEY", "EXCHANGE__SECRET"))
           for key in environment):
        raise UnsafeConfiguration("环境中存在交易所密钥变量名，拒绝启动")
    overrides = {key for key in environment if key.startswith("FREQTRADE__")}
    if overrides - ALLOWED_FREQTRADE_ENV:
        raise UnsafeConfiguration("存在未经允许的 Freqtrade 环境覆盖项")
    if environment.get("FREQTRADE__DRY_RUN", "true").lower() != "true":
        raise UnsafeConfiguration("Dry Run 环境变量必须为 true")
    api = config.get("api_server", {})
    if api.get("enabled") is not True:
        raise UnsafeConfiguration("V1.1 本地 WebUI 配置缺失")
    username = environment.get("FREQTRADE__API_SERVER__USERNAME", "")
    password = environment.get("FREQTRADE__API_SERVER__PASSWORD", "")
    jwt = environment.get("FREQTRADE__API_SERVER__JWT_SECRET_KEY", "")
    if not username or password.lower() in DEFAULT_PASSWORDS or len(password) < 16 or len(jwt) < 32:
        raise UnsafeConfiguration("WebUI 用户名、强密码或 JWT secret 未配置")


def prepare_environment(environment: Mapping[str, str]) -> dict[str, str]:
    cleaned = {key: value for key, value in environment.items()
               if not key.startswith("FREQTRADE__") and not key.upper().endswith(
                   ("API_KEY", "API_SECRET", "EXCHANGE__KEY", "EXCHANGE__SECRET"))}
    for key in ALLOWED_FREQTRADE_ENV - {"FREQTRADE__DRY_RUN"}:
        if key in environment:
            cleaned[key] = environment[key]
    cleaned["FREQTRADE__DRY_RUN"] = "true"
    return cleaned


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    check_only = len(args) == 3 and args[0] == "--check"
    if check_only:
        args = args[1:]
    if len(args) != 2 or args[0] not in {"A", "B", "C"}:
        raise SystemExit("用法：python -m hoshi_v11.safe_launcher [--check] <A|B|C> <config.json>")
    account_id, config_path = args
    path = Path(config_path).resolve(strict=True)
    config = json.loads(path.read_text(encoding="utf-8"))
    validate_configuration(config, os.environ, account_id)
    if check_only:
        return 0
    command = [
        "freqtrade", "trade", "--config", str(path), "--strategy",
        f"HoshiSpotV11{account_id}", "--dry-run",
    ]
    return subprocess.call(command, env=prepare_environment(os.environ))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (UnsafeConfiguration, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"V1.1 安全启动检查失败：{exc}", file=sys.stderr)
        raise SystemExit(2)
