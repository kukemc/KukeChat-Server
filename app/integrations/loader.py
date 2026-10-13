"""挑选集成点的实现：有私有包就用它的，没有就用占位实现。

私有包是 `app._private`（不在本仓库里，见 README 的「集成点」一节）。它要提供两个不带参数的工厂函数：

    def create_ccw_gateway() -> CcwGateway: ...
    def create_coin_ledger() -> CoinLedger: ...

判断「有没有」只看这个包找不找得到，不 import 它；找得到就照常 import，包自己有问题（语法错、缺工厂函数、
返回的东西不是接口的实现）会原样抛出来 —— app.main 在导入时调 load()，所以这类问题在启动时就暴露，
而不是悄悄退回占位实现、让绑定和商店在线上无声失效。

测试里用 use_ccw_gateway / use_coin_ledger（或 set_*）换成假实现；换上去的实现优先于 load() 选出来的。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.integrations.ccw_gateway import CcwGateway, NullCcwGateway
from app.integrations.coin_ledger import CoinLedger, NullCoinLedger

logger = logging.getLogger(__name__)

PRIVATE_PACKAGE = "app._private"

_lock = threading.Lock()
# load() 选出来的默认实现：(网关, 账本, 是否来自私有包)；还没选过是 None
_defaults: tuple[CcwGateway, CoinLedger, bool] | None = None
# 手动换上去的实现，优先于默认的
_gateway_override: CcwGateway | None = None
_ledger_override: CoinLedger | None = None


def private_package_present() -> bool:
    """私有包在不在。只找，不 import。"""
    return importlib.util.find_spec(PRIVATE_PACKAGE) is not None


def _build(module: Any, factory_name: str, interface: type) -> Any:
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise ImportError(f"{PRIVATE_PACKAGE} 没有提供 {factory_name}()")
    instance = factory()
    if not isinstance(instance, interface):
        raise TypeError(f"{PRIVATE_PACKAGE}.{factory_name}() 返回的不是 {interface.__name__}：{type(instance)!r}")
    return instance


def load() -> None:
    """选出默认实现。可以重复调用，只有第一次真的干活。"""
    global _defaults
    if _defaults is not None:
        return
    with _lock:
        if _defaults is not None:
            return
        if private_package_present():
            module = importlib.import_module(PRIVATE_PACKAGE)
            selected: tuple[CcwGateway, CoinLedger, bool] = (
                _build(module, "create_ccw_gateway", CcwGateway),
                _build(module, "create_coin_ledger", CoinLedger),
                True,
            )
        else:
            selected = (NullCcwGateway(), NullCoinLedger(), False)
        _defaults = selected
    logger.info(
        "integrations loaded: private=%s ccw_gateway=%s coin_ledger=%s",
        selected[2],
        _name(selected[0]),
        _name(selected[1]),
    )


def _loaded() -> tuple[CcwGateway, CoinLedger, bool]:
    load()
    assert _defaults is not None
    return _defaults


def get_ccw_gateway() -> CcwGateway:
    return _gateway_override if _gateway_override is not None else _loaded()[0]


def get_coin_ledger() -> CoinLedger:
    return _ledger_override if _ledger_override is not None else _loaded()[1]


def set_ccw_gateway(gateway: CcwGateway | None) -> None:
    """换上一个网关实现；传 None 换回默认的。"""
    global _gateway_override
    _gateway_override = gateway


def set_coin_ledger(ledger: CoinLedger | None) -> None:
    """换上一个账本实现；传 None 换回默认的。"""
    global _ledger_override
    _ledger_override = ledger


@contextmanager
def use_ccw_gateway(gateway: CcwGateway) -> Iterator[CcwGateway]:
    """with 块里用这个网关，出来后恢复成原来的。"""
    global _gateway_override
    previous = _gateway_override
    _gateway_override = gateway
    try:
        yield gateway
    finally:
        _gateway_override = previous


@contextmanager
def use_coin_ledger(ledger: CoinLedger) -> Iterator[CoinLedger]:
    """with 块里用这个账本，出来后恢复成原来的。"""
    global _ledger_override
    previous = _ledger_override
    _ledger_override = ledger
    try:
        yield ledger
    finally:
        _ledger_override = previous


def _name(instance: object) -> str:
    cls = type(instance)
    return f"{cls.__module__}.{cls.__qualname__}"


def describe() -> dict[str, Any]:
    """现在用的是哪两个实现（排查和启动日志用）。private：默认实现是不是来自私有包。"""
    return {
        "private": _loaded()[2],
        "ccw_gateway": _name(get_ccw_gateway()),
        "coin_ledger": _name(get_coin_ledger()),
    }
