"""Acceptance-only execution fuse: no real orders, paper orders, fills or positions."""

from __future__ import annotations

from functools import wraps


class NoTradeBlocked(RuntimeError):
    pass


BOT_METHODS = (
    "create_trade", "execute_entry", "execute_trade_exit", "exit_positions",
    "emergency_exit", "_check_and_execute_exit", "check_and_call_adjust_trade_position",
    "handle_replace_order", "create_stoploss_order", "handle_trade",
    "process_open_trade_positions", "manage_open_orders", "manage_trade_stoploss_orders",
    "_update_trade_after_fill", "update_trade_state", "handle_order_fee",
    "handle_onexchange_order", "order_close_notify", "startup_update_open_orders",
    "replace_order", "handle_cancel_order", "cancel_open_orders_of_trade",
    "cancel_all_open_orders",
)
EXCHANGE_METHODS = (
    "create_order", "create_dry_run_order", "check_dry_limit_order_filled",
    "fetch_dry_run_order", "cancel_order", "cancel_stoploss_order",
    "cancel_order_with_result", "cancel_stoploss_order_with_result",
    "add_dry_order_fee",
)


def _deny(name: str, original):
    @wraps(original)
    def blocked(*_args, **_kwargs):
        raise NoTradeBlocked(f"V1.1 无交易验收禁止执行：{name}")

    blocked.__hoshi_no_trade__ = True
    return blocked


def install() -> None:
    """Patch both orchestration and exchange boundaries before importing Freqtrade main."""
    from freqtrade.freqtradebot import FreqtradeBot
    from freqtrade.exchange.exchange import Exchange

    targets = [(FreqtradeBot, name) for name in BOT_METHODS] + [
        (Exchange, name) for name in EXCHANGE_METHODS
    ]
    missing = [f"{cls.__name__}.{name}" for cls, name in targets
               if not callable(getattr(cls, name, None))]
    if missing:
        raise RuntimeError(f"Freqtrade 无交易保护无法安装：{', '.join(missing)}")
    for cls, name in targets:
        current = getattr(cls, name)
        if not getattr(current, "__hoshi_no_trade__", False):
            setattr(cls, name, _deny(f"{cls.__name__}.{name}", current))
    if not is_installed():
        raise RuntimeError("Freqtrade 无交易保护自检失败")


def is_installed() -> bool:
    from freqtrade.freqtradebot import FreqtradeBot
    from freqtrade.exchange.exchange import Exchange

    return all(getattr(getattr(cls, name, None), "__hoshi_no_trade__", False)
               for cls, names in ((FreqtradeBot, BOT_METHODS), (Exchange, EXCHANGE_METHODS))
               for name in names)
