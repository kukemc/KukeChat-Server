"""集成点：KukeChat 依赖的、实现不在本仓库里的两个外部能力。

- ccw_gateway：从 CCW 社区取评论、学生资料、作品详情（账号绑定、资料同步、作品链接卡片用）。
- coin_ledger：某个外部账号累计入账了多少金币（商店余额用）。

本仓库只定义接口和占位实现；真正的实现放在私有包 `app._private` 里，由 loader 在启动时挑选。
自己部署时怎么提供实现、不提供时哪些功能不可用，见 README 的「集成点」一节。
"""

from app.integrations.ccw_gateway import CcwGateway, CcwUnavailable, NullCcwGateway, StudentPayloads
from app.integrations.coin_ledger import CoinLedger, CoinsUnavailable, LedgerSource, NullCoinLedger
from app.integrations.loader import (
    PRIVATE_PACKAGE,
    describe,
    get_ccw_gateway,
    get_coin_ledger,
    load,
    private_package_present,
    set_ccw_gateway,
    set_coin_ledger,
    use_ccw_gateway,
    use_coin_ledger,
)

__all__ = [
    "PRIVATE_PACKAGE",
    "CcwGateway",
    "CcwUnavailable",
    "CoinLedger",
    "CoinsUnavailable",
    "LedgerSource",
    "NullCcwGateway",
    "NullCoinLedger",
    "StudentPayloads",
    "describe",
    "get_ccw_gateway",
    "get_coin_ledger",
    "load",
    "private_package_present",
    "set_ccw_gateway",
    "set_coin_ledger",
    "use_ccw_gateway",
    "use_coin_ledger",
]
