"""V1.1 唯一允许的 Freqtrade 交易进程入口；先封锁真实订单边界。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .order_guard import install, is_installed
from .safe_launcher import UnsafeConfiguration, validate_configuration


def validate_trade_argv(args: list[str], environment: dict[str, str]) -> None:
    """仅接受启动器生成的精确 trade 命令，拒绝额外 CLI 覆盖项。"""
    if (len(args) != 6 or args[0:2] != ["trade", "--config"]
            or args[3] != "--strategy" or args[5] != "--dry-run"
            or args[4] not in {"HoshiSpotV11A", "HoshiSpotV11B", "HoshiSpotV11C"}):
        raise UnsafeConfiguration("V1.1 交易进程参数未通过安全入口检查")
    config = json.loads(Path(args[2]).read_text(encoding="utf-8"))
    validate_configuration(config, environment, args[4][-1])


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "trade":
        validate_trade_argv(args, dict(os.environ))
    elif not args or args[0] != "backtesting":
        raise UnsafeConfiguration("V1.1 订单隔离入口只允许受控 trade 或离线 backtesting")
    install()
    if not is_installed():
        raise RuntimeError("订单边界防护未安装")
    from freqtrade.main import main as freqtrade_main

    freqtrade_main(args)


if __name__ == "__main__":
    main()
