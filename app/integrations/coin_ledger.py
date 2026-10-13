"""金币账本：商店的余额从哪儿来。这里只有接口，没有实现。

KukeChat 自己不发币也不存币。商店（app/services/shop.py）只问账本一件事：「这个外部账号累计入账了多少币」，
余额 = 累计入账 − 在商店里花掉的。入账记在哪、怎么查，由部署方提供的实现决定，见 README 的「集成点」一节。
没有实现时用 NullCoinLedger：configured() 为 False，商店能逛、能领等级解锁的商品，不能花币买。

约定：

- 「账号标识」是余额来源那边的账号 id。商店传进来的是用户绑定的外部账号标识，不是 KukeChat 的用户 id。
- 数额都是非负整数。名单（dict）的键是规范化后的账号标识（小写、去掉首尾空白）。
- 核实不了（没配置、网络不通、对方返回的内容看不懂、名单没取全）一律抛 CoinsUnavailable，
  调用方按「现在不知道」处理 —— 绝不能用 0 或残缺的名单冒充「这个账号没有入账」。
- 实现要自己负责不阻塞事件循环，自己决定要不要缓存、要不要合并同时发生的查询。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class CoinsUnavailable(Exception):
    """现在没法向余额来源核实入账数。"""


@dataclass(frozen=True)
class LedgerSource:
    """余额来源是哪一个，只给后台展示用，业务逻辑不依赖它。"""

    # 余额来源的标识；没配置时为 None
    id: str | None = None
    # 这个标识取自哪个配置项；没配置时为 None
    setting: str | None = None


class CoinLedger(ABC):
    """余额来源。"""

    @abstractmethod
    def configured(self) -> bool:
        """现在能不能核实余额。为 False 时商店不向账本发任何查询。"""

    @abstractmethod
    def source(self) -> LedgerSource:
        """余额来源的说明（后台「商店设置」里展示）。"""

    @abstractmethod
    def topup_url(self) -> str | None:
        """用户去充值的网页地址；没有就返回 None。"""

    @abstractmethod
    async def credited_to(self, account_id: str) -> int:
        """这个账号累计入账了多少币。确实没有入账返回 0，核实不了抛 CoinsUnavailable。"""

    @abstractmethod
    async def recent_credits(self, limit: int | None = None) -> dict[str, int]:
        """最近入账的前 limit 个账号：{账号标识: 累计入账}。limit 为 None 时由实现决定取多少。"""

    @abstractmethod
    async def all_credits(self, *, fresh: bool = False) -> dict[str, int]:
        """全部有入账的账号：{账号标识: 累计入账}。fresh=True 时不用缓存，重新取一遍。

        取不全就抛 CoinsUnavailable，不返回残缺的名单。
        """

    def reset_cache(self) -> None:
        """清掉实现自己的缓存（测试用；换了余额来源之后也可以调）。没有缓存的实现不用管。"""


class NullCoinLedger(CoinLedger):
    """没有接入余额来源时的占位实现。"""

    def configured(self) -> bool:
        return False

    def source(self) -> LedgerSource:
        return LedgerSource()

    def topup_url(self) -> str | None:
        return None

    async def credited_to(self, account_id: str) -> int:
        raise CoinsUnavailable("本服务没有接入余额来源")

    async def recent_credits(self, limit: int | None = None) -> dict[str, int]:
        raise CoinsUnavailable("本服务没有接入余额来源")

    async def all_credits(self, *, fresh: bool = False) -> dict[str, int]:
        raise CoinsUnavailable("本服务没有接入余额来源")
