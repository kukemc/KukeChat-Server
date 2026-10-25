#!/usr/bin/env python
"""2.0.10 商店改价：最早那 15 件付费商品改成买一次 31 天、价格跟着降（docs/shop.md 2.4）。

默认商品只在 sku 不存在时插入，线上已有的这 15 件不会跟着 DEFAULT_ITEMS 变，所以用这个脚本改一次。
只改 shop_items 的 price 和 duration_days，不碰任何拥有记录：已经买了永久的人照样永久（docs/shop.md 2.5）。
不存在的 sku 跳过；已经是目标值的不动。

在后端目录里、用生产配置跑（.env 在当前目录）：

    python scripts/shop_reprice_2_0_10.py            # 只预览：打印「改前 → 改后」
    python scripts/shop_reprice_2_0_10.py --apply    # 真的写库

发布脚本只上传 app/，服务器上没有 scripts/ 时把这个文件单独传到后端目录的 scripts/ 下再跑。
脚本进程里没有在线连接，发不了 shop.changed；客户端下次打开商店时会拿到新价格。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.database import AsyncSessionLocal, engine  # noqa: E402
from app.models.shop import ShopItem  # noqa: E402

DAYS = 31
# sku -> 新价格
PRICES: dict[str, int] = {
    "bubble.mint": 8,
    "bubble.peach": 18,
    "bubble.ocean": 30,
    "bubble.pixel": 35,
    "bubble.galaxy": 50,
    "bubble.aurora": 88,
    "frame.mint": 8,
    "frame.sakura": 18,
    "frame.ocean": 30,
    "frame.neko": 35,
    "frame.galaxy": 50,
    "frame.aurora": 88,
    "frame.bow": 20,
    "frame.devil": 40,
    "frame.angel": 66,
}


def _days(value: int | None) -> str:
    return "永久" if value is None else f"{value} 天"


async def run(apply: bool) -> int:
    """返回改了（或预览里要改）几件。"""
    async with AsyncSessionLocal() as db:
        rows = {item.sku: item for item in (await db.execute(select(ShopItem).where(ShopItem.sku.in_(PRICES)))).scalars()}
        changed = 0
        for sku, price in PRICES.items():
            item = rows.get(sku)
            if item is None:
                print(f"  {sku}: 不存在，跳过")
                continue
            if item.price == price and item.duration_days == DAYS:
                print(f"  {sku} {item.name}: 已经是 {price} 币 / {DAYS} 天")
                continue
            print(f"  {sku} {item.name}: {item.price} 币 / {_days(item.duration_days)} → {price} 币 / {DAYS} 天")
            item.price, item.duration_days = price, DAYS
            changed += 1
        if apply and changed:
            await db.commit()
        else:
            await db.rollback()
    await engine.dispose()
    return changed


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="2.0.10 商店改价：15 件付费商品改成 31 天、新价格。默认只预览，加 --apply 才写库。")
    parser.add_argument("--apply", action="store_true", help="真的写库")
    args = parser.parse_args(argv)
    changed = asyncio.run(run(args.apply))
    if args.apply:
        print(f"已改 {changed} 件。客户端下次打开商店时拿到新价格。")
    else:
        print(f"预览：要改 {changed} 件。确认无误后加 --apply 再跑一次。")


if __name__ == "__main__":
    main()
