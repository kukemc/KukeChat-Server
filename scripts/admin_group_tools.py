#!/usr/bin/env python
"""群 / 金币的后台运维命令（实现在 app/cli/admin_group_tools.py，说明见那里和 docs/group-plus.md 9.3）。

在后端目录里、用生产配置跑（.env 在当前目录）：

    python scripts/admin_group_tools.py --find "官方"
    python scripts/admin_group_tools.py --admin <管理员用户名> --permanent-max <群 id> [--apply]
    python scripts/admin_group_tools.py --admin <管理员用户名> --unlimited <群 id> [--apply]
    python scripts/admin_group_tools.py --admin <管理员用户名> --grant-coins <用户 id 或用户名> <金额> --reason "原因" [--apply]

默认只预览，加 --apply 才写库。发布脚本只上传 app/，服务器上没有 scripts/ 时直接用 python -m app.cli.admin_group_tools，参数一样。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cli.admin_group_tools import main  # noqa: E402

if __name__ == "__main__":
    asyncio.run(main())
