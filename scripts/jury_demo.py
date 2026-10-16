#!/usr/bin/env python
"""风纪系统 v2 的本地联调实例：一条命令起一个带假数据的后端，给客户端做端到端走查。

    python scripts/jury_demo.py              # 没有库就建库灌数据，然后起服务
    python scripts/jury_demo.py --reset      # 删库重建
    python scripts/jury_demo.py --seed-only  # 只灌数据，不起服务
    python scripts/jury_demo.py --port 8791
    python scripts/jury_demo.py --db-dir kukechat-jury-demo-test --port 8793   # 另起一套互不影响的实例

数据库和上传目录都在系统临时目录的 kukechat-jury-demo/ 下（--db-dir 可以换一个目录，仍然必须在临时目录里）；脚本会先把所有配置
指到那里、清掉外部环境带进来的同名变量，再 import app.*，所以不会连到任何真实
数据库或外部服务。说明见 docs/jury-demo.md。

假数据按期分函数（seed_base、seed_phase1、seed_phase2、seed_phase3、seed_red_packets、seed_gifts）。以后再加数据往 PHASES 里追加
新函数即可：已经灌过的期不会重跑。

红包（docs/red-packets.md）也顺手开着：演示实例没有真的余额来源，这里注入一个只存在于本脚本里的假账本（DemoCoinLedger），
绑定了 CCW 的演示账号各有 1000 金币。礼物（docs/gifts.md）同样开着，另外有一个今天过生日的账号、一件快到期的限时头像框。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import shutil
import socket
import sys
import tempfile
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
DEMO_DIR = TEMP_ROOT / "kukechat-jury-demo"
DB_PATH = DEMO_DIR / "jury-demo.sqlite3"
UPLOAD_DIR = DEMO_DIR / "uploads"


def use_demo_dir(path: Path) -> None:
    """换一个目录放演示数据（--db-dir）。必须在 import app.* 之前调用。"""
    global DEMO_DIR, DB_PATH, UPLOAD_DIR
    DEMO_DIR = path
    DB_PATH = DEMO_DIR / "jury-demo.sqlite3"
    UPLOAD_DIR = DEMO_DIR / "uploads"


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
# 所有演示账号（包括批量建的群成员 m001–m300）都是这个密码。
PASSWORD = "demo123456"
# 只用于这个演示库：固定值，方便重启后旧令牌继续有效。绝不能用在别处。
DEMO_JWT_SECRET = "kukechat-jury-demo-only-fixed-secret-do-not-reuse-0123456789"

# 用户名 / 昵称 / 角色或层级 / 适合用来看什么
ACCOUNTS: list[tuple[str, str, str, str]] = [
    ("admin", "演示管理员", "管理员", "后台 /admin/*：案件队列（含合议队列）、委员名单、题库、考试记录、观点管理、开关、申诉、复审监控、判例、议事厅"),
    ("juror", "阿澄", "在任 · 正式委员（主账号）", "评审台、投后详情、观点区、我的评审；风纪值、成就、排行榜；离资深还差两项；练习场做了一部分；议事厅里发过帖、投过提案；不是资深，取合议案件是 403"),
    ("trainee1", "见习小林", "在任 · 见习委员", "晋升正式的四项条件都已满足：他投过的案件再有一个定案，就会升为正式委员"),
    ("trainee2", "见习小鹿", "在任 · 见习委员", "是「像素画社」成员：那个群的案件对他回避；考了两次才通过；主动回避过两次"),
    ("regular2", "正式阿木", "在任 · 正式委员", "由系统自动从见习晋升（通知中心有一条）；公开了委员身份；是 offender 的好友"),
    ("senior1", "资深老周", "在任 · 资深委员", "资深合议（4 件可取）、申诉复审和举报人复议（3 件可取）；标过判例，也有可以标成判例的案件；议事厅里发过提案和讨论帖；公开了委员身份"),
    ("senior2", "资深青禾", "在任 · 资深委员", "合议里已经投过一票的那件不会再分给他（3 件可取）；复审 3 件可取；公开了委员身份"),
    ("senior3", "资深南风", "在任 · 资深委员", "/jury/me.warnings 里的 demote_risk：最近的一致率接近降级线；合议 4 件可取，复审里已经投过一件（2 件可取）"),
    ("hesitant", "犹豫的小周", "在任 · 见习委员", "/jury/me.warnings 里的 high_abstain：「拿不准」投得偏多"),
    ("cooldown", "歇一歇的阿信", "在任 · 见习委员（弃权冷却中）", "/jury/me.cooldown_until；取案是 429 jury_cooldown"),
    ("suspended", "被暂停的小北", "被系统暂停的委员", "/jury/me 的 suspended + review_note；通知中心有一条；仍可回看投过的案件"),
    ("mutedjuror", "被禁言的阿泽", "在任 · 见习委员（禁言中）", "/jury/me.restriction；评审和发观点被拒（juror_restricted）；练习场不受影响"),
    ("expired", "任期已满的老陈", "任期届满的委员", "/jury/me 的 expired：有违规记录所以没有自动续任，通知中心有一条"),
    ("eligible", "够格的小满", "普通用户 · 满足全部条件，没考过", "入门考试（可以直接开考）、申请入口、练习场、总览和公示栏的非委员视角"),
    ("eligible2", "够格的小雪", "普通用户 · 满足全部条件，没考过", "第二个可以直接开考的账号（自测脚本用它演示「没通过」）"),
    ("examfail", "没考过的小鲁", "普通用户 · 考试没通过，冷却中", "GET /jury/exam 的 last_result / next_attempt_at；开考是 429 exam_cooldown"),
    ("exampass", "考过了的小顾", "普通用户 · 考试通过，还没申请", "exam.passed=true；申请后带成绩进审批队列；练习场做完了一整轮"),
    ("examout", "考了三次的小范", "普通用户 · 考试次数用尽", "开考是 429 exam_attempts_exhausted"),
    ("newbie", "新来的豆豆", "普通用户 · 不满足条件", "/jury/me.requirements 里未达标的样子；考试和练习都是 403 jury_not_eligible"),
    ("applicant", "待审批的阿宁", "普通用户 · 已申请待审批", "/jury/me 的 applied；后台审批队列里带着考试成绩"),
    ("reporter1", "举报人小艾", "举报人", "「我的举报」里各种处理结果；一条可以申请复议（can_reconsider），一条复议转给了管理员、还没结果"),
    ("reporter2", "举报人小贝", "举报人", "同上；一条复议正在资深复审里（reconsider_status = pending）"),
    ("reporter3", "举报人小柯", "举报人", "同上；举报过一条私聊消息（只进管理员队列）；一条复议已维持原结论（upheld）"),
    ("reporter4", "举报人小丁", "举报人", "同上；一条复议已改判为违规（overturned）"),
    ("offender", "被举报的小黑", "被处罚的用户", "/moderation/me：一条可以申诉的处罚（原审里有资深委员，凑不齐三位，申诉会直接到管理员）"),
    ("appellant", "申诉中的小白", "被处罚的用户", "/moderation/me：一条申诉待处理的处罚（开关打开之前提交的，在管理员手里；对应案件暂不公示）"),
    ("appealing", "申诉中的小何", "被处罚的用户（禁言中）", "/moderation/me：四条申诉都还没结果：两条在资深复审里（委员定案的、机器处罚的各一条），一条复审超时转了管理员，一条私聊相关的直接到管理员"),
    ("punished", "被处罚的小乔", "被处罚的用户", "/moderation/me：三条申诉已由资深复审出了结果（维持 / 减轻 / 撤销各一条），另有一条还没申诉的"),
    ("dayfirst", "今天还没投的小钟", "在任 · 见习委员", "今天（UTC+8）还没投过票：投今天的第一张有效票，响应的 reward 里有 daily_first（+2）"),
    ("streak6", "连投六天的小夏", "在任 · 见习委员", "连续 6 天有票、今天还没投：投今天的第一张有效票，reward 里同时有 daily_first 和 streak_7（+10），并拿到「连续 7 天」的成就"),
]
# 三期才有的账号：由 seed_phase3 创建（已有的一、二期演示库升级上来时也能补上）
PHASE3_ACCOUNTS = ("appealing", "punished", "dayfirst", "streak6")

_LOG_PREFIX = "[jury-demo]"


def log(message: str = "") -> None:
    print(f"{_LOG_PREFIX} {message}" if message else "", flush=True)


def die(message: str, code: int = 2) -> None:
    print(f"{_LOG_PREFIX} 错误：{message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


# ====================================================================== 环境隔离


def _settings_env_names() -> set[str]:
    """app/core/config.py 里声明的全部配置项（大写的环境变量名）。直接读源码，不能 import。"""
    source = (REPO_ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8")
    return {match.group(1).upper() for match in re.finditer(r"^    ([a-z][a-z0-9_]*)\s*:", source, re.MULTILINE)}


def configure_environment(extra_cors_origins: list[str], db_dir: str | None = None) -> None:
    """必须在 import 任何 app.* 之前调用。"""
    if db_dir:
        target = Path(db_dir).expanduser()
        # 相对路径按系统临时目录解析；写绝对路径也行，但下面会核对它确实在临时目录里
        use_demo_dir((target if target.is_absolute() else TEMP_ROOT / target).resolve())
    if not DB_PATH.resolve().is_relative_to(TEMP_ROOT) or not UPLOAD_DIR.resolve().is_relative_to(TEMP_ROOT):
        die(f"演示数据的路径不在系统临时目录下：{DB_PATH}")
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    if (DEMO_DIR / ".env").exists():
        die(f"{DEMO_DIR} 里有一个 .env，配置会被它影响。请先删掉它。")

    # 1. 清掉外部环境里所有同名配置（REDIS_URL、S3_*、SMTP_*、CONTENT_MODERATION_URL、CCW_BIND_* ……）
    names = _settings_env_names()
    for key in list(os.environ):
        if key.upper() in names:
            del os.environ[key]
    # 2. 只设置演示需要的几项，其余全部走代码里的默认值（外部服务默认都是关的）
    os.environ.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{DB_PATH.as_posix()}",
            "JWT_SECRET_KEY": DEMO_JWT_SECRET,
            "UPLOAD_DIR": UPLOAD_DIR.as_posix(),
            "STORAGE_BACKEND": "local",
            "CONTENT_MODERATION_ENABLED": "false",
            "COOKIE_SECURE": "false",
            "SMTP_ENABLED": "false",
            "TRANSPORT_METRICS_ENABLED": "false",
            "APP_NAME": "KukeChat API (jury demo)",
        }
    )
    if extra_cors_origins:
        defaults = ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174", "http://tauri.localhost", "https://tauri.localhost"]
        os.environ["CORS_ORIGINS"] = json.dumps([*defaults, *extra_cors_origins])
    # 3. 配置类会读当前目录的 .env：换到演示目录，仓库里（或启动目录里）的 .env 就读不到了
    os.chdir(DEMO_DIR)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    log(f"数据库：{DB_PATH}")
    log(f"上传目录：{UPLOAD_DIR}")


def verify_settings() -> None:
    """import 之后再核对一遍实际生效的配置。任何一项不对都直接退出。"""
    from sqlalchemy.engine import make_url

    from app.core.config import settings
    from app.core.database import engine

    problems: list[str] = []
    for label, raw in (("settings.database_url", settings.database_url), ("engine.url", engine.url.render_as_string(hide_password=False))):
        url = make_url(raw)
        database = Path(url.database).resolve() if url.database else None
        if not url.drivername.startswith("sqlite") or database is None or not database.is_relative_to(TEMP_ROOT) or database != DB_PATH.resolve():
            problems.append(f"{label} 指向了 {raw}，不是临时目录里的演示库")
    if not Path(settings.upload_dir).resolve().is_relative_to(TEMP_ROOT):
        problems.append(f"上传目录 {settings.upload_dir} 不在临时目录下")
    if settings.redis_url:
        problems.append("REDIS_URL 仍然有值")
    if settings.storage_backend != "local" or settings.s3_endpoint_url or settings.s3_bucket:
        problems.append("存储后端不是本地磁盘")
    if settings.content_moderation_enabled or settings.content_moderation_url:
        problems.append("内容审核服务仍然是开启 / 已配置的")
    if settings.smtp_enabled or settings.smtp_host:
        problems.append("SMTP 仍然是开启 / 已配置的")
    if settings.ccw_bind_subject_oid or settings.ccw_bind_guest_id:
        problems.append("CCW 绑定仍然是已配置的")
    if settings.jwt_secret_key != DEMO_JWT_SECRET:
        problems.append("JWT 密钥不是演示用的固定值")
    if problems:
        die("配置没有被完全隔离，拒绝启动：\n  - " + "\n  - ".join(problems))


def reset_demo_files() -> None:
    """删库重建：只删演示目录里自己的文件。"""
    targets = [DB_PATH, Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm"), Path(f"{DB_PATH}-journal")]
    try:
        for path in targets:
            if path.exists():
                path.unlink()
        if UPLOAD_DIR.exists():
            shutil.rmtree(UPLOAD_DIR)
    except PermissionError as error:
        die(f"删不掉演示数据（{error.filename}）：多半是演示服务还在运行，先把它停掉再 --reset。")
    log("已删除旧的演示库和上传目录")


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1" if host in {"0.0.0.0", ""} else host, port)) == 0


# ====================================================================== 输出


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def print_accounts(host: str, port: int) -> None:
    headers = ("用户名", "昵称", "角色 / 层级", "适合用来看什么")
    widths = [max(_width(row[index]) for row in [headers, *ACCOUNTS]) for index in range(3)]
    log()
    log(f"演示账号（密码都是 {PASSWORD}）")
    log("  ".join(_pad(headers[index], widths[index]) for index in range(3)) + "  " + headers[3])
    log("  ".join("-" * width for width in widths) + "  " + "-" * 30)
    for row in ACCOUNTS:
        log("  ".join(_pad(row[index], widths[index]) for index in range(3)) + "  " + row[3])
    log("另有 m001–m300：批量建的大厅成员（路人、被举报人），同一个密码，一般用不到。")
    log("其中 m011–m016 是三期追加的 6 位委员（正式 3、见习 3）：只用来给三期的案件凑原审的票、在议事厅里说话。")
    log()
    shown = "127.0.0.1" if host in {"0.0.0.0", ""} else host
    log(f"接口基础地址  http://{shown}:{port}/api/v1   （/api/v1/* 都要带客户端证明头）")
    log(f"用户实时通道  ws://{shown}:{port}/ws?token=<access_token>")
    log(f"后台实时通道  ws://{shown}:{port}/admin/ws?token=<access_token>")
    log(f"上传的图片    http://{shown}:{port}/uploads/...")
    base = "" if port == DEFAULT_PORT else f" --base http://{shown}:{port}"
    log(f"自测：python scripts/jury_demo_check.py{base}（会改数据，跑完用 --reset 重建）")
    log()


# ====================================================================== 假数据：素材

CONVERSATIONS = {
    # key: (标题, join_mode, 简介)
    "plaza": ("创作大厅", "open", "所有人都在的公开大群，成员数超过大型公开群阈值"),
    "pixel": ("像素画社", "approval", "画像素画的小群，进群要审批"),
    "code": ("编程互助组", "approval", "互相看代码的小群，进群要审批"),
    "weekend": ("周末联机", "open", "刚建的小群，公开但人很少"),
    "lounge": ("深夜灌水小分队", "approval", "闲聊群，没有案件"),
}
BULK_USERS = 300
# 各群里负责「说话」的路人；被举报人和举报人另外取，互不重叠
PLAZA_CHATTERS = [f"m{index:03d}" for index in range(241, 301)]
PLAZA_REPORTERS = ["reporter1", "reporter2", "reporter3", "reporter4", *[f"m{index:03d}" for index in range(201, 241)]]
PLAZA_OFFENDERS = [f"m{index:03d}" for index in range(100, 200)]
SMALL_GROUPS = {
    # key: (群主, 被举报人候选, 路人, 举报人, 另外的成员)
    "pixel": ("m005", ["m020", "m021", "m022", "m023"], ["m024", "m025", "m026", "m027", "m028", "m029"], ["reporter1", "reporter2", "m030", "m031"], ["trainee2"]),
    "code": ("m006", ["m040", "m041", "m042", "m043"], ["m044", "m045", "m046", "m047", "m048", "m049"], ["reporter3", "reporter4", "m050", "m051"], ["offender", "eligible"]),
    "weekend": ("m007", ["m060"], ["m061", "m062", "m063", "m064"], ["reporter1", "m065", "m066"], ["newbie"]),
}
# 近 30 天在闲聊群里发够 30 条消息的人（委员申请条件之一）
ACTIVE_CHATTERS = ["juror", "trainee1", "trainee2", "regular2", "senior1", "senior2", "senior3", "suspended", "mutedjuror", "expired", "eligible",
                   "hesitant", "cooldown", "eligible2", "examfail", "exampass", "examout", "applicant"]

FILLER = [
    "有人在吗", "刚放学，来看看大家在聊啥", "今天的热门作品看了吗", "克隆体到底怎么用啊", "我卡在碰撞检测了", "谁有好看的背景素材",
    "周末有比赛吗", "新版编辑器好像更流畅了", "哈哈哈哈", "这个特效怎么做的", "求一个存档的教程", "我做了个小游戏，一会儿发",
    "变量和列表有啥区别", "先写作业去了", "回来了", "有没有人一起做个 RPG", "音效一般去哪找", "画师在吗，想约个头像",
    "刚看到一个超厉害的 3D 作品", "广播积木真好用", "我的角色怎么老是穿墙", "多用几个造型就行", "学到了", "蹲一个联机教程",
    "晚上八点开黑吗", "这个 bug 我也遇到过", "重启一下编辑器试试", "好耶", "明天发新作品", "谁懂啊，调了一晚上",
    "原来如此", "膜拜大佬", "收藏了", "有人看昨天的直播吗", "作品审核一般要多久", "我也想学画像素画",
]

# 群聊消息案件的素材：(举报理由, 被举报的那句, 之前的对话, 之后的对话)。
# 角色：X 被举报人，A 第一个举报人（通常是被针对的那位），B / C 路人。
MSG_VIOLATIONS = [
    ("辱骂骚扰", "你做的游戏就是一坨垃圾，别出来丢人现眼了", [("A", "我的新作品发布了，大家帮忙看看～"), ("B", "玩了一下，手感还不错")], [("A", "……不喜欢可以不玩，没必要这么说吧"), ("C", "好好说话")]),
    ("垃圾广告", "代刷作品点赞，1 元 100 个，要的加 QQ 10000", [("A", "今天的热门榜怎么全是老作品")], [("B", "又是广告"), ("C", "举报了")]),
    ("辱骂骚扰", "脑子有病就去治，天天在群里装什么大佬", [("A", "这个克隆体的写法其实可以再优化一下"), ("B", "学到了")], [("A", "我只是提个建议……")]),
    ("诈骗或钓鱼", "免费领会员！点这个链接登录账号就送：http://gift.example/login", [("A", "会员的云变量上限是多少来着")], [("B", "别点，是钓鱼链接"), ("A", "差点就点了……")]),
    ("辱骂骚扰", "你就是个废物，抄别人的代码还好意思发出来", [("A", "我照着教程做了一个跑酷"), ("B", "第一次做成这样很厉害了")], [("A", "我写了参考来源的……")]),
    ("垃圾广告", "【兼职】在家动动手指日入 300，名额有限，详情私聊我", [("A", "有人一起做游戏吗"), ("B", "我可以画素材")], [("C", "这种一看就是骗人的")]),
    ("违法违规", "有人要破解版的付费素材包吗，我这有全套，私聊发你", [("A", "那个素材包好贵，买不起")], [("B", "这是盗版吧"), ("C", "别在群里发这个")]),
    ("辱骂骚扰", "滚滚滚，都给我滚，一群菜鸡也配评价我的作品", [("A", "感觉这个作品的碰撞判定有点问题"), ("B", "我也卡墙里了")], [("C", "说话注意点")]),
    ("垃圾广告", "新群招人！进群就发红包，群号 000000，手慢无", [("A", "周末有比赛吗")], [("B", "管理来一下")]),
    ("辱骂骚扰", "管理就是摆设，谁举报我我就骂谁全家", [("A", "刚才那条刷屏的被撤回了"), ("B", "清净多了")], [("C", "……"), ("A", "这下又得被撤回")]),
    ("诈骗或钓鱼", "我是官方客服，你的账号有异常，把密码发我帮你解封", [("A", "我的账号怎么登不上了")], [("B", "官方不会找你要密码的"), ("A", "好险")]),
    ("辱骂骚扰", "长这么丑还发自拍，笑死人了，赶紧删了吧", [("A", "今天去漫展啦，发张照片"), ("B", "好看！")], [("A", "……"), ("C", "你这样说话太过分了")]),
    ("垃圾广告", "出售各种游戏外挂，稳定不封号，价格私聊", [("A", "联机对战有人来吗")], [("B", "外挂狗退群")]),
    ("辱骂骚扰", "小学生就别来丢人了，回去写作业吧，菜得抠脚", [("A", "我五年级，这是我做的第一个游戏"), ("B", "加油")], [("C", "年龄小怎么了"), ("A", "……")]),
    ("违法违规", "谁有那个付费课程的网盘资源，我出 5 块钱收", [("A", "那个课讲得挺好的")], [("B", "支持正版吧")]),
    ("辱骂骚扰", "你画的这是什么鬼东西，眼睛不要可以捐了", [("A", "画了个新角色，求点评"), ("B", "配色挺舒服的")], [("A", "可以提意见，但请别这样说")]),
    ("诈骗或钓鱼", "扫码进群免费领 648 充值卡，仅限今天", [("A", "今天有什么活动吗")], [("B", "假的，别扫")]),
    ("垃圾广告", "帮写作业、代做课设，价格便宜量大从优，加我", [("A", "期末要交一个作品，好愁")], [("B", "自己做才有意思"), ("C", "广告举报了")]),
    ("辱骂骚扰", "就你也配参加比赛？上次倒数第一还没丢够人吗", [("A", "这次比赛我报名了，冲一把"), ("B", "加油加油")], [("A", "倒数第一怎么了，我在进步啊"), ("C", "嘴下留情")]),
    ("垃圾广告", "低价代充会员，比官方便宜三成，秒到账，私我", [("A", "会员快到期了")], [("B", "这种一般都是黑卡"), ("C", "别信")]),
    ("诈骗或钓鱼", "中奖通知：你被抽中为幸运用户，点击领取 iPad：http://lucky.example/claim", [("A", "今天群里好安静")], [("B", "假的"), ("A", "连我叫什么都没写")]),
    ("辱骂骚扰", "别 @ 我了，看见你的头像就恶心，离我远点", [("A", "@成员 上次说的合作还做吗"), ("B", "我也想参加")], [("A", "不做就不做，干嘛骂人")]),
    ("违法违规", "分享一个能绕过作品审核的办法，把违规词拆开写就行，亲测有效", [("A", "我的作品怎么又被退回了")], [("B", "别教这个"), ("C", "被发现要封号的")]),
    ("辱骂骚扰", "做个游戏连剧情都抄，你爸妈知道你这么没出息吗", [("A", "我的 RPG 第一章做完了"), ("B", "剧情有点眼熟，不过挺好玩")], [("A", "我只是致敬，写了出处的"), ("C", "扯家人就过分了")]),
    ("垃圾广告", "全网最全素材合集，9.9 打包带走，加群 111111 领取", [("A", "谁有好看的 UI 素材")], [("B", "素材库里免费的就很多")]),
    ("辱骂骚扰", "菜成这样还敢开直播教人，观众都是来看笑话的吧", [("A", "今晚八点我直播做游戏，欢迎来看"), ("B", "一定到")], [("A", "……"), ("C", "不想看可以不看")]),
]
MSG_CLEAN = [
    ("辱骂骚扰", "这个 bug 真是气死我了，垃圾代码！（我自己写的）", [("A", "你那个存档功能修好了吗")], [("A", "哈哈哈自己骂自己"), ("B", "慢慢调，不急")]),
    ("垃圾广告", "我的新作品《像素冒险》发布啦，欢迎来玩，求个赞～", [("B", "最近有什么新作品吗")], [("B", "去玩了，不错"), ("C", "已赞")]),
    ("辱骂骚扰", "你个笨蛋哈哈哈，积木拖反了啦", [("A", "为什么我的角色往后走啊")], [("A", "啊真的！谢谢"), ("B", "经典错误")]),
    ("垃圾广告", "谁有空帮我测试一下联机？加我好友就行", [("A", "联机功能好难做")], [("B", "我来"), ("C", "带我一个")]),
    ("辱骂骚扰", "我觉得这次比赛的评分不太公平，第一名明显是套模板", [("A", "比赛结果出来了")], [("B", "评委有评委的标准吧"), ("C", "可以去反馈一下")]),
    ("垃圾广告", "求推荐好用的画图软件，免费的最好", [("A", "鼠标画画太难了")], [("B", "试试自带的矢量模式"), ("C", "我用的也是免费的")]),
    ("辱骂骚扰", "卧槽这个特效也太帅了吧，怎么做的", [("A", "给你们看我新做的粒子效果")], [("A", "用了克隆体加随机数"), ("B", "求教程")]),
    ("违法违规", "有没有免费可商用的音效网站推荐？", [("A", "我想给游戏加点音效")], [("B", "素材库里就有不少"), ("C", "注意看授权")]),
    ("辱骂骚扰", "这代码写得跟我一样烂，哈哈哈我们半斤八两", [("A", "我这段判断写了二十层嵌套")], [("A", "哈哈哈确实"), ("B", "一起重构吧")]),
    ("垃圾广告", "我们社团在招新，喜欢做游戏的可以来看看置顶", [("A", "有没有一起做游戏的小组")], [("B", "我想加"), ("C", "要求高吗")]),
    ("辱骂骚扰", "别吵了别吵了，再吵我可要生气了（叉腰）", [("A", "明明是我先想到这个点子的"), ("B", "我上周就发过了好吧")], [("A", "好吧好吧"), ("B", "不吵了")]),
    ("辱骂骚扰", "这关设计得太阴间了，作者出来挨打（狗头）", [("A", "我的新关卡上线了，欢迎受苦")], [("A", "嘿嘿，后面还有更难的"), ("B", "我已经死了五十次")]),
    ("垃圾广告", "我在做一个调查问卷，想了解大家平时用什么设备做作品，链接放群文件了", [("A", "最近在写一篇小论文")], [("B", "填了"), ("C", "已填")]),
    ("辱骂骚扰", "你这个思路不行，换我来写三行就搞定了", [("A", "这个排序我写了五十多块积木")], [("A", "那你教教我"), ("B", "他说话直，别介意")]),
    ("诈骗或钓鱼", "官方公告说下周维护，大家记得提前保存作品（链接是官网的公告页）", [("A", "听说要停服维护？")], [("B", "看到了，是官网的"), ("C", "谢谢提醒")]),
    ("辱骂骚扰", "笑死，我自己做的 boss 把我自己打死了，我是不是傻", [("A", "你那个 boss 战做完了吗")], [("A", "哈哈哈难度太高了吧"), ("B", "数值调一下")]),
    ("违法违规", "这个开源项目的代码可以直接拿来改吗？我看协议写的是 MIT", [("A", "想学一下别人的引擎怎么写的")], [("B", "MIT 可以，记得保留署名"), ("C", "对")]),
    ("垃圾广告", "周六晚上社团有线上分享会，讲怎么做对话系统，想听的进语音频道", [("A", "对话系统好难做")], [("B", "几点？"), ("C", "我去听")]),
]
# 公开动态案件的素材：(举报理由, 动态正文, 图片张数)
POST_VIOLATIONS = [
    ("垃圾广告", "低价出售游戏账号，稀有皮肤都有，需要的加微信 demo_wx_001", 2),
    ("诈骗或钓鱼", "免费送 1000 金币！转发这条动态并私聊我你的账号密码即可领取", 1),
    ("垃圾广告", "刷粉刷赞工作室，一单起刷，诚信经营，联系 QQ 10000", 0),
    ("辱骂骚扰", "某些人做的东西就是垃圾，说的就是隔壁那个做跑酷的，建议直接退圈", 0),
    ("垃圾广告", "全网最低价代练代肝，包上榜，不上榜退款，详情看主页", 1),
    ("违法违规", "破解版编辑器下载，去广告无限制，评论区留邮箱我发你", 0),
]
POST_CLEAN = [
    ("垃圾广告", "我的作品终于破 1000 赞了！感谢大家的支持～", 1),
    ("辱骂骚扰", "吐槽一下，最近社区里抄袭的作品越来越多了，心累", 0),
    ("垃圾广告", "招募美术！想做一个 RPG，缺画师，有兴趣的评论区找我", 0),
    ("垃圾广告", "新手教程第三期更新啦：怎么做一个会跳的角色", 2),
    ("辱骂骚扰", "今天的比赛我弃权了，规则改来改去的没意思", 0),
]
NOTES_VIOLATION = [
    "明显是针对人的，不是玩笑", "留了联系方式，就是来引流的", "看上下文是故意的，对方都说不舒服了", "这种链接一看就是钓鱼",
    "语气很冲，但只有这一句，我给轻微", "已经不是第一句了，前面也在攻击", "对方只是正常发言，他上来就骂", "内容本身就违规，和语气无关",
]
NOTES_CLEAN = [
    "结合上下文是朋友间开玩笑", "只是正常发作品，不算广告", "是在吐槽自己，没有针对谁", "语气词而已，没有攻击对象",
    "正常表达意见，没有人身攻击", "对方也没有觉得被冒犯", "举报理由对不上内容",
]
MESSAGE_IMAGES = ["demo-poster.png", "demo-sample-1.png", "demo-sample-2.png", "demo-photo.png"]
POST_IMAGES = ["demo-post-1.png", "demo-post-2.png", "demo-post-3.png"]

# 已定案的委员共识案件：(定案距今多少小时, 类型, 地点, 结论 v 违规 / c 不违规, 有效票数 3 或 5, 主账号怎么投)
# 3 票 = 一致通过；5 票 = 4:1。主账号 disagree 时他就是那 1 票。
D = 24
HISTORY = [
    (48 * D, "msg", "plaza", "v", 3, "agree"),
    (44 * D + 5, "msg", "pixel", "c", 3, None),
    (41 * D, "post", None, "v", 5, "agree"),
    (37 * D + 9, "msg", "plaza", "v", 3, "agree"),
    (33 * D, "msg", "plaza", "c", 5, "disagree"),
    (29 * D + 3, "msg", "plaza", "v", 3, "agree"),
    (28 * D, "post", None, "c", 3, "agree"),
    (27 * D + 6, "msg", "code", "v", 5, "agree"),
    (26 * D + 11, "msg", "plaza", "v", 3, None),
    (25 * D + 2, "msg", "plaza", "c", 3, "agree"),
    (24 * D, "post", None, "v", 3, "agree"),
    (23 * D + 8, "msg", "plaza", "v", 5, "disagree"),
    (22 * D, "msg", "pixel", "v", 3, "agree"),
    (20 * D + 4, "msg", "plaza", "c", 3, "abstain"),
    (19 * D, "msg", "plaza", "v", 3, "agree"),
    (18 * D + 7, "post", None, "v", 5, "agree"),
    (17 * D, "msg", "plaza", "v", 3, "agree"),
    (15 * D + 1, "msg", "code", "c", 5, "agree"),
    (14 * D, "msg", "plaza", "v", 3, "agree"),
    (13 * D + 5, "post", None, "c", 3, None),
    (12 * D + 3, "msg", "plaza", "v", 3, "agree"),
    (11 * D + 9, "msg", "plaza", "v", 5, "agree"),
    (10 * D + 2, "msg", "pixel", "c", 3, "agree"),
    (9 * D + 2, "msg", "plaza", "v", 3, "abstain"),
    (8 * D + 5, "post", None, "v", 3, "agree"),
    (7 * D + 6, "msg", "plaza", "c", 5, "disagree"),
    (6 * D + 3, "msg", "plaza", "v", 3, "agree"),
    (5 * D + 8, "msg", "code", "v", 3, "agree"),
    (5 * D, "post", None, "c", 3, "agree"),
    (4 * D + 4, "msg", "plaza", "v", 5, "agree"),
    (3 * D + 12, "msg", "plaza", "c", 3, "agree"),  # 84 小时前：刚过公示延迟
    (50, "msg", "plaza", "v", 3, "agree"),  # 不足 72 小时：不在公示栏里
    (20, "post", None, "c", 3, None),
    (6, "msg", "plaza", "v", 3, "agree"),
]
# 举报到定案隔了多少小时（都小于 72，否则会先超时）
SPANS = [3, 7, 11, 19, 26, 5, 38, 14, 2, 47, 9, 31]
# 没有会定成严重（l3）的组合：严重违规要收满 5 张有效票，三票定不了案
LEVELS_3 = [("l2", "l2", "l2"), ("l1", "l2", "l2"), ("l2", "l3", "l2"), ("l1", "l1", "l2"), ("l1", "l2", "l1"), ("l2", "l2", "l1")]
LEVELS_4 = [("l2", "l2", "l1", "l2"), ("l2", "l3", "l2", "l1"), ("l1", "l1", "l2", "l1"), ("l3", "l2", "l2", "l3")]
# 上线前由管理员结案的历史案件，是题库里最早的一批题：(类型, 结论)。
# 公开动态认定违规的案件内容会进公示栏，不能当题目，所以这里没有。
GOLDEN = [("msg", "v"), ("msg", "c"), ("post", "c"), ("msg", "v"), ("msg", "c"), ("msg", "v")] * 5
# 管理员核对题目时写的讲解
EXPLANATIONS = {
    "辱骂骚扰": "对具体的人进行贬低或辱骂就算违规。是不是「开玩笑」要看对方的反应：这里对方明确表示了不适。",
    "垃圾广告": "留联系方式招揽生意或引流就是广告，和东西本身好不好无关。",
    "诈骗或钓鱼": "索要账号密码、引导点击来路不明的链接，一律按诈骗处理。",
    "违法违规": "传播盗版、破解或外挂资源，免费分享也算违规。",
    "clean": "没有攻击对象，也没有引流，只是正常的聊天或分享；举报理由和内容对不上。",
}
# 各位委员的任命距今多少天、任期还剩多少天、任命时的层级。regular2 任命时是见习，后来由系统晋升
APPOINTMENTS = [
    ("juror", "regular", 52, 22), ("trainee1", "trainee", 21, 12), ("trainee2", "trainee", 12, 18), ("regular2", "trainee", 45, 9),
    ("senior1", "senior", 75, 25), ("senior2", "senior", 70, 16), ("senior3", "senior", 66, 27),
    ("hesitant", "trainee", 24, 21), ("cooldown", "trainee", 22, 19),
    ("suspended", "trainee", 30, 20), ("mutedjuror", "trainee", 26, 14), ("expired", "regular", 55, -3),
]
# 自测脚本靠这几句话认出有特别用途的待评审案件（委员看到的编号是不透明的评审编号，认不出来）
DEMO_TARGETS = {
    "settle_violation": "就你这种智商还学编程？趁早放弃吧，废物一个，看见你就烦",
    "settle_clean": "【福利】关注我并转发这条动态，抽 3 个人送我自己画的全套像素素材包，周日开奖",
    "no_consensus": "低价转让会员账号，先付款后给密码，支持分期",
    "need_context": "懂的都懂，上次那件事就是他干的，大家心里有数",
    "severe": "再让我看见你发作品，我就找人去你学校门口堵你，说到做到",
    "category": "签到签到签到签到签到签到（今天第 20 遍）",
    "recused": "隔壁三班的小林做的那个游戏纯纯抄袭，大家别玩，谁玩谁是傻子",
    # —— 三期 ——
    "panel_fresh": "说实话这游戏做得跟屎一样，建议作者先去玩玩别人的再来做",
    "panel_one_vote": "招募内测员：帮我测新游戏并写 50 字好评的同学，每人发 5 元红包，名额 20 个，评论区留言我私聊你",
    "panel_need_context": "某些人赢了也不光彩，怎么赢的自己清楚",
    "panel_settled": "前十有什么了不起，全靠刷分的吧，没实力就别出来显摆",
    "appeal_jury": "你是不是有病啊，同一个问题问八百遍，没人欠你的",
    "appeal_machine": "免费代刷作品浏览量，一天涨一千，想要的私我",
    "reconsider_pending": "别人做的都比你好，你还是别做了，浪费大家时间",
    "reconsider_open": "你昨天那把打得也太坑了吧，下次别选射手了",
    "machine_1": "这种垃圾作品也能上推荐？评审是瞎了吗",
    "machine_2": "有偿带过关，一关两块，童叟无欺",
    "machine_3": "实名吐槽隔壁班的王某：借了我的素材不还，还把我拉黑了，大家避雷",
}
# 灌数据时这一票「应该发生」的时间；_seed_daily_rewards 用它决定每日首票记在哪一天
_SEED_CLOCK: datetime | None = None
OTHER_JURORS = ["senior1", "trainee1", "senior2", "regular2", "senior3", "trainee2", "suspended", "mutedjuror"]
# suspended 在 6.5 天前被系统暂停，mutedjuror 在 20 小时前被禁言：之后的案件他们不再参与
SUSPENDED_AT_HOURS = 156
MUTED_AT_HOURS = 20


# ====================================================================== 假数据：工具


def _bind_app_modules() -> None:
    """把 app.* 里用到的名字绑到模块全局。只能在 configure_environment() 之后调用。"""
    import app.models  # noqa: F401
    from sqlalchemy import func, or_, select, update
    from sqlalchemy.orm import joinedload

    from app.core.config import settings
    from app.core.database import AsyncSessionLocal, engine, init_db
    from app.core.security import hash_password
    from app.models.case import (
        JuryAchievement,
        JuryExamAttempt,
        JuryMember,
        JuryNotification,
        JuryOpinion,
        JuryPointLog,
        JuryPracticeAnswer,
        JuryReview,
        JuryReviewVote,
        JuryVote,
        ReportCase,
    )
    from app.models.conversation import Conversation, ConversationMember
    from app.models.enums import ConversationType, MemberRole, MessageType, ModerationAction, ModerationSource, PlatformRole, PostVisibility, ReportTargetType
    from app.models.friend import Friendship
    from app.models.jury_hall import JuryHallComment, JuryHallPost, JuryHallReport, JuryHallVote, JuryPrecedent
    from app.models.message import Message
    from app.models.penalty import UserViolation
    from app.models.post import Post
    from app.models.report import AdminAuditLog, ContentModerationRecord, UserReport
    from app.models.system import SystemSetting
    from app.models.user import User
    from app.schemas.report import ReportCreate
    from app.services import cases as case_service
    from app.services import jury as jury_service
    from app.services import jury_bank as bank_service
    from app.services import jury_exam as exam_service
    from app.services import jury_hall as hall_service
    from app.services import jury_machine as machine_service
    from app.services import jury_opinions as opinion_service
    from app.services import jury_points as points_service
    from app.services import jury_overview as overview_service
    from app.services import jury_panel as panel_service
    from app.services import jury_precedents as precedent_service
    from app.services import jury_reviews as review_service
    from app.services import jury_settings
    from app.services import penalties as penalty_service
    from app.models.gift import GiftRecord
    from app.models.shop import ShopItem, ShopPurchase
    from app.services import birthdays as birthday_service
    from app.services import gifts as gift_service
    from app.services import red_packets as red_packet_service
    from app.services import reports as report_service
    from app.services import shop as shop_service

    globals().update({name: value for name, value in locals().items() if not name.startswith("_")})


def write_demo_images() -> None:
    """在上传目录里生成几张几 KB 的占位图（和它们的缩略图），给图片消息和带图动态用。"""
    palette = [(239, 108, 87), (86, 156, 214), (120, 190, 120), (240, 190, 80), (160, 120, 210), (90, 200, 200), (230, 130, 180)]
    jobs = [("messages", name) for name in MESSAGE_IMAGES] + [("posts", name) for name in POST_IMAGES]
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # 没有 Pillow 就退回手写的纯色 PNG，不生成缩略图
        Image = None
    for index, (folder, name) in enumerate(jobs):
        target = UPLOAD_DIR / folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        color = palette[index % len(palette)]
        if Image is None:
            target.write_bytes(_solid_png(64, 48, color))
            continue
        image = Image.new("RGB", (480, 320), color)
        draw = ImageDraw.Draw(image)
        # 几个色块 + 编号，让每张图一眼能分清，又不会被当成真实内容
        for block in range(6):
            x = 30 + block * 72
            shade = tuple(max(0, channel - 40 - block * 12) for channel in color)
            draw.rectangle([x, 60 + (block % 2) * 40, x + 56, 200 + (block % 3) * 30], fill=shade)
        draw.rectangle([0, 270, 480, 320], fill=(255, 255, 255))
        draw.text((16, 286), f"DEMO IMAGE {index + 1}  ({folder}/{name})", fill=(60, 60, 60))
        image.save(target, format="PNG", optimize=True)
        thumb = UPLOAD_DIR / folder / "thumbs" / f"{target.stem}.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        image.resize((240, 160)).save(thumb, format="JPEG", quality=70)


def _solid_png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\x00" + bytes(color) * width
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(row * height, 9)) + chunk(b"IEND", b"")


class Seeder:
    """灌数据用的工具。

    约定：有业务逻辑的数据（举报、分队列、投票、定案、观点、改判、处罚、申诉）一律调服务层，
    这样代号、计数、一致率、公示标记都是真的；服务层把时间写成「现在」，这里随后把时间戳改到过去。
    """

    def __init__(self, db: Any, now: datetime) -> None:
        self.db = db
        self.now = now
        self.rng = random.Random(20261012)
        self.user_ids: dict[str, int] = {}
        self.conversation_ids: dict[str, int] = {}
        self._marks: dict[str, int] = {}
        self._counters: dict[str, int] = {}
        self._rotation = 0

    # ------------------------------------------------------------ 基础

    def ago(self, hours: float = 0, *, days: float = 0, minutes: float = 0) -> datetime:
        return self.now - timedelta(days=days, hours=hours, minutes=minutes)

    async def load(self) -> None:
        """从库里读出用户和群的 id，这样每一期都可以单独在一个新会话里跑。"""
        rows = await self.db.execute(select(User.id, User.username))
        self.user_ids = {username: user_id for user_id, username in rows.all()}
        rows = await self.db.execute(select(Conversation.id, Conversation.title).where(Conversation.type == ConversationType.GROUP))
        by_title = {title: conversation_id for conversation_id, title in rows.all()}
        self.conversation_ids = {key: by_title[title] for key, (title, _mode, _text) in CONVERSATIONS.items() if title in by_title}
        await self._read_marks()

    def uid(self, username: str) -> int:
        return self.user_ids[username]

    async def user(self, username: str) -> Any:
        return await self.db.get(User, self.uid(username))

    def take(self, name: str, items: list[Any]) -> Any:
        """按顺序循环取素材。"""
        index = self._counters.get(name, 0)
        self._counters[name] = index + 1
        return items[index % len(items)]

    @staticmethod
    def _stamped() -> tuple[tuple[Any, str], ...]:
        return ((AdminAuditLog, "created_at"), (JuryPointLog, "created_at"), (JuryAchievement, "earned_at"), (JuryNotification, "created_at"))

    async def _read_marks(self) -> None:
        for model, _column in self._stamped():
            self._marks[model.__tablename__] = int((await self.db.execute(select(func.coalesce(func.max(model.id), 0)))).scalar_one())

    async def stamp_new(self, at: datetime) -> None:
        """服务层在上次调用之后新写的审计日志、风纪值流水、成就、通知（时间都是现在）改到 at。"""
        await self.db.flush()
        for model, column in self._stamped():
            await self.db.execute(update(model).where(model.id > self._marks.get(model.__tablename__, 0)).values({column: at}))
        await self._read_marks()

    def local_time(self, days_ago: int, hour: float) -> datetime:
        """北京时间 days_ago 天前那一天的 hour 点。连续天数、每日首票、周榜月榜都按 UTC+8 的自然日算。"""
        day = points_service.local_day(self.now) - timedelta(days=days_ago)
        moment = datetime(day.year, day.month, day.day, tzinfo=points_service.LOCAL_TZ) + timedelta(hours=hour)
        return min(moment.astimezone(UTC), self.now - timedelta(minutes=2))

    def this_week(self, at: datetime, slot: int = 0) -> datetime:
        """把时间挪到本周（自然周）之内：周一刚过零点灌数据时，周榜上也得有人。"""
        floor = points_service.period_start("week", self.now) + timedelta(minutes=4 * (slot + 1))
        return max(at, min(floor, self.now - timedelta(minutes=2)))

    # ------------------------------------------------------------ 内容

    async def message(self, conversation: str | int, sender: str, content: str, at: datetime, **options: Any) -> Any:
        conversation_id = conversation if isinstance(conversation, int) else self.conversation_ids[conversation]
        metadata = {"images": options["images"]} if options.get("images") else None
        item = Message(
            conversation_id=conversation_id,
            sender_id=self.uid(sender),
            type=MessageType.IMAGE if options.get("image") else MessageType.TEXT,
            content=content,
            metadata_json=json.dumps(metadata, ensure_ascii=False) if metadata else None,
            created_at=at,
            recalled_at=at + timedelta(seconds=20) if options.get("recalled") else None,
            recalled_by_id=self.uid(sender) if options.get("recalled") else None,
        )
        self.db.add(item)
        await self.db.flush()
        return item

    def filler(self, count: int, speakers: list[str]) -> list[tuple[str, str]]:
        return [(self.rng.choice(speakers), self.take("filler", FILLER)) for _ in range(count)]

    async def report(self, reporter: str, target_type: Any, target_id: int, reason: str, at: datetime, **extra: Any) -> Any:
        """走真实的举报流程（生成快照、并案、算优先级），再把时间改到 at。"""
        # 生成快照时要用到举报人：和接口里一样，先把他加载进会话并一直引用着。
        reporter_user = await self.user(reporter)
        report = await report_service.create_report(self.db, reporter_user.id, ReportCreate(target_type=target_type, target_id=target_id, reason=reason, **extra))
        case = await self.db.get(ReportCase, report.case_id)
        report.created_at = at
        if case.first_report_id == report.id:
            case.created_at = at
        case.updated_at = at
        await self.db.commit()
        return report

    async def message_case(
        self,
        *,
        conversation: str | int,
        offender: str,
        target: str,
        reporters: list[str],
        reason: str,
        reported_at: datetime,
        before: list[tuple] | None = None,
        after: list[tuple] | None = None,
        pad_before: int = 0,
        pad_after: int = 0,
        details: str | None = None,
        target_options: dict[str, Any] | None = None,
    ) -> int:
        """在群里写一段对话，然后由 reporters 依次举报其中 offender 的那一句。返回案件 id。"""
        if conversation == "plaza":
            chatters = PLAZA_CHATTERS
        elif isinstance(conversation, int):  # 私聊：只有两个人
            chatters = [reporters[0], offender]
        else:
            chatters = SMALL_GROUPS[conversation][2]
        extras = self.rng.sample(chatters, 2)
        roles = {"X": offender, "A": reporters[0], "B": extras[0], "C": extras[1]}

        def lines(items: list[tuple] | None) -> list[tuple[str, str, dict[str, Any]]]:
            return [(roles.get(item[0], item[0]), item[1], item[2] if len(item) > 2 else {}) for item in items or []]

        scripted = lines(before)
        sent_at = reported_at - timedelta(minutes=8)
        own = pad_before + len(scripted)
        # 快照会取被举报消息之前的 10 条。自己写的不够 10 条时，前面会接上这个会话里更早的消息；
        # 而案件不是严格按时间先后造的，接上的那几条时间可能反而更晚。遇到这种情况就把自己的上下文补满 10 条。
        if own < 10 and not await self._earlier_messages_fit(conversation, 10 - own, sent_at - timedelta(seconds=47 * own)):
            pad_before = 10 - len(scripted)
        before_lines = [(who, text, {}) for who, text in self.filler(pad_before, chatters)] + scripted
        after_lines = lines(after) + [(who, text, {}) for who, text in self.filler(pad_after, chatters)]
        created = []
        for index, (who, text, options) in enumerate(before_lines):
            created.append(await self.message(conversation, who, text, sent_at - timedelta(seconds=47 * (len(before_lines) - index)), **options))
        target_message = await self.message(conversation, offender, target, sent_at, **(target_options or {}))
        created.append(target_message)
        for index, (who, text, options) in enumerate(after_lines):
            created.append(await self.message(conversation, who, text, sent_at + timedelta(seconds=38 * (index + 1)), **options))
        await self.db.commit()
        target_id = target_message.id
        # 让这些消息之后从库里重新读：线上生成快照时消息都是读出来的（SQLite 不带时区），演示数据保持同样的形状。
        for item in created:
            self.db.expire(item)
        case_id = 0
        for index, reporter in enumerate(reporters):
            report = await self.report(reporter, ReportTargetType.MESSAGE, target_id, reason, reported_at + timedelta(minutes=4 * index), details=details)
            case_id = report.case_id
        return case_id

    async def _earlier_messages_fit(self, conversation: str | int, count: int, start: datetime) -> bool:
        """这个会话里最后 count 条消息是不是时间递增、并且都早于 start。"""
        conversation_id = conversation if isinstance(conversation, int) else self.conversation_ids[conversation]
        rows = await self.db.execute(select(Message.created_at).where(Message.conversation_id == conversation_id).order_by(Message.id.desc()).limit(count))
        times = [penalty_service.aware(value) for value in reversed(rows.scalars().all())]
        return all(left <= right for left, right in zip(times, times[1:])) and (not times or times[-1] < start)

    async def post(self, author: str, content: str, images: int, at: datetime) -> Any:
        urls = [f"/uploads/posts/{POST_IMAGES[index % len(POST_IMAGES)]}" for index in range(images)]
        item = Post(
            author_id=self.uid(author),
            content=content,
            visibility=PostVisibility.PUBLIC,
            image_urls_json=json.dumps(urls),
            moderation_status="approved",
            created_at=at,
            updated_at=at,
        )
        self.db.add(item)
        await self.db.commit()
        return item

    async def post_case(self, *, author: str, content: str, images: int, reporters: list[str], reason: str, reported_at: datetime, details: str | None = None) -> int:
        item = await self.post(author, content, images, reported_at - timedelta(hours=2, minutes=13))
        post_id = item.id
        self.db.expire(item)
        case_id = 0
        for index, reporter in enumerate(reporters):
            report = await self.report(reporter, ReportTargetType.POST, post_id, reason, reported_at + timedelta(minutes=6 * index), details=details)
            case_id = report.case_id
        return case_id

    def reporters_for(self, conversation: str | None, count: int = 1) -> list[str]:
        pool = PLAZA_REPORTERS if conversation in {None, "plaza"} else SMALL_GROUPS[conversation][3]
        return [self.take(f"reporter:{conversation or 'plaza'}", pool) for _ in range(count)]

    def offender_for(self, conversation: str | None) -> str:
        pool = PLAZA_OFFENDERS if conversation in {None, "plaza"} else SMALL_GROUPS[conversation][1]
        return self.take(f"offender:{conversation or 'plaza'}", pool)

    async def scenario_case(self, kind: str, conversation: str | None, outcome: str, reported_at: datetime, *, reporters: int = 1) -> int:
        """从素材库里按顺序取一个场景建案。outcome 只决定取「看起来违规」还是「看起来没问题」的素材。"""
        names = self.reporters_for(conversation, reporters)
        offender = self.offender_for(conversation)
        if kind == "post":
            reason, content, images = self.take(f"post:{outcome}", POST_VIOLATIONS if outcome == "v" else POST_CLEAN)
            return await self.post_case(author=offender, content=content, images=images, reporters=names, reason=reason, reported_at=reported_at)
        reason, target, before, after = self.take(f"msg:{outcome}", MSG_VIOLATIONS if outcome == "v" else MSG_CLEAN)
        index = self._counters[f"msg:{outcome}"]
        return await self.message_case(
            conversation=conversation or "plaza",
            offender=offender,
            target=target,
            reporters=names,
            reason=reason,
            reported_at=reported_at,
            before=before,
            after=after,
            # 大厅里话多，上下文补长一些；小群保持原样
            pad_before=(4 + index % 5) if conversation == "plaza" else 0,
            pad_after=(index % 6) if conversation == "plaza" else 0,
        )

    # ------------------------------------------------------------ 案件流转

    async def case(self, case_id: int) -> Any:
        case = await self.db.get(ReportCase, case_id)
        await self.db.refresh(case)
        return case

    async def triage(self, case_id: int, at: datetime) -> Any:
        """机器预审之后分队列（演示里审核服务是关的，所以只做分队列这一步）。"""
        await case_service.triage_case(case_id)
        case = await self.case(case_id)
        if case.queue == "jury":
            case.queued_at = at
        case.updated_at = at
        await self.db.commit()
        return case

    async def vote(
        self, case_id: int, juror: str, vote: str, at: datetime, note: str | None = None, *, category: str | None = None, quality: bool = False
    ) -> dict[str, Any]:
        """分给这位委员并让他投票（走 jury_service.cast_vote），再把时间改到 at。

        case_id 是案件的真实编号；cast_vote 认的是评审编号（分配记录的 id），这里换一下。
        quality=True 表示这次分配是一道质控题。vote 可以是 recuse（主动回避）。
        """
        global _SEED_CLOCK
        user = await self.user(juror)
        case = await self.case(case_id)
        if await jury_service._has_conflict(self.db, user.id, case):
            raise RuntimeError(f"{juror} 对案件 {case_id} 有回避关系，演示数据不应该让他投这一票")
        assignment = JuryVote(case_id=case_id, juror_id=user.id, assigned_at=self.now, is_quality_check=quality)
        self.db.add(assignment)
        await self.db.commit()
        member = await jury_service.get_member(self.db, user.id)
        cooldown_before = member.cooldown_until
        _SEED_CLOCK = at
        try:
            result = await jury_service.cast_vote(self.db, user, assignment.id, vote, note, category)
        finally:
            _SEED_CLOCK = None
        if vote == "recuse":
            assignment.recused_at = at
        else:
            assignment.voted_at = at
        assignment.assigned_at = at - timedelta(minutes=9)
        if member.cooldown_until is not None and member.cooldown_until != cooldown_before:
            # 这一票触发了弃权冷却：冷却从这一票的时间算起
            member.cooldown_until = at + timedelta(hours=jury_service.ABSTAIN_COOLDOWN_HOURS)
        await self.db.execute(
            update(JuryOpinion).where(JuryOpinion.case_id == case_id, JuryOpinion.author_id == user.id, JuryOpinion.is_note.is_(True)).values(created_at=at)
        )
        await self.stamp_new(at)
        await self.db.commit()
        return result

    async def stamp_closed(self, case_id: int, at: datetime) -> Any:
        """案件刚由服务层结案（时间是现在）：把定案时间以及它带出来的处罚、撤回、举报结论都改到 at。"""
        case = await self.case(case_id)
        closed_at = penalty_service.aware(case.closed_at)
        case.closed_at = at
        case.updated_at = at
        if case.jury_outcome == "manual":
            case.escalated_at = at
        for report in await case_service.case_reports(self.db, case_id):
            if report.reviewed_at is not None:
                report.reviewed_at = at
        await self._stamp_content(case, closed_at, at)
        await self._stamp_violations(case, closed_at, at)
        await self.stamp_new(at)
        await self.db.commit()
        return case

    async def _stamp_content(self, case: Any, since: datetime, at: datetime) -> None:
        margin = since - timedelta(seconds=30)
        if case.target_type == "message" and case.message_id is not None:
            item = await self.db.get(Message, case.message_id)
            await self.db.refresh(item)
            if item.recalled_at is not None and penalty_service.aware(item.recalled_at) >= margin:
                item.recalled_at = at
        elif case.target_type == "post":
            item = await self.db.get(Post, case.target_id)
            await self.db.refresh(item)
            if item.deleted_at is not None and penalty_service.aware(item.deleted_at) >= margin:
                item.deleted_at = at

    async def _stamp_violations(self, case: Any, since: datetime, at: datetime) -> None:
        """这个案件在 since 之后记下 / 撤销的处罚，时间整体平移到 at，并按台账重算禁言 / 封禁。"""
        margin = since - timedelta(seconds=30)
        violations = (await self.db.execute(select(UserViolation).where(UserViolation.case_id == case.id))).scalars().all()
        for violation in violations:
            await self.db.refresh(violation)
            created_at = penalty_service.aware(violation.created_at)
            if created_at >= margin:
                violation.created_at = at
                violation.expires_at = at + timedelta(days=penalty_service.POINT_WINDOW_DAYS)
                if violation.penalty_ends_at is not None:
                    violation.penalty_ends_at = penalty_service.aware(violation.penalty_ends_at) + (at - created_at)
            if violation.revoked_at is not None and penalty_service.aware(violation.revoked_at) >= margin:
                violation.revoked_at = at
            await self.db.flush()
            user = await self.db.get(User, violation.user_id)
            await self.db.refresh(user)
            await penalty_service._resync_restrictions(self.db, user, violation, self.now)
            if user.banned_at is not None:
                user.banned_at = at

    async def admin_resolve(self, case_id: int, at: datetime, *, upheld: bool, category: str | None = None, level: int | None = None, note: str | None = None) -> Any:
        """管理员结案。和 POST /admin/cases/{id}/resolve 做的事一样：结案、回填委员的一致率、作为未核对的题进题库。"""
        admin = await self.user("admin")
        case = await case_service.resolve_case(self.db, case_id, source="admin", actor_id=admin.id, upheld=upheld, category=category, level=level, note=note)
        await jury_service.settle_after_admin(self.db, case)
        await bank_service.include_after_admin(self.db, case)
        await self.db.commit()
        await points_service.flush_events(self.db)
        return await self.stamp_closed(case_id, at)

    async def overturn(self, case_id: int, at: datetime, *, upheld: bool, note: str, category: str | None = None, level: int | None = None) -> Any:
        """管理员改判（case_service.overturn_case），再把改判时间改到 at。"""
        admin = await self.user("admin")
        started = datetime.now(UTC)
        await case_service.overturn_case(self.db, case_id, actor_id=admin.id, upheld=upheld, category=category, level=level, note=note)
        return await self.stamp_overturned(case_id, started, at)

    async def stamp_overturned(self, case_id: int, started: datetime, at: datetime) -> Any:
        """案件刚被服务层改判（管理员改判、申诉撤销、复议改判；时间是现在）：把改判时间和它带出来的处罚、撤回、举报结论都改到 at。"""
        case = await self.case(case_id)
        case.overturned_at = at
        case.updated_at = at
        for report in await case_service.case_reports(self.db, case_id):
            report.reviewed_at = at
        await self._stamp_content(case, started, at)
        await self._stamp_violations(case, started, at)
        await self.stamp_new(at)
        await self.db.commit()
        return case

    # ------------------------------------------------------------ 三期：合议、申诉复审、复议

    async def trial(self, case_id: int, ballots: list[tuple], start: datetime, end: datetime) -> dict[str, Any]:
        """原审：ballots 里的委员依次投票，时间均匀排在 start 到 end 之间。每项是 (委员, 票, 备注?, 类别?)。返回最后一票的结果。"""
        result: dict[str, Any] = {}
        for position, ballot in enumerate(ballots):
            at = start + (end - start) * ((position + 1) / len(ballots))
            result = await self.vote(case_id, ballot[0], ballot[1], at, ballot[2] if len(ballot) > 2 else None, category=ballot[3] if len(ballot) > 3 else None)
        return result

    async def to_panel(self, case_id: int, at: datetime, outcome: str) -> Any:
        """案件刚由服务层转进资深合议（cases.escalate_from_jury）：核对一下，再把转出的时间改到 at。合议的 48 小时从这里算起。"""
        case = await self.case(case_id)
        if (case.status, case.queue, case.jury_outcome) != ("open", "panel", outcome):
            raise RuntimeError(f"案件 {case_id} 应该因为 {outcome} 进了资深合议，实际是 {case.status} / {case.queue} / {case.jury_outcome}")
        case.escalated_at = case.panel_at = case.updated_at = at
        await self.db.commit()
        return case

    async def panel_vote(self, case_id: int, juror: str, vote: str, at: datetime, note: str | None = None, *, category: str | None = None) -> dict[str, Any]:
        """分给这位资深委员并让他投合议票（走 panel_service.cast_vote），再把时间改到 at。"""
        global _SEED_CLOCK
        user = await self.user(juror)
        case = await self.case(case_id)
        if await jury_service._has_conflict(self.db, user.id, case):
            raise RuntimeError(f"{juror} 对案件 {case_id} 有回避关系，演示数据不应该让他参加合议")
        assignment = JuryVote(case_id=case_id, juror_id=user.id, assigned_at=self.now, stage="panel")
        self.db.add(assignment)
        await self.db.commit()
        _SEED_CLOCK = at
        try:
            result = await panel_service.cast_vote(self.db, user, assignment.id, vote, note, category)
        finally:
            _SEED_CLOCK = None
        assignment.voted_at = at
        assignment.assigned_at = at - timedelta(minutes=14)
        await self.db.execute(
            update(JuryOpinion).where(JuryOpinion.case_id == case_id, JuryOpinion.author_id == user.id, JuryOpinion.is_note.is_(True)).values(created_at=at)
        )
        await self.stamp_new(at)
        await self.db.commit()
        return result

    async def stamp_violation(self, violation: Any, at: datetime) -> None:
        """一条不经过案件、刚记下的处罚（时间是现在）改到 at，并按台账重算禁言 / 封禁。"""
        await self.db.refresh(violation)
        created_at = penalty_service.aware(violation.created_at)
        violation.created_at = at
        violation.expires_at = at + timedelta(days=penalty_service.POINT_WINDOW_DAYS)
        if violation.penalty_ends_at is not None:
            violation.penalty_ends_at = penalty_service.aware(violation.penalty_ends_at) + (at - created_at)
        await self.db.flush()
        user = await self.db.get(User, violation.user_id)
        await self.db.refresh(user)
        await penalty_service._resync_restrictions(self.db, user, violation, self.now)
        await self.stamp_new(at)
        await self.db.commit()

    async def appeal(self, username: str, violation_id: int, text: str, at: datetime) -> Any:
        """提交申诉（penalty_service.submit_appeal），再把时间改到 at。进了资深复审的返回那件复审，直接到管理员的返回 None。"""
        violation = await penalty_service.submit_appeal(self.db, self.uid(username), violation_id, text)
        violation.appealed_at = at
        review = (await self.db.execute(select(JuryReview).where(JuryReview.kind == "appeal", JuryReview.violation_id == violation_id))).scalar_one_or_none()
        if review is not None:
            review.created_at = at
            review.expires_at = at + timedelta(hours=review_service.REVIEW_TIMEOUT_HOURS)
        await self.db.commit()
        return review

    async def reconsider(self, username: str, report_id: int, text: str, at: datetime) -> Any:
        """举报人申请复议（review_service.request_reconsider），再把时间改到 at。返回那件复审。"""
        await review_service.request_reconsider(self.db, self.uid(username), report_id, text)
        review = (await self.db.execute(select(JuryReview).where(JuryReview.kind == "reconsider", JuryReview.report_id == report_id))).scalar_one()
        review.created_at = at
        review.expires_at = at + timedelta(hours=review_service.REVIEW_TIMEOUT_HOURS)
        if review.escalated_at is not None:
            review.escalated_at = at
        await self.db.commit()
        return review

    async def review_vote(
        self, review_id: int, juror: str, vote: str, at: datetime, note: str | None = None, *, level: int | None = None, category: str | None = None
    ) -> dict[str, Any]:
        """分给这位资深委员并让他投复审票（走 review_service.cast_vote），再把时间改到 at。出了结果的话，结果落地的时间也跟着改。"""
        user = await self.user(juror)
        assignment = JuryReviewVote(review_id=review_id, juror_id=user.id, assigned_at=self.now)
        self.db.add(assignment)
        await self.db.commit()
        started = datetime.now(UTC)
        result = await review_service.cast_vote(self.db, user, review_id, vote, note, level, category)
        assignment.assigned_at = at - timedelta(minutes=17)
        assignment.voted_at = at
        review = await self.db.get(JuryReview, review_id)
        await self.db.refresh(review)
        if result["settled"]:
            review.closed_at = at
            if review.violation_id is not None:
                violation = await self.db.get(UserViolation, review.violation_id)
                await self.db.refresh(violation)
                violation.appeal_reviewed_at = at
                if violation.reviewed_at is not None and penalty_service.aware(violation.reviewed_at) >= started - timedelta(seconds=30):
                    violation.reviewed_at = at
            if result["outcome"] in {"revoke", "overturn"} and review.case_id is not None:
                # 撤销处罚 / 复议改判都会连带改判案件
                await self.db.commit()
                await self.stamp_overturned(review.case_id, started, at)
        await self.stamp_new(at)
        await self.db.commit()
        return result

    # ------------------------------------------------------------ 观点区

    async def note_id(self, case_id: int, author: str) -> int | None:
        rows = await self.db.execute(
            select(JuryOpinion.id).where(JuryOpinion.case_id == case_id, JuryOpinion.author_id == self.uid(author), JuryOpinion.is_note.is_(True))
        )
        return rows.scalars().first()

    async def opinion(self, case_id: int, author: str, content: str, at: datetime, parent_id: int | None = None) -> int:
        user = await self.user(author)
        # 观点区的接口认的也是评审编号
        review_id = await points_service.review_id(self.db, user.id, case_id)
        view = await opinion_service.create_opinion(self.db, user, review_id, content, parent_id)
        # 立刻改到过去：既是数据需要，也免得连续造数据撞上「每分钟 5 条」的限制
        await self.db.execute(update(JuryOpinion).where(JuryOpinion.id == view["id"]).values(created_at=at))
        await self.db.commit()
        return view["id"]

    async def like(self, opinion_id: int, juror: str, at: datetime | None = None) -> None:
        await opinion_service.set_like(self.db, await self.user(juror), opinion_id, True)
        if at is None:
            created = (await self.db.execute(select(JuryOpinion.created_at).where(JuryOpinion.id == opinion_id))).scalar_one()
            at = min(self.now, penalty_service.aware(created) + timedelta(minutes=50))
        # 赞数到了阈值会给作者加风纪值，流水的时间跟着改
        await self.stamp_new(at)
        await self.db.commit()

    # ------------------------------------------------------------ 考试、练习、连续天数

    async def exam(self, username: str, score: int, at: datetime, *, minutes: int = 18) -> dict[str, Any]:
        """让这个人考一场入门考试并拿到指定的分数（走 exam_service 的开考和交卷），再把时间改到 at。"""
        user = await self.user(username)
        started = await exam_service.start_exam(self.db, user)
        attempt = await self.db.get(JuryExamAttempt, started["attempt_id"])
        answers: list[dict[str, Any]] = []
        for number, case_id in enumerate(json.loads(attempt.question_ids), start=1):
            if number > score + 2:
                continue  # 没通过的卷子留几道没答：没答的按答错算
            upheld = bank_service.question_upheld(await self.db.get(ReportCase, int(case_id)))
            right = number <= score
            answers.append({"question": number, "vote": ("l2" if upheld else "none") if right else ("none" if upheld else "l1")})
        result = await exam_service.submit_exam(self.db, user, attempt.id, answers)
        if result["score"] != score:
            raise RuntimeError(f"{username} 的考试应该得 {score} 分，实际 {result['score']} 分")
        attempt.started_at = at
        attempt.expires_at = at + timedelta(minutes=exam_service.EXAM_TIME_LIMIT_MINUTES)
        attempt.submitted_at = at + timedelta(minutes=minutes)
        await self.stamp_new(at + timedelta(minutes=minutes))
        await self.db.commit()
        return result

    async def practice(self, username: str, times: list[datetime], *, wrong: tuple[int, ...] = ()) -> int:
        """在练习场按顺序做 len(times) 道题（走 exam_service 的取题和作答），第 wrong 里那几道故意答错。返回做了几道。"""
        user = await self.user(username)
        done = 0
        for index, at in enumerate(times):
            question = (await exam_service.practice_next(self.db, user))["question"]
            if question is None:
                break
            upheld = bank_service.question_upheld(await self.db.get(ReportCase, question["question_id"]))
            right = index not in wrong
            await exam_service.practice_answer(self.db, user, question["question_id"], ("l2" if upheld else "none") if right else ("none" if upheld else "l1"))
            latest = (await self.db.execute(select(func.max(JuryPracticeAnswer.id)).where(JuryPracticeAnswer.user_id == user.id))).scalar_one()
            # 立刻改到过去：练习上限按最近 24 小时算
            await self.db.execute(update(JuryPracticeAnswer).where(JuryPracticeAnswer.id == latest).values(created_at=at))
            await self.db.commit()
            done += 1
        return done

    async def rebuild_streaks(self) -> None:
        """按每个人实际投票的日子重算连续参与天数，并补上「连续 7 天」的加分。

        灌数据不是按时间先后一票一票投的，所以投票时没有记连续天数（见 _seed_daily_rewards），在这里统一按时间顺序重放一遍。
        """
        for member in (await self.db.execute(select(JuryMember))).scalars().all():
            rows = await self.db.execute(
                select(JuryVote.voted_at)
                .where(JuryVote.juror_id == member.user_id, JuryVote.voted_at.is_not(None), JuryVote.vote.in_(jury_service.DECIDED_VOTES))
                .order_by(JuryVote.voted_at.asc())
            )
            last_vote_of_day: dict[Any, datetime] = {}
            for voted_at in rows.scalars().all():
                moment = penalty_service.aware(voted_at)
                last_vote_of_day[points_service.local_day(moment)] = moment
            member.streak_days, member.streak_last_day = 0, None
            reached: datetime | None = None
            for day in sorted(last_vote_of_day):
                if points_service.touch_streak(member, day):
                    await points_service.award(self.db, member, "streak_7", ref_id=day.toordinal(), announce=False)
                    await self.stamp_new(last_vote_of_day[day])
                    reached = last_vote_of_day[day]
            await points_service.check_achievements(self.db, member.user_id, member)
            await self.stamp_new(reached or self.now)
        self.db.info.pop("jury_events", None)
        await self.db.commit()

    # ------------------------------------------------------------ 委员

    def others(self, count: int, hours: float, exclude: tuple[str, ...] = ()) -> list[str]:
        """轮流挑几位别的委员来投票，跳过那个时间点已经不能评审的。"""
        picked: list[str] = []
        index = self._rotation
        while len(picked) < count:
            name = OTHER_JURORS[index % len(OTHER_JURORS)]
            index += 1
            if name in exclude or name in picked:
                continue
            if (name == "suspended" and hours <= SUSPENDED_AT_HOURS + 4) or (name == "mutedjuror" and hours <= MUTED_AT_HOURS + 4):
                continue
            picked.append(name)
        self._rotation = (self._rotation + 3) % len(OTHER_JURORS)
        return picked


# ====================================================================== 假数据：基础（账号、群、动态、委员任命）


async def seed_base(s: Seeder) -> None:
    db = s.db
    write_demo_images()
    password_hash = hash_password(PASSWORD)
    # (用户名, 注册距今天数, 是否绑定 CCW)
    profiles = {
        "admin": (400, True), "juror": (300, True), "trainee1": (70, True), "trainee2": (65, True), "regular2": (200, True),
        "senior1": (380, True), "senior2": (350, True), "senior3": (330, True), "suspended": (150, True), "mutedjuror": (120, True),
        "expired": (250, True), "eligible": (120, True), "newbie": (2, False), "applicant": (90, True),
        "reporter1": (210, True), "reporter2": (180, False), "reporter3": (160, True), "reporter4": (95, False),
        "offender": (180, True), "appellant": (140, False),
        "hesitant": (110, True), "cooldown": (100, True), "eligible2": (75, True), "examfail": (60, True), "exampass": (85, True), "examout": (130, True),
    }
    for index, (username, nickname, _role, _use) in enumerate(ACCOUNTS):
        if username in PHASE3_ACCOUNTS:
            continue
        days, bound = profiles[username]
        db.add(
            User(
                username=username,
                hashed_password=password_hash,
                nickname=nickname,
                bio="风纪系统联调用的演示账号",
                platform_role=PlatformRole.ADMIN if username == "admin" else PlatformRole.USER,
                ccw_student_oid=f"demo{index:020d}" if bound else None,
                ccw_name=nickname if bound else None,
                created_at=s.ago(days=days),
                updated_at=s.ago(days=days),
            )
        )
    first = ["像素", "代码", "星星", "积木", "月光", "云朵", "闪电", "奶茶", "橘子", "薄荷", "雪球", "风铃", "番茄", "海盐", "可乐", "松果", "咖喱", "芒果", "布丁", "电波"]
    second = ["小王子", "制作人", "画师", "程序猿", "收藏家", "玩家", "冒险家", "学徒", "研究员", "搬运工", "设计师", "鼓手", "骑士", "旅人", "工坊"]
    for index in range(1, BULK_USERS + 1):
        days = 40 + (index * 7) % 360
        db.add(
            User(
                username=f"m{index:03d}",
                hashed_password=password_hash,
                nickname=f"{first[index % len(first)]}{second[(index // len(first) + index) % len(second)]}",
                created_at=s.ago(days=days),
                updated_at=s.ago(days=days),
            )
        )
    await db.flush()

    for key, (title, join_mode, description) in CONVERSATIONS.items():
        created_at = s.ago(days=3) if key == "weekend" else s.ago(days=420)
        db.add(Conversation(type=ConversationType.GROUP, title=title, description=description, join_mode=join_mode, created_at=created_at, updated_at=created_at))
    await db.flush()
    await s.load()

    def join(conversation: str, username: str, role: Any = None, days: float = 200) -> None:
        db.add(
            ConversationMember(
                conversation_id=s.conversation_ids[conversation],
                user_id=s.uid(username),
                role=role or MemberRole.MEMBER,
                joined_at=s.ago(days=min(days, profiles.get(username, (days, False))[0])),
            )
        )

    # 大厅：所有人都在（委员也在，但他们从不在大厅说话，所以不会因为「快照里的发言人」回避）
    join("plaza", "m001", MemberRole.OWNER, 420)
    for index in range(2, BULK_USERS + 1):
        join("plaza", f"m{index:03d}", days=min(30 + (index * 5) % 300, 40 + (index * 7) % 360))
    for username, _nickname, _role, _use in ACCOUNTS:
        if username not in PHASE3_ACCOUNTS:
            join("plaza", username, MemberRole.ADMIN if username == "admin" else None)
    for key, (owner, offenders, chatters, reporters, extra) in SMALL_GROUPS.items():
        days = 2.5 if key == "weekend" else 150
        join(key, owner, MemberRole.OWNER, days)
        for username in [*offenders, *chatters, *reporters, *extra]:
            join(key, username, days=days)
    join("lounge", "m008", MemberRole.OWNER, 300)
    for username in ACTIVE_CHATTERS:
        join("lounge", username, days=60)
    # 好友：regular2 和 offender 是好友（offender 的案件对 regular2 回避）
    for left, right in (("regular2", "offender"), ("juror", "senior1"), ("eligible", "reporter1")):
        db.add(Friendship(user_id=s.uid(left), friend_id=s.uid(right), created_at=s.ago(days=50)))
        db.add(Friendship(user_id=s.uid(right), friend_id=s.uid(left), created_at=s.ago(days=50)))
    await db.commit()

    # 闲聊群里的日常发言：每人近 30 天 34 条，满足「近 30 天发言不少于 30 条」
    chatter: list[tuple[datetime, str, str]] = []
    for username in ACTIVE_CHATTERS:
        for _ in range(34):
            chatter.append((s.ago(hours=s.rng.uniform(1, 27 * 24)), username, s.take("filler", FILLER)))
    for at, username, text in sorted(chatter, key=lambda item: item[0]):
        await s.message("lounge", username, text, at)
    # 大厅里很久以前的闲聊，newbie 最近的几句
    for index, (who, text) in enumerate(s.filler(40, PLAZA_CHATTERS)):
        await s.message("plaza", who, text, s.ago(days=82) + timedelta(minutes=3 * index))
    await db.commit()

    # 正常的公开动态
    ordinary = [
        ("m250", "终于把联机大厅做出来了，放个截图", 1, 26), ("eligible", "学了一周克隆体，做了个弹幕小游戏，欢迎来虐", 0, 19),
        ("m262", "求助：角色碰到边缘就卡住怎么办？", 0, 15), ("juror", "分享一个做存档的思路：用列表记录每一关的状态", 2, 11),
        ("m271", "今天的像素画练习，画了一只猫", 1, 8), ("senior1", "提醒一下：发作品别忘了写清楚素材来源", 0, 6),
        ("m283", "比赛作品提交啦，祝大家都能拿奖", 0, 3), ("reporter1", "有人一起做解谜游戏吗，缺关卡设计", 0, 1),
    ]
    for author, content, images, days in ordinary:
        await s.post(author, content, images, s.ago(days=days))

    # 委员任命：走后台的任命流程（jury_service.review_member），再把时间改到过去
    admin = await s.user("admin")
    for username, tier, days, term_left in APPOINTMENTS:
        member = await jury_service.review_member(db, s.uid(username), action="approve", actor_id=admin.id, note="创始任命", tier=tier)
        member.statement = "想帮忙把社区环境维护得更好"
        member.applied_at = s.ago(days=days + 1)
        member.reviewed_at = s.ago(days=days)
        member.term_ends_at = s.now + timedelta(days=term_left)
        if username == "regular2":
            # 他的在任天数从一开始就是真的：之后票数和一致率够了，会由系统自动晋升为正式委员。
            # 其他人的在任天数留到最后再改（seed_phase2），免得灌历史数据的过程中被一路自动晋升。
            member.serving_since = s.ago(days=days)
        await s.stamp_new(s.ago(days=days))
        await db.commit()
    # expired：任期三天前届满。他十天前有一条违规记录，所以没有自动续任（没有违规记录的会自动续任，状态就不是 expired 了）
    outcome = await penalty_service.record_violation(
        db, user_id=s.uid("expired"), category="flooding", level=1, source="admin", actor_id=admin.id, summary="在大厅连续刷屏", reason="管理员巡查时处置"
    )
    await db.commit()
    await penalty_service.flush_notifications(db)
    outcome.violation.created_at = s.ago(days=10)
    outcome.violation.expires_at = s.ago(days=10) + timedelta(days=penalty_service.POINT_WINDOW_DAYS)
    await s.stamp_new(s.ago(days=10))
    await db.commit()
    if await jury_service.renew_expired_terms(db) != 1:
        raise RuntimeError("expired 应该在这里被标记为任期届满未续任")
    await s.stamp_new(s.ago(days=3))
    await db.commit()
    # applicant：自己提交的申请（会真的校验四项条件），等管理员审批
    applicant = await jury_service.apply(db, await s.user("applicant"), "我每天都在线，想为社区出一份力")
    applicant.applied_at = s.ago(hours=31)
    await db.commit()


# ====================================================================== 假数据：一期（案件、投票、观点、公示、改判、处罚）


async def seed_phase1(s: Seeder) -> None:
    db = s.db
    await s.load()
    golden_ids: list[int] = []
    # 已核对、且不在练习那一份里的历史题：评审中的质控题只从这一份里出
    scored: list[int] = []
    steps: list[tuple[float, Any]] = []
    admin = await s.user("admin")

    # ---- 题库里最早的一批题：上线前管理员结掉的历史案件，从没进过委员队列。其中大部分后来由管理员核对过，一部分写了讲解
    async def golden_case(index: int, kind: str, outcome: str) -> None:
        hours = 75 * D - index * 14
        case_id = await s.scenario_case(kind, "plaza" if kind == "msg" else None, outcome, s.ago(hours=hours + 20))
        case = await s.case(case_id)
        case.queue = "admin"
        await db.commit()
        case = await s.admin_resolve(case_id, s.ago(hours=hours), upheld=outcome == "v", note="历史举报，管理员核实后处理" if outcome == "v" else "历史举报，核实后未发现违规")
        if not case.is_golden:
            raise RuntimeError(f"历史案件 {case_id} 应该进了题库")
        # 来源记成 legacy：服务层只有上线时的回填会写这个值，这里模拟的就是那批回填的题
        case.golden_source = "legacy"
        await db.commit()
        if index % 5 != 4:
            changes: dict[str, Any] = {"verified": True}
            if index % 2 == 0:
                changes["explanation"] = EXPLANATIONS[case.primary_reason] if outcome == "v" else EXPLANATIONS["clean"]
            await bank_service.update_question(db, case_id, actor_id=admin.id, changes=changes)
            await s.stamp_new(s.ago(hours=hours - 40))
            await db.commit()
            if bank_service.question_pool(await s.case(case_id)) == "scored":
                scored.append(case_id)
        golden_ids.append(case_id)

    for index, (kind, outcome) in enumerate(GOLDEN):
        steps.append((75 * D - index * 14, partial(golden_case, index, kind, outcome)))

    async def answer_quality(position: int, juror: str, right: bool, hours: float, note: str | None = None) -> None:
        """这位委员在评审中遇到一道质控题。position 是 scored 里的第几道。"""
        case = await s.case(scored[position])
        violation = bank_service.question_upheld(case)
        vote = ("l2" if violation else "none") if right else ("none" if violation else "l1")
        await s.vote(case.id, juror, vote, s.ago(hours=hours), note, quality=True)

    # 主账号答过 8 道（错 1 道）；别的委员零星答过几道
    for position, (hours, right) in enumerate(
        [(27 * D + 5, True), (22 * D + 3, True), (18 * D + 2, False), (13 * D + 4, True), (11 * D + 6, True), (9 * D + 7, True), (7 * D + 2, True), (5 * D + 2, True)]
    ):
        steps.append((hours, partial(answer_quality, position, "juror", right, hours, "看着像玩笑，没想到算违规" if not right else None)))
    for position, juror, right, hours in [
        (8, "senior1", True, 30 * D), (9, "senior1", True, 21 * D), (10, "senior1", True, 11 * D),
        (0, "trainee1", True, 19 * D), (1, "trainee1", False, 14 * D), (2, "trainee1", True, 9 * D), (3, "trainee1", True, 4 * D),
        (4, "regular2", True, 25 * D), (5, "regular2", True, 8 * D + 3),
    ]:
        steps.append((hours, partial(answer_quality, position, juror, right, hours)))

    # suspended：5 道质控题错了 4 道，由系统自动暂停（jury_service 里的规则，不是手工改状态），并收到一条通知
    async def suspend_by_quality(position: int, right: bool, hours: float, last: bool) -> None:
        await answer_quality(position, "suspended", right, hours)
        if last:
            member = await jury_service.get_member(db, s.uid("suspended"))
            await db.refresh(member)
            if member.status != "suspended":
                raise RuntimeError("suspended 应该已经被系统自动暂停")
            member.reviewed_at = s.ago(hours=hours)
            await db.commit()

    for order, (position, right, hours) in enumerate([(11, False, 12 * D), (12, True, 10 * D + 12), (13, False, 9 * D + 5), (14, False, 8 * D), (15, False, SUSPENDED_AT_HOURS)]):
        steps.append((hours, partial(suspend_by_quality, position, right, hours, order == 4)))

    # ---- 委员共识定案的案件
    async def history_case(index: int, hours: float, kind: str, place: str | None, outcome: str, size: int, juror_mode: str | None) -> None:
        closed_at = s.ago(hours=hours)
        reported_at = closed_at - timedelta(hours=SPANS[index % len(SPANS)])
        case_id = await s.scenario_case(kind, place, outcome, reported_at, reporters=2 if index % 5 == 2 else 1)
        await s.triage(case_id, reported_at + timedelta(minutes=1))
        exclude = ("trainee2",) if place == "pixel" else ()
        majority = list(LEVELS_3[index % len(LEVELS_3)] if size == 3 else LEVELS_4[index % len(LEVELS_4)]) if outcome == "v" else ["none"] * (3 if size == 3 else 4)
        minority = "none" if outcome == "v" else ("l1", "l2")[index % 2]
        if juror_mode == "disagree" and size != 5:
            raise RuntimeError("主账号投了不一致的票时必须是 5 票的案件")
        # 先排好每个席位投什么，再把主账号放进去
        slots: list[list[Any]] = [[None, vote] for vote in majority]
        if size == 5:
            slots.insert(1, [None, minority])  # 少数票必须在前三张里，否则前三张一致就直接定案了
        if juror_mode == "agree":
            choices = [position for position, slot in enumerate(slots) if not (size == 5 and position == 1)]
            slots[choices[index % len(choices)]][0] = "juror"
        elif juror_mode == "disagree":
            slots[1][0] = "juror"
        if juror_mode == "abstain":
            slots.insert(1, ["juror", "abstain"])
        elif index % 6 == 4:
            slots.insert(1, [None, "abstain"])  # 偶尔有别的委员弃权，让票数分布有变化
        names = iter(s.others(sum(1 for slot in slots if slot[0] is None), hours, exclude))
        for slot in slots:
            slot[0] = slot[0] or next(names)
        result: dict[str, Any] = {}
        for position, (name, vote) in enumerate(slots):
            at = reported_at + (closed_at - reported_at) * ((position + 1) / len(slots))
            note = None
            if vote != "abstain" and ((name == "juror" and (index % 2 == 0 or juror_mode == "disagree")) or (name != "juror" and (index + position) % 3 == 0)):
                note = s.take("note:c", NOTES_CLEAN) if vote == "none" else s.take("note:v", NOTES_VIOLATION)
            result = await s.vote(case_id, name, vote, at, note)
        if not result.get("settled"):
            raise RuntimeError(f"案件 {case_id} 应该在最后一票定案")
        await s.stamp_closed(case_id, closed_at)
        # 观点区里互相点几个赞
        voters = [name for name, _vote in slots]
        juror_note = await s.note_id(case_id, "juror") if "juror" in voters else None
        if juror_note is not None and index % 3 != 1:
            for name in [name for name in voters if name != "juror"][: 1 + index % 2]:
                await s.like(juror_note, name)
        if "juror" in voters and index % 4 == 1:
            for name in voters:
                other_note = await s.note_id(case_id, name) if name != "juror" else None
                if other_note is not None:
                    await s.like(other_note, "juror")
                    break

    for index, (hours, kind, place, outcome, size, juror_mode) in enumerate(HISTORY):
        steps.append((hours, partial(history_case, index, hours, kind, place, outcome, size, juror_mode)))

    # ---- 特别安排的案件
    special: dict[str, int] = {}

    async def offender_case() -> None:
        """offender 在小群里骂人，委员认定违规：他名下有一条可以申诉的处罚。"""
        closed_at = s.ago(hours=5 * D + 6)
        reported_at = closed_at - timedelta(hours=9)
        case_id = await s.message_case(
            conversation="code", offender="offender", reporters=["reporter3", "m050"], reason="辱骂骚扰", reported_at=reported_at,
            target="问这么蠢的问题，你是没长脑子吗？自己不会查？",
            before=[("A", "请问列表怎么按分数排序呀"), ("B", "可以用冒泡排序，我找个例子")],
            after=[("A", "我查了没看懂才来问的……"), ("B", "别这样说话，大家都是从不会开始的")],
        )
        await s.triage(case_id, reported_at + timedelta(minutes=1))
        # regular2 是 offender 的好友，要回避
        for position, (name, vote, note) in enumerate([("senior2", "l2", "对方是正常提问，他上来就人身攻击"), ("juror", "l2", "同意，而且被提醒之后也没有收敛"), ("trainee1", "l1", None)]):
            await s.vote(case_id, name, vote, reported_at + timedelta(hours=3 * (position + 1)), note)
        await s.stamp_closed(case_id, closed_at)
        special["offender"] = case_id

    async def appellant_case() -> None:
        """appellant 的动态被认定为广告，他提交了申诉、还没处理：这个案件暂时不公示。"""
        closed_at = s.ago(hours=6 * D + 2)
        reported_at = closed_at - timedelta(hours=14)
        case_id = await s.post_case(
            author="appellant", reporters=["reporter2", "m207"], reason="垃圾广告", reported_at=reported_at, images=1,
            content="我的工作室开张啦！定制头像 5 元一张、游戏立绘 20 元起，要的私聊，支持先看稿",
        )
        await s.triage(case_id, reported_at + timedelta(minutes=1))
        for position, (name, vote, note) in enumerate([("senior1", "l2", "明码标价拉生意，属于广告"), ("juror", "l1", "是广告，但只是个人接稿，我给轻微"), ("senior3", "l2", None)]):
            await s.vote(case_id, name, vote, reported_at + timedelta(hours=4 * (position + 1)), note)
        case = await s.stamp_closed(case_id, closed_at)
        violation = await penalty_service.submit_appeal(db, s.uid("appellant"), case.violation_id, "我只是分享自己接稿的消息，没有反复刷屏，也没有骗人，不应该算垃圾广告")
        violation.appealed_at = s.ago(hours=5 * D)
        await db.commit()
        special["appellant"] = case_id

    async def overturned_to_clean() -> None:
        """委员认定违规，两天后管理员改判为不违规（群聊消息）。主账号投的是违规，改判后变成「不一致」。"""
        closed_at = s.ago(hours=15 * D + 4)
        reported_at = closed_at - timedelta(hours=12)
        offender = s.offender_for("plaza")
        case_id = await s.message_case(
            conversation="plaza", offender=offender, reporters=s.reporters_for("plaza", 2), reason="辱骂骚扰", reported_at=reported_at, pad_before=5, pad_after=2,
            target="你这个大笨蛋！说好今天一起肝活动的，又放我鸽子，看我明天不揍你",
            before=[("B", "今天活动最后一天了"), ("X", "我等了一晚上")],
            after=[("B", "哈哈哈你俩又开始了"), ("C", "感情真好")],
        )
        await s.triage(case_id, reported_at + timedelta(minutes=1))
        for position, (name, vote, note) in enumerate([("trainee1", "l1", "说要揍人，算威胁吧"), ("juror", "l2", "有辱骂也有威胁"), ("regular2", "l1", None)]):
            await s.vote(case_id, name, vote, reported_at + timedelta(hours=4 * (position + 1)), note)
        await s.stamp_closed(case_id, closed_at)
        await s.overturn(case_id, s.ago(hours=13 * D + 2), upheld=False, note="两人是现实中的朋友，结合上下文是朋友间的玩笑，被“威胁”的一方也确认没有被冒犯")
        special["overturned_clean"] = case_id

    async def overturned_to_violation() -> None:
        """委员 4:1 认定不违规，一天后管理员改判为违规（公开动态）。主账号是当时唯一投违规的。"""
        closed_at = s.ago(hours=9 * D + 9)
        reported_at = closed_at - timedelta(hours=20)
        case_id = await s.post_case(
            author=s.offender_for(None), reporters=s.reporters_for(None, 2), reason="垃圾广告", reported_at=reported_at, images=1,
            content="内部渠道拿到一批会员兑换码，比官方便宜一半，要的评论区扣 1，我私聊你",
        )
        await s.triage(case_id, reported_at + timedelta(minutes=1))
        ballots = [("senior2", "none", "只是分享优惠信息"), ("juror", "l2", "来路不明的兑换码，实际是在卖东西"), ("trainee2", "none", None), ("senior3", "none", None), ("regular2", "none", None)]
        for position, (name, vote, note) in enumerate(ballots):
            await s.vote(case_id, name, vote, reported_at + timedelta(hours=3.8 * (position + 1)), note)
        await s.stamp_closed(case_id, closed_at)
        await s.overturn(case_id, s.ago(hours=8 * D + 3), upheld=True, category="advertising", level=2, note="多名用户反馈付款后没有收到兑换码，核实后认定为违规售卖")
        special["overturned_violation"] = case_id

    async def no_consensus(key: str, *, kind: str, hours_escalated: float, ballots: list[tuple[str, str, str | None]], resolve: tuple[float, bool, str | None, int | None, str] | None, **content: Any) -> None:
        """五票没有达到 4 票一致：转给管理员。resolve 不为空时管理员随后定案。"""
        escalated_at = s.ago(hours=hours_escalated)
        reported_at = escalated_at - timedelta(hours=16)
        if kind == "post":
            case_id = await s.post_case(reported_at=reported_at, **content)
        else:
            case_id = await s.message_case(reported_at=reported_at, **content)
        await s.triage(case_id, reported_at + timedelta(minutes=1))
        for position, (name, vote, note) in enumerate(ballots):
            await s.vote(case_id, name, vote, reported_at + (escalated_at - reported_at) * ((position + 1) / len(ballots)), note)
        case = await s.case(case_id)
        if (case.queue, case.jury_outcome) != ("admin", "no_consensus"):
            raise RuntimeError(f"案件 {case_id} 应该因为没有共识转给管理员")
        case.escalated_at = escalated_at
        case.updated_at = escalated_at
        await db.commit()
        if resolve is not None:
            hours, upheld, category, level, note = resolve
            await s.admin_resolve(case_id, s.ago(hours=hours), upheld=upheld, category=category, level=level, note=note)
        special[key] = case_id

    async def timeout_case(key: str, *, hours_queued: float, ballots: list[tuple[str, str]], resolve_hours: float | None) -> None:
        """进委员队列 72 小时没凑够票：由定时任务的同一段逻辑转出去。
        原审里没有资深委员的（timeout_open）凑得齐合议庭，进资深合议；有资深委员投过票的（timeout_closed）凑不齐，转管理员。"""
        queued_at = s.ago(hours=hours_queued)
        case_id = await s.scenario_case("msg", "plaza", "v", queued_at - timedelta(minutes=1))
        await s.triage(case_id, queued_at)
        for position, (name, vote) in enumerate(ballots):
            await s.vote(case_id, name, vote, queued_at + timedelta(hours=5 * (position + 1)))
        if await case_service.escalate_stale_jury_cases(db) != 1:
            raise RuntimeError("超时转出的案件数不对：此刻委员队列里应该只有这一个超时的案件")
        case = await s.case(case_id)
        case.escalated_at = queued_at + timedelta(hours=case_service.JURY_TIMEOUT_HOURS)
        case.updated_at = case.escalated_at
        if case.queue == "panel":
            # 合议的 48 小时从转出那一刻算起
            case.panel_at = case.escalated_at
        await db.commit()
        if resolve_hours is not None:
            await s.admin_resolve(case_id, s.ago(hours=resolve_hours), upheld=True, note="委员评审超时，管理员核实后认定违规")
        special[key] = case_id

    async def manual_case(hours: float, outcome: str) -> None:
        """还在委员队列里时管理员直接接手结案（没有委员投过票，不进公示）。"""
        closed_at = s.ago(hours=hours)
        case_id = await s.scenario_case("msg", "plaza", outcome, closed_at - timedelta(hours=6))
        await s.triage(case_id, closed_at - timedelta(hours=6) + timedelta(minutes=1))
        await s.admin_resolve(case_id, closed_at, upheld=outcome == "v", note="管理员巡查时直接处理")

    steps += [
        (5 * D + 6, offender_case),
        (6 * D + 2, appellant_case),
        (15 * D + 4, overturned_to_clean),
        (9 * D + 9, overturned_to_violation),
        (
            21 * D,
            partial(
                no_consensus, "privacy", kind="post", hours_escalated=21 * D + 10,
                # 隐私类：委员意见分歧，管理员按「泄露隐私」定案。公示栏里这类案件不带内容
                author="m198", reporters=["reporter4", "m211", "m212"], reason="违法违规", images=0,
                content="曝光一下：昨天在群里和我吵架的那个人，真名叫张某某，在某某中学初二（3）班，家住某某小区，大家认准了",
                ballots=[("juror", "l3", "把别人的真实姓名、学校、住址都挂出来了，性质很严重"), ("trainee1", "none", "只是在吐槽吵架的事吧"), ("senior1", "l2", None), ("regular2", "none", None), ("senior2", "l1", "不确定这些信息是不是真的")],
                resolve=(21 * D, True, "privacy", 3, "公开他人真实姓名、学校和住址，属于泄露隐私"),
            ),
        ),
        (
            12 * D + 8,
            partial(
                no_consensus, "nc_clean", kind="msg", hours_escalated=12 * D + 15,
                conversation="plaza", offender="m197", reporters=["m213"], reason="辱骂骚扰", pad_before=6, pad_after=3,
                target="你这个设计思路完全是错的，再这么做下去就是浪费时间，建议推倒重来",
                before=[("A", "我打算把所有逻辑都写在一个角色里"), ("B", "那会很乱吧")], after=[("A", "说话能不能客气点"), ("C", "他说得也有道理")],
                ballots=[("senior3", "l1", "太不留情面了"), ("juror", "none", "只是批评得直接，没有骂人"), ("trainee2", "l1", None), ("senior1", "none", None), ("mutedjuror", "none", None)],
                resolve=(12 * D + 8, False, None, None, "属于正常的批评意见，没有人身攻击"),
            ),
        ),
        (
            60,
            partial(
                no_consensus, "nc_recent", kind="msg", hours_escalated=66,
                conversation="plaza", offender="m196", reporters=["m214", "m215"], reason="垃圾广告", pad_before=4, pad_after=2,
                target="我开了个付费答疑群，9.9 永久，Scratch 的问题随便问，二维码私聊找我要",
                before=[("A", "有没有人能教教我做联机")], after=[("B", "这算广告吗"), ("C", "收费的就算吧")],
                ballots=[("senior1", "l2", "收费拉群就是广告"), ("juror", "none", "只是提供帮助顺带收费，没有刷屏"), ("senior2", "l1", None), ("trainee1", "none", None), ("senior3", "l2", None)],
                resolve=(60, True, "advertising", 1, "在公开群里招揽付费群，按轻微广告处理"),
            ),
        ),
        (16 * D, partial(timeout_case, "timeout_closed", hours_queued=20 * D, ballots=[("senior3", "l2")], resolve_hours=16 * D)),
        (26 * D, partial(manual_case, 26 * D, "v")),
        (10 * D + 6, partial(manual_case, 10 * D + 6, "c")),
        (76, partial(timeout_case, "timeout_open", hours_queued=76, ballots=[("regular2", "l1")], resolve_hours=None)),
    ]

    # mutedjuror：20 小时前被管理员禁言 14 天（在任，但受处罚期间不能评审）
    async def mute_juror() -> None:
        admin = await s.user("admin")
        at = s.ago(hours=MUTED_AT_HOURS)
        outcome = await penalty_service.record_violation(
            db, user_id=s.uid("mutedjuror"), category="harassment", level=2, source="admin", actor_id=admin.id,
            summary="在大厅连续多次辱骂其他成员", reason="管理员巡查时处置", override=penalty_service.Penalty("mute", 24 * 14),
        )
        await db.commit()
        await penalty_service.flush_notifications(db)
        violation = outcome.violation
        violation.created_at = at
        violation.expires_at = at + timedelta(days=penalty_service.POINT_WINDOW_DAYS)
        violation.penalty_ends_at = at + timedelta(days=14)
        user = await s.user("mutedjuror")
        user.muted_until = violation.penalty_ends_at
        await s.stamp_new(at)
        await db.commit()

    steps.append((MUTED_AT_HOURS, mute_juror))

    # 按时间从早到晚依次重放。每一步都是完整的（建案到结案），所以任一时刻委员队列里没有别的旧案件。
    for _hours, step in sorted(steps, key=lambda item: -item[0]):
        await step()

    await seed_phase1_open_cases(s, special)


async def seed_phase1_open_cases(s: Seeder, special: dict[str, int]) -> None:
    """现在还没结的案件：主账号投过票的、委员队列里等着评审的、管理员队列里的。"""
    db = s.db
    sample = lambda name: f"/uploads/messages/{name}"  # noqa: E731

    async def queued(case_id: int, hours: float) -> int:
        await s.triage(case_id, s.ago(hours=hours) + timedelta(minutes=1))
        return case_id

    # ---- P1：主账号已投票、还没定案，观点区里什么情况都有
    p1 = await queued(
        await s.message_case(
            conversation="plaza", offender="m190", reporters=["reporter2", "m220"], reason="辱骂骚扰", reported_at=s.ago(hours=11), pad_before=5, pad_after=3,
            target="就你这水平还出教程教别人？回去再练十年吧，垃圾",
            before=[("A", "我写了一篇克隆体的入门教程，新手可以看看"), ("B", "写得挺清楚的"), ("X", "别出来误人子弟了")],
            after=[("A", "哪里写错了可以指出来，没必要这样"), ("B", "有问题就说问题"), ("C", "@管理 这边有人骂人")],
        ),
        11,
    )
    await s.vote(p1, "senior1", "l2", s.ago(hours=9), "针对个人的贬低，对方只是在好心分享教程")
    await s.vote(p1, "trainee1", "none", s.ago(hours=8), "感觉只是说话冲，没到违规的程度")
    await s.vote(p1, "juror", "l1", s.ago(hours=6), "有攻击性，但只有这一句，我倾向轻微")
    await s.vote(p1, "regular2", "abstain", s.ago(hours=5.5))
    note_senior1, note_trainee1, note_juror = [await s.note_id(p1, name) for name in ("senior1", "trainee1", "juror")]
    # 被点赞的
    await s.like(note_senior1, "juror")
    await s.like(note_senior1, "trainee1")
    await s.like(note_senior1, "regular2")
    await s.like(note_juror, "senior1")
    # 回复、回复别人的回复
    reply_juror = await s.opinion(p1, "juror", "他前面还说了一句“别出来误人子弟”，我觉得已经是针对人了", s.ago(hours=5.4), note_trainee1)
    await s.opinion(p1, "senior1", "同意，结合上一句看就不是单纯的嘴臭", s.ago(hours=5.1), reply_juror)
    await s.opinion(p1, "trainee1", "好吧，我没注意到上一句", s.ago(hours=4.8), note_trainee1)
    # 被两名委员举报后自动折叠的
    exposed = await s.opinion(p1, "regular2", "这种人就该直接封号，他以前在别的群也这样，我认识他，就是那个叫……的", s.ago(hours=4.5))
    await opinion_service.report_opinion(db, await s.user("senior1"), exposed, "泄露身份或隐私", None)
    await opinion_service.report_opinion(db, await s.user("trainee1"), exposed, "其他", "在猜被举报人是谁，和案件本身无关")
    # 本人删除的：下面有回复的留一个占位，没有回复的直接不出现
    withdrawn = await s.opinion(p1, "senior1", "补充：建议大家统一按轻微处理", s.ago(hours=4.2))
    await s.opinion(p1, "juror", "为什么是轻微？上面不是说针对个人了吗", s.ago(hours=4.0), withdrawn)
    await opinion_service.delete_opinion(db, await s.user("senior1"), withdrawn)
    mistake = await s.opinion(p1, "trainee1", "手滑发错了", s.ago(hours=3.9))
    await opinion_service.delete_opinion(db, await s.user("trainee1"), mistake)
    special["discussion"] = p1

    # ---- P2 / P3：主账号投过票的另外两个未结案件
    p2 = await queued(
        await s.post_case(author="m191", reporters=["reporter4"], reason="垃圾广告", reported_at=s.ago(hours=22), images=0, content="收徒！手把手教你做爆款游戏，学费 9.9，先交钱后拉群，名额只有 10 个"),
        22,
    )
    await s.vote(p2, "juror", "none", s.ago(hours=20), "更像是个人收费教学，没到垃圾广告的程度？不太确定")
    await s.vote(p2, "senior2", "l2", s.ago(hours=18), "先交钱后拉群，明显是在引流收费")
    p3 = await queued(
        await s.message_case(
            conversation="code", offender="m043", reporters=["reporter4"], reason="其他", details="反复在群里刷同样的内容，影响其他人讨论问题", reported_at=s.ago(hours=6),
            target="哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈",
            before=[("B", "有人知道怎么读取云变量吗"), ("X", "哈哈哈哈哈哈哈哈"), ("C", "先看看文档"), ("X", "哈哈哈哈哈哈哈哈哈哈哈哈")], after=[("B", "能不能别刷了")],
        ),
        6,
    )
    await s.vote(p3, "juror", "abstain", s.ago(hours=3))
    await s.vote(p3, "senior3", "none", s.ago(hours=2), "有点吵，但算不上违规")
    special["voted_post"], special["voted_abstain"] = p2, p3

    # ---- N1：主账号投过票，五票没有共识，现在在管理员队列里等着（详情里 stage 是 admin）
    n1 = await queued(
        await s.message_case(
            conversation="plaza", offender="m192", reporters=["m221"], reason="违法违规", reported_at=s.ago(hours=16), pad_before=4, pad_after=2,
            target="谁要游戏激活码生成器，亲测能用，免费分享，要的私聊", before=[("A", "那个游戏好贵啊")], after=[("B", "这是盗版工具吧"), ("C", "免费分享也不行吗")],
        ),
        16,
    )
    ballots = [("senior2", "l2", None, 14), ("trainee2", "none", None, 12), ("juror", "l2", "生成器就是盗版工具，免费也一样", 9), ("senior3", "none", "没看到他真的发出来", 7), ("trainee1", "l1", None, 5)]
    for name, vote, note, hours in ballots:
        await s.vote(n1, name, vote, s.ago(hours=hours), note)
    case = await s.case(n1)
    if (case.queue, case.jury_outcome) != ("admin", "no_consensus"):
        raise RuntimeError("N1 应该因为没有共识转给管理员")
    case.escalated_at = s.ago(hours=5)
    await db.commit()
    special["no_consensus_open"] = n1

    # ---- 委员队列里等着评审的案件（主账号都没投过）
    # Q1：多人举报、上下文很长；已有两张违规票，再来一张违规票就当场定案
    q1 = await queued(
        await s.message_case(
            conversation="plaza", offender="m193", reporters=["reporter1", "m222", "m223"], reason="辱骂骚扰", reported_at=s.ago(hours=10), pad_before=6, pad_after=6,
            target=DEMO_TARGETS["settle_violation"],
            before=[("A", "请问克隆体怎么删除自己呀，我试了好几次都不行"), ("B", "用「删除此克隆体」那块积木"), ("A", "放了，但是角色本体也没了"), ("B", "那是用错积木了，截个图看看")],
            after=[("A", "我是新手，不懂才问的……"), ("B", "问问题很正常，别理他"), ("X", "菜就多练，别在群里丢人"), ("C", "@管理 有人骂人")],
        ),
        10,
    )
    await s.vote(q1, "senior1", "l2", s.ago(hours=8), "对新手提问直接人身攻击，后面还补了一句")
    await s.vote(q1, "senior2", "l2", s.ago(hours=6))
    # Q2：被举报的是一条图片消息（三人举报，排得靠前，走查时早点能看到带图的案件）
    await queued(
        await s.message_case(
            conversation="plaza", offender="m194", reporters=["m224", "reporter4", "m233"], reason="垃圾广告", reported_at=s.ago(hours=7), pad_before=3, pad_after=2,
            target=sample("demo-poster.png"), target_options={"image": True},
            before=[("X", "有好东西给大家看")], after=[("B", "这是什么二维码"), ("X", "扫了就知道，稳赚不赔"), ("C", "广告吧这是")],
        ),
        7,
    )
    # Q3：文字带两张图；上下文里也有一条图片消息
    q3 = await queued(
        await s.message_case(
            conversation="plaza", offender="m195", reporters=["m225", "m234"], reason="垃圾广告", reported_at=s.ago(hours=5), pad_before=2, pad_after=1,
            target="加群看更多内部素材，下面两张是样品，群号在图里", target_options={"images": [sample("demo-sample-1.png"), sample("demo-sample-2.png")]},
            before=[("A", "今天拍的照片，分享一下"), ("A", sample("demo-photo.png"), {"image": True}), ("B", "好看")], after=[("B", "又来打广告")],
        ),
        5,
    )
    await s.vote(q3, "trainee1", "l2", s.ago(hours=4))
    # Q4：刚建的小群，上下文很短，两人举报
    await queued(
        await s.message_case(
            conversation="weekend", offender="m060", reporters=["reporter1", "m065"], reason="辱骂骚扰", reported_at=s.ago(hours=16),
            target="带你这种猪队友我宁可单机，上次就是你害我们输的，滚远点",
            before=[("A", "今晚八点联机，谁来"), ("B", "我我我")], after=[("A", "……上次我是第一次玩")],
        ),
        16,
    )
    # Q5：公开动态，两人举报；已有两张不违规票，再来一张不违规票就当场定案
    q5 = await queued(
        await s.post_case(author="m189", reporters=["reporter3", "m226"], reason="垃圾广告", reported_at=s.ago(hours=14), images=2, content=DEMO_TARGETS["settle_clean"]),
        14,
    )
    await s.vote(q5, "trainee1", "none", s.ago(hours=12), "看着是正常的抽奖活动，素材也是自己画的")
    await s.vote(q5, "senior3", "none", s.ago(hours=11))
    # Q6：公开动态，诈骗
    await queued(
        await s.post_case(author="m188", reporters=["m227"], reason="诈骗或钓鱼", reported_at=s.ago(hours=3), images=1, content="官方内测资格免费领！填写账号和密码即可登记：http://beta-signup.example"),
        3,
    )
    # Q7：理由是「其他」，上下文中等
    await queued(
        await s.message_case(
            conversation="plaza", offender="m187", reporters=["m228"], reason="其他", details="故意剧透还嘲讽没看过的人，很多人被影响了", reported_at=s.ago(hours=18), pad_before=3, pad_after=1,
            target="结局就是主角死了，没看过的活该哈哈哈", before=[("A", "新剧场版我周末去看，求别剧透"), ("B", "我也还没看")], after=[("A", "？？？"), ("B", "你故意的吧")],
        ),
        18,
    )
    # Q8：小群，上下文里有一条已撤回的消息（委员看不到它的内容）
    await queued(
        await s.message_case(
            conversation="code", offender="m042", reporters=["reporter3"], reason="辱骂骚扰", reported_at=s.ago(hours=20),
            target="抄我代码还不署名，你就是个小偷，全家都是小偷",
            before=[("A", "我参考了群文件里的寻路算法，做了个迷宫"), ("X", "那是我写的"), ("X", "（一句已经被撤回的话）", {"recalled": True}), ("A", "我在作品简介里写了参考来源的"), ("B", "有话好好说"), ("C", "先看看简介再说")],
            after=[("A", "你可以去看作品简介，我写了的"), ("B", "骂人家人就过分了"), ("C", "冷静一下")],
        ),
        20,
    )
    # Q9：公开动态，看起来只是发牢骚
    await queued(
        await s.post_case(author="m186", reporters=["m229"], reason="辱骂骚扰", reported_at=s.ago(hours=21), images=0, content="说实话，这届比赛前三名的作品都不如去年的，评委是不是放水了"),
        21,
    )
    # Q10：已有四张票 2:2，再来一张就凑满五票、没有共识、转给管理员
    q10 = await queued(
        await s.message_case(
            conversation="plaza", offender="m185", reporters=["m230"], reason="诈骗或钓鱼", reported_at=s.ago(hours=12), pad_before=4, pad_after=2,
            target=DEMO_TARGETS["no_consensus"], before=[("A", "会员好贵，有没有便宜点的办法")], after=[("B", "先付款？不怕被骗吗"), ("X", "信誉保证，老用户了")],
        ),
        12,
    )
    for name, vote, hours in [("senior1", "l2", 10), ("senior2", "none", 9), ("senior3", "l2", 7), ("regular2", "none", 4)]:
        await s.vote(q10, name, vote, s.ago(hours=hours))
    # Q11：出自「像素画社」。trainee2 是这个群的成员，分不到它（小群按成员回避）
    await queued(
        await s.message_case(
            conversation="pixel", offender="m023", reporters=["reporter2"], reason="辱骂骚扰", reported_at=s.ago(hours=9),
            target="画成这样也敢发群里？手残就别学人画画了，看着都辣眼睛",
            before=[("A", "临摹了一张像素小屋，大家看看哪里能改"), ("B", "屋顶的阴影可以再深一点")], after=[("A", "我才学了两周……"), ("B", "提意见可以，别人身攻击"), ("C", "画得挺好的，别理他")],
        ),
        9,
    )
    # Q12：被举报的是 offender 的动态。regular2 是 offender 的好友，分不到它
    await queued(
        await s.post_case(author="offender", reporters=["m232"], reason="辱骂骚扰", reported_at=s.ago(hours=13), images=0, content="某些人自己菜还到处举报别人，说的就是编程互助组里那几位，玻璃心就别上网"),
        13,
    )

    # ---- 管理员队列里的案件
    # 色情低俗：整类不进委员队列
    await queued(
        await s.message_case(
            conversation="plaza", offender="m184", reporters=["reporter2", "m231"], reason="色情低俗", reported_at=s.ago(hours=8), pad_before=3, pad_after=1,
            target="想看刺激的图就私聊我，懂的都懂，未成年别来", before=[("A", "有人一起画画吗")], after=[("B", "举报了")],
        ),
        8,
    )
    # 举报用户（带证据消息）
    plaza_id = s.conversation_ids["plaza"]
    evidence = []
    for index, text in enumerate(["加我好友，不加就一直 @ 你", "为什么不理我？", "再不回我就去你每个作品下面刷评论"]):
        evidence.append(await s.message("plaza", "m183", text, s.ago(hours=17) + timedelta(minutes=2 * index)))
    await db.commit()
    evidence_ids = [item.id for item in evidence]
    for item in evidence:
        db.expire(item)
    report = await s.report(
        "reporter1", ReportTargetType.USER, s.uid("m183"), "辱骂骚扰", s.ago(hours=15),
        conversation_id=plaza_id, details="这个人连续好几天在群里 @ 我、私下加好友骚扰，拒绝之后还威胁要去刷评论", evidence_message_ids=evidence_ids,
    )
    await queued(report.case_id, 15)
    # 举报群聊
    report = await s.report("reporter1", ReportTargetType.CONVERSATION, s.conversation_ids["weekend"], "违法违规", s.ago(hours=40), details="这个群里有人在发外挂下载链接，群主看到了也不管")
    await queued(report.case_id, 40)
    # 举报私聊消息：私聊不给委员看
    left, right = sorted((s.uid("reporter3"), s.uid("m182")))
    direct = Conversation(type=ConversationType.DIRECT, direct_key=f"{left}:{right}", created_by_id=s.uid("m182"), created_at=s.ago(days=4), updated_at=s.ago(hours=5))
    db.add(direct)
    await db.flush()
    for username in ("reporter3", "m182"):
        db.add(ConversationMember(conversation_id=direct.id, user_id=s.uid(username), role=MemberRole.MEMBER, joined_at=s.ago(days=4)))
    await db.commit()
    await queued(
        await s.message_case(
            conversation=direct.id, offender="m182", reporters=["reporter3"], reason="辱骂骚扰", reported_at=s.ago(hours=4),
            target="再不把账号借我，我就去你学校门口堵你", before=[("X", "把你的账号借我玩两天"), ("A", "不行，账号不能外借")], after=[("A", "你别这样")],
        ),
        4,
    )
    await s.stamp_new(s.now)
    await db.commit()
    # 记下几个有特别用途的案件编号，自测脚本和文档会用到
    db.add(SystemSetting(key="jury_demo.cases", value=json.dumps(special, ensure_ascii=False)))
    await db.commit()


# ====================================================================== 假数据：二期（题库、考试、练习、风纪值与层级、新的投票选项）


async def seed_phase2(s: Seeder) -> None:
    db = s.db
    await s.load()
    # 考试抽题用的是全局随机源：固定下来，每次重建抽到的题都一样
    random.seed(20261012)
    admin = await s.user("admin")
    juror_id = s.uid("juror")
    phase1 = json.loads((await db.get(SystemSetting, "jury_demo.cases")).value)
    special: dict[str, int] = {}

    # ---- 二期上线后的一批已定案案件：带类别的违规票、「上下文不足」、主动回避都出现过。
    # hesitant（弃权偏多）和 cooldown（弃权多到被暂停发案）几乎每个案件都参加；主账号从第 10 个起每天一案，连续天数就是这么来的。
    core = ["senior3", "regular2", "trainee1", "senior1", "senior2", "trainee2"]

    def available(name: str, hours: float) -> bool:
        # trainee2 是 12 天前才任命的
        return name != "trainee2" or hours < 12 * D - 6

    async def senior3_dissents() -> bool:
        """senior3 最近的一致率要落在降级线（70%）和预警线（80%）之间：还高就再投一张不一致的票，但不能跌破降级线。"""
        sample, rate = await points_service.recent_agreement(db, s.uid("senior3"), points_service.TIER_RULES["senior"]["demote_below"]["window"])
        return rate >= 0.77 and round(rate * sample) / (sample + 1) >= 0.72

    async def batch_case(
        k: int, closed_at: datetime, size: int, outcome: str, kind: str, juror: str | None = None, *, others: list[str] | None = None, span: float = 4.0, recused: bool = False
    ) -> int:
        hours = (s.now - closed_at).total_seconds() / 3600
        reported_at = closed_at - timedelta(hours=span)
        case_id = await s.scenario_case(kind, "plaza" if kind == "msg" else None, outcome, reported_at)
        case = await s.triage(case_id, reported_at + timedelta(minutes=1))
        default = case_service.reason_category(case.primary_reason)
        category = default if default in jury_service.VOTE_CATEGORIES else None
        undecided: list[tuple[str, str]] = []
        decided: list[str] = []
        if k not in (0, 2):
            if k % 3 == 1:
                undecided.append(("hesitant", "need_context" if k % 6 == 1 else "abstain"))
            else:
                decided.append("hesitant")
        if k % 5 in (0, 1, 3):
            undecided.append(("cooldown", "need_context" if k % 2 == 0 else "abstain"))
        else:
            decided.append("cooldown")
        seats = max(0, size - len(decided) - (1 if juror else 0))
        if others is None:
            pool = [name for name in core[k % len(core) :] + core[: k % len(core)] if available(name, hours)]
            if size == 5 and "senior3" in pool:
                pool.remove("senior3")
                pool.insert(0, "senior3")
            others = pool
        others = others[:seats]
        minority: str | None = None
        if size == 5:
            if juror == "disagree":
                minority = "juror"
            elif "senior3" in others and await senior3_dissents():
                minority = "senior3"
            else:
                # 其余案件里投了少数票的人：轮着来，不让 regular2 / trainee1 的一致率掉下去
                present = {*decided, *others}
                preferred = ["cooldown", "senior2", "hesitant", "trainee2", "senior1"]
                minority = next((name for name in preferred[k % 2 :] + preferred[: k % 2] if name in present), None) or others[-1]
        levels = ["l2", "l1", "l2", "l2", "l1"]

        def majority_vote(index: int) -> tuple[str, str | None]:
            if outcome == "c":
                return "none", None
            # 偶尔有人选了不一样的类别；定案类别取多数
            chosen = "profanity" if category == "harassment" and index == 1 and k % 4 == 0 else category
            return levels[(k + index) % len(levels)], chosen

        majority = [name for name in (*decided, *others) if name != minority]
        ballots: list[tuple[str, str, str | None]] = [(majority[0], *majority_vote(0))]
        if minority is not None:
            ballots.append((minority, *(("l1", category) if outcome == "c" else ("none", None))))
        ballots.extend((name, vote, None) for name, vote in undecided)
        ballots.extend((name, *majority_vote(index)) for index, name in enumerate(majority[1:], start=1))
        if juror == "agree":
            ballots.append(("juror", *majority_vote(4)))
        if recused:
            # trainee2 主动回避了这个案件：不占席位，不算投票
            await s.vote(case_id, "trainee2", "recuse", reported_at + timedelta(minutes=20), "被举报的人我可能认识")
        result: dict[str, Any] = {}
        for position, (name, vote, vote_category) in enumerate(ballots):
            at = reported_at + (closed_at - reported_at) * ((position + 1) / len(ballots))
            note = None
            if vote in jury_service.DECIDED_VOTES and ((name == "juror" and k % 2 == 0) or (position == 0 and k % 3 == 0)):
                note = s.take("note:c", NOTES_CLEAN) if vote == "none" else s.take("note:v", NOTES_VIOLATION)
            elif vote == "need_context":
                note = "只看这几句判断不了，前面应该还有别的事"
            result = await s.vote(case_id, name, vote, at, note, category=vote_category)
        if not result.get("settled"):
            raise RuntimeError(f"二期的案件 {case_id}（第 {k} 个）应该在最后一票定案")
        await s.stamp_closed(case_id, closed_at)
        return case_id

    for k, hours, size, outcome, kind in [
        (0, 19 * D + 2, 3, "v", "msg"), (1, 18 * D + 5, 3, "c", "msg"), (2, 17 * D + 3, 5, "v", "msg"), (3, 16 * D + 6, 3, "v", "post"), (4, 15 * D + 2, 3, "v", "msg"),
        (5, 14 * D + 7, 5, "c", "msg"), (6, 13 * D + 3, 3, "v", "msg"), (7, 12 * D + 5, 3, "v", "msg"), (8, 11 * D + 4, 5, "v", "msg"), (9, 10 * D + 6, 3, "c", "post"),
    ]:
        await batch_case(k, s.ago(hours), size, outcome, kind)
    # 最近九天每天一案，主账号都参加（第 14 个他投了不一致的票）
    daily = [(10, 9, 3, "v", "msg", "agree"), (11, 8, 5, "v", "msg", "agree"), (12, 7, 3, "v", "msg", "agree"), (13, 6, 3, "c", "msg", "agree"), (14, 5, 5, "v", "msg", "disagree"), (15, 4, 3, "v", "post", "agree")]
    for k, days_ago, size, outcome, kind, mode in daily:
        case_id = await batch_case(k, s.local_time(days_ago, 13 + (k % 3) * 0.4), size, outcome, kind, mode)
        if k == 10:
            # 主账号这条备注被三个人点了赞：赞数到 3 给作者加一次风纪值
            note_id = await s.note_id(case_id, "juror")
            voters = (await db.execute(select(User.username).join(JuryVote, JuryVote.juror_id == User.id).where(JuryVote.case_id == case_id, JuryVote.voted_at.is_not(None), User.id != juror_id))).scalars().all()
            for name in voters[:3]:
                await s.like(note_id, name)
            special["liked_note"] = case_id
    # 主账号三天前上午遇到一道质控题（答对了）。之后他又投了六七张普通票：下一道质控题随时可能出现，最多再隔四件一定出
    quality = await jury_service._assignable_cases(db, juror_id, golden=True, limit=1)
    await s.vote(quality[0].id, "juror", "l2" if bank_service.question_upheld(quality[0]) else "none", s.local_time(3, 10.2), quality=True)
    for k, days_ago, hour, size, outcome, kind, mode in [(16, 3, 13.3, 3, "v", "msg", "agree"), (17, 2, 13.7, 5, "c", "msg", "agree"), (18, 1, 13.1, 3, "v", "msg", "agree"), (19, 1, 20.5, 3, "v", "msg", None), (20, 1, 21.7, 5, "v", "msg", None)]:
        await batch_case(k, s.local_time(days_ago, hour), size, outcome, kind, mode)
    # 今天的三个案件：所有在任委员本周都有得分，周榜上才有人（时间保证落在本周之内）
    today = [(21, 3.2, "v", ["senior3", "senior1", "trainee2"], False), (22, 2.1, "c", ["senior2", "regular2", "trainee1"], True), (23, 1.1, "v", ["senior3", "senior1", "senior2"], True)]
    for slot, (k, hours, outcome, others, recused) in enumerate(today):
        special[f"today_{slot + 1}"] = await batch_case(k, s.this_week(s.ago(hours), slot), 5, outcome, "msg", "agree", others=others, span=1.0, recused=recused)

    # ---- 题库：委员全票一致的案件自动入库（定时任务里的同一个函数），管理员再核对、改答案、移出几道
    included = await bank_service.auto_include(db)
    if included < 10:
        raise RuntimeError(f"自动入库的题太少：{included} 道")
    await bank_service.update_question(
        db, phase1["nc_clean"], actor_id=admin.id, changes={"verified": True, "explanation": "批评得很直接，但说的是做法不是人，没有人身攻击。委员当时 3 比 2 没有共识，管理员认定不违规。"}
    )
    legacy_unverified = (
        await db.execute(select(ReportCase).where(ReportCase.is_golden.is_(True), ReportCase.golden_source == "legacy", ReportCase.golden_verified.is_(False)).order_by(ReportCase.id))
    ).scalars().all()
    overridden = next(case for case in legacy_unverified if case.target_type == "message" and case.status == "dismissed")
    await bank_service.update_question(
        db, overridden.id, actor_id=admin.id, changes={"outcome": "violation", "level": 1, "category": "advertising", "explanation": "当时按不违规结的案；现在的规则下，在公共群里求赞属于轻微广告。标准答案改为违规。"}
    )
    await bank_service.update_question(db, legacy_unverified[-1].id, actor_id=admin.id, changes={"in_bank": False})
    special["question_overridden"], special["question_removed"] = overridden.id, legacy_unverified[-1].id
    await s.stamp_new(s.ago(days=2, hours=3))
    await db.commit()

    # ---- 还没结的案件，演示新的投票选项
    async def queued(case_id: int, hours: float) -> int:
        await s.triage(case_id, s.ago(hours=hours) + timedelta(minutes=1))
        return case_id

    # 主账号自己投过一张「上下文不足」（案件还在委员队列里；再来一张就转出）
    own_context = await queued(
        await s.message_case(
            conversation="plaza", offender="m180", reporters=["m235"], reason="辱骂骚扰", reported_at=s.ago(32),
            target="你上次说的那个事，我可都记着呢，等着瞧", before=[("A", "这次的合作名单出来了"), ("B", "有我有我")], after=[("A", "你在说谁？")],
        ),
        32,
    )
    await s.vote(own_context, "juror", "need_context", s.ago(30), "只看这几句，判断不了是在威胁还是在开玩笑")
    # 两张「上下文不足」：已经转给管理员
    escalated = await queued(
        await s.message_case(
            conversation="plaza", offender="m181", reporters=["m236"], reason="辱骂骚扰", reported_at=s.ago(18),
            target="呵呵，某些人自己心里清楚", before=[("A", "大家觉得新来的管理怎么样")], after=[("B", "？")],
        ),
        18,
    )
    await s.vote(escalated, "senior1", "need_context", s.ago(16), "没头没尾的一句话，不知道在说谁")
    await s.vote(escalated, "trainee2", "need_context", s.ago(14))
    case = await s.case(escalated)
    if (case.queue, case.jury_outcome) != ("admin", "need_context"):
        raise RuntimeError("两张「上下文不足」之后案件应该已经转给管理员")
    case.escalated_at = s.ago(14)
    await db.commit()
    # 主账号五小时前主动回避的案件（别人还能评审）
    recused = await queued(
        await s.message_case(
            conversation="plaza", offender="m179", reporters=["m237"], reason="辱骂骚扰", reported_at=s.ago(6),
            target=DEMO_TARGETS["recused"], before=[("A", "三班的小林新出的游戏你们玩了吗"), ("B", "玩了，还行")], after=[("A", "你有证据吗"), ("B", "别乱说")],
        ),
        6,
    )
    await s.vote(recused, "juror", "recuse", s.ago(5), "被举报的人提到的小林可能是我同学")
    # 已有一张「上下文不足」：再投一张就转出
    need_context = await queued(
        await s.message_case(
            conversation="plaza", offender="m178", reporters=["m238"], reason="辱骂骚扰", reported_at=s.ago(9),
            target=DEMO_TARGETS["need_context"], before=[("B", "刚才群里怎么了，我错过了什么")], after=[("B", "到底什么事啊")],
        ),
        9,
    )
    await s.vote(need_context, "senior2", "need_context", s.ago(7), "只有这一句，看不出在说什么事、针对谁")
    # 三张票都是严重违规：严重违规要收满五张有效票，正在等第 4、5 票
    severe = await queued(
        await s.message_case(
            conversation="plaza", offender="m177", reporters=["reporter2", "m239", "m240"], reason="辱骂骚扰", reported_at=s.ago(4), pad_before=4, pad_after=2,
            target=DEMO_TARGETS["severe"], before=[("A", "我的新作品上热门啦"), ("X", "抄我的创意还敢发？"), ("A", "我没有抄，做之前都没看过你的")], after=[("A", "你这是在威胁我吗"), ("B", "@管理 有人威胁人")],
        ),
        4,
    )
    await s.vote(severe, "senior1", "l3", s.ago(3), "明确的线下人身威胁，不是气话", category="threat")
    await s.vote(severe, "senior2", "l3", s.ago(2.5), category="threat")
    result = await s.vote(severe, "senior3", "l3", s.ago(2), category="harassment")
    if result["settled"] or (await s.case(severe)).queue != "jury":
        raise RuntimeError("三张严重违规的票不应该定案：要等到五张有效票")
    # 举报理由是「其他」：投违规票时得自己选类别。已经有一张选了「刷屏」的票
    flooding = await queued(
        await s.message_case(
            conversation="plaza", offender="m176", reporters=["m234", "reporter3", "m233"], reason="其他", details="同一句话刷了二十遍，把别人求助的消息都顶上去了", reported_at=s.ago(2.5),
            target=DEMO_TARGETS["category"], before=[("A", "有人能帮我看看这个报错吗"), ("X", "签到签到签到签到签到签到"), ("A", "能不能别刷了，我在问问题")], after=[("B", "他已经刷了一上午了")],
        ),
        2.5,
    )
    await s.vote(flooding, "trainee2", "l1", s.ago(1.5), category="flooding")
    special.update(own_need_context=own_context, need_context_escalated=escalated, juror_recused=recused, need_context=need_context, severe=severe, category=flooding)

    # ---- 入门考试（走开考和交卷的服务函数；同一个人的多场按先后顺序考）
    for username, score, at in [
        ("hesitant", 8, s.ago(days=26)), ("examout", 5, s.ago(days=25)), ("trainee1", 10, s.ago(days=23)), ("trainee2", 6, s.ago(days=16)), ("trainee2", 8, s.ago(days=14)),
        ("examout", 7, s.ago(days=12)), ("examout", 6, s.ago(days=3)), ("exampass", 9, s.ago(days=2)), ("applicant", 9, s.ago(hours=33)), ("examfail", 6, s.ago(hours=5)),
    ]:
        await s.exam(username, score, at)
    # 通过考试的风纪值是成为委员之后才记上的：流水的时间改到任命那天
    for username, _tier, days, _term in APPOINTMENTS:
        await db.execute(update(JuryPointLog).where(JuryPointLog.user_id == s.uid(username), JuryPointLog.kind == "exam_passed").values(created_at=s.ago(days=days)))
    await db.commit()

    # ---- 练习场：主账号做了一部分（错两道），exampass 做完了一整轮又重做了三道
    done = await s.practice("juror", [s.ago(hours) for hours in (6 * D + 3, 6 * D + 2.8, 5 * D + 1, 4 * D + 5, 4 * D + 4.9, 2 * D + 2, D + 12, 7, 5)], wrong=(2, 6))
    total = (await exam_service.practice_next(db, await s.user("exampass")))["round"]["total"]
    step = min(3.0, 92 / (total + 3))
    finished = await s.practice("exampass", [s.ago(96 - step * index) for index in range(total + 3)], wrong=(3, 9, 14, 20))
    if done != 9 or finished != total + 3 or total < 12:
        raise RuntimeError(f"练习场的数据不对：主账号做了 {done} 道，exampass 做了 {finished} 道，一轮共 {total} 道")

    # ---- 风纪值的管理员调整
    for username, points, note, at in [("senior1", 15, "协助核对了一批题库里的旧题", s.ago(days=9)), ("juror", 10, "反馈了评审台的一个显示问题", s.ago(days=4)), ("trainee2", -3, "误操作多记的分，扣回", s.ago(days=2))]:
        await jury_service.review_member(db, s.uid(username), action="adjust_points", actor_id=admin.id, note=note, points=points)
        await s.stamp_new(at)
        await db.commit()

    # ---- 公开展示委员身份：三位打开了开关，并在大厅里说过话（聊天里能看到徽章）
    for username in ("senior1", "senior2", "regular2"):
        await jury_service.update_settings(db, await s.user(username), badge_visible=True)
    lines = [("senior1", "提醒一下新来的同学：发作品记得写清素材来源～", 52), ("m255", "收到，谢谢老周", 49), ("senior2", "周末的像素画比赛有人一起参加吗", 36), ("regular2", "我报名了，正好缺队友", 33), ("m268", "带我一个", 30), ("senior1", "比赛规则在群公告里，记得先看", 24)]
    for username, text, minutes in lines:
        await s.message("plaza", username, text, s.ago(minutes=minutes))
    await db.commit()

    # ---- 连续参与天数按实际投票的日子重算；在任天数改成真实的
    await s.rebuild_streaks()
    for username, _tier, days, _term in APPOINTMENTS:
        member = await jury_service.get_member(db, s.uid(username))
        member.serving_since = s.ago(days=days)
    await db.commit()
    db.add(SystemSetting(key="jury_demo.cases2", value=json.dumps(special, ensure_ascii=False)))
    await db.commit()


# ====================================================================== 假数据：三期（资深合议、申诉复审、机器案源、复议、判例、议事厅）

# 三期追加的 6 位委员：(用户名, 层级, 任命距今多少天, 任期还剩多少天)。只用来给三期的案件凑原审的票、给议事厅添点人气，
# 这样不用动一、二期里那几位有特别用途的委员（他们的一致率、弃权率、晋升进度都是算好的）。
BG_JURORS = [("m011", "regular", 41, 19), ("m012", "regular", 38, 22), ("m013", "regular", 36, 24), ("m014", "trainee", 20, 10), ("m015", "trainee", 18, 12), ("m016", "trainee", 16, 14)]
# 两位「今天还没投票」的见习委员：(用户名, 层级, 任命距今多少天, 任期还剩多少天, 之前哪几天答过质控题 [(几天前, 北京时间几点)])。
# 他们以前的票都是质控题（不牵扯任何真实案件）；今天一张没投，所以今天的第一张有效票能看到 daily_first，streak6 还能看到 streak_7。
LATE_JURORS = [
    ("dayfirst", "trainee", 9, 21, [(5, 20.1), (3, 12.4), (2, 21.3)]),
    ("streak6", "trainee", 8, 22, [(6, 19.2), (5, 12.8), (4, 20.6), (3, 18.1), (2, 13.3), (1, 19.7)]),
]
# 三期用到的被举报人：一、二期都没用过的大厅成员
PHASE3_OFFENDERS = [f"m{index:03d}" for index in range(161, 176)]

# 判例：(案件怎么找, 标题, 要旨, 创建距今多少天, 指定由谁创建)。
# 「怎么找」是一期记下的案件名（phase1 里的 key），或者被举报的那句原文（一、二期按素材库建的案件）。
# 指定为 None 时：有资深委员投过这个案件就由他标，否则由管理员在后台标。
PRECEDENTS: list[tuple[str, str, str, float, str | None]] = [
    (
        "key:nc_recent", "在公开群里招揽付费答疑群，按轻微广告处理",
        "被举报人在大厅里说自己开了付费答疑群，让想进的人私聊他要二维码。原审五票 3 比 2 没有共识，最后认定为轻微广告。\n\n"
        "要点：一、在公共场合招揽付费服务并留下联系办法，就是广告，和服务本身好不好无关。二、只发了一次、没有刷屏，内容也不涉及诈骗，所以按轻微处理。"
        "三、同样的话反复发，或者换着群发，应当按一般处理。\n\n注意区分：回答别人问题时顺口提一句「我也接这种单」，没有主动招揽，不算广告。",
        2.1, "senior1",
    ),
    (
        "key:offender", "别人正常提问，上来就骂人：辱骂骚扰（一般）",
        "有人在小群里问列表怎么排序，被举报人回了一句「你是没长脑子吗」。三位委员一致认定违规。\n\n"
        "要点：一、对方是正常提问，没有任何挑衅；二、话是冲着人说的，不是在评价问题本身；三、被旁人提醒之后也没有收敛。三条都占，按一般处理。\n\n"
        "如果只是不耐烦地回一句「自己查」，没有贬低对方，不算违规。",
        4.2, "senior2",
    ),
    (
        "key:nc_clean", "批评得很直接，但说的是做法不是人：不算辱骂",
        "被举报的那句是「你这个设计思路完全是错的，再这么做下去就是浪费时间」。原审 3 比 2 没有共识，管理员认定不违规。\n\n"
        "要点：判断是不是人身攻击，看的是话冲着谁。说「这个思路是错的」「这样做是浪费时间」，对象是做法；说「你就是个废物」，对象才是人。"
        "语气不留情面本身不构成违规。\n\n被批评的人觉得不舒服可以理解，但这不是举报能解决的问题。",
        9.3, "admin",
    ),
    (
        "text:免费送 1000 金币！转发这条动态并私聊我你的账号密码即可领取", "索要账号密码，不管用什么名义，一律按诈骗处理",
        "公开动态里说转发并私聊账号密码就送金币。委员一致认定违规。\n\n要点：一、官方和任何正规活动都不会索要密码，凡是要密码的，默认就是骗号；"
        "二、不需要证明真的有人被骗，发出来就算；三、「转发才能领」会让更多人看到，情节不因为它是「活动」而减轻。\n\n"
        "同类情形：冒充客服要验证码、发来路不明的「登录领奖」链接。",
        11.4, "admin",
    ),
    (
        "text:刷粉刷赞工作室，一单起刷，诚信经营，联系 QQ 10000", "刷粉刷赞的广告：留了联系方式就按一般处理",
        "公开动态里给刷粉刷赞的生意打广告，并留了联系方式。\n\n要点：一、卖的东西本身就在破坏社区的公平（刷出来的赞会挤掉别人的作品），不是普通的个人接单；"
        "二、留了站外联系方式，明显是在引流；三、只发了一条，所以没有到严重。\n\n和「我的作品求个赞」区分：那是在给自己的作品拉人气，不是在卖东西。",
        8.2, None,
    ),
    (
        "text:你个笨蛋哈哈哈，积木拖反了啦", "朋友之间的玩笑话，对方没有不适，不算辱骂",
        "有人问为什么角色往后走，被举报人回「你个笨蛋哈哈哈，积木拖反了啦」，提问的人回了一句「啊真的！谢谢」。委员一致认定不违规。\n\n"
        "要点：一、「笨蛋」出现在帮对方解决问题的那句话里，后面跟着的是答案；二、被说的人自己接了话、道了谢，没有任何不适的表示；三、举报人不是当事人。\n\n"
        "同一个词，换成对陌生人、对方明确表示不舒服、或者反复说，结论会不一样。",
        7.3, None,
    ),
    (
        "text:小学生就别来丢人了，回去写作业吧，菜得抠脚", "拿年龄嘲讽新手，属于辱骂骚扰",
        "一个五年级的同学发了自己做的第一个游戏，被举报人回「小学生就别来丢人了」。\n\n要点：一、针对的是人（年龄、水平），不是作品哪里做得不好；"
        "二、对方是新手，正在分享自己的作品，没有任何挑衅；三、社区里未成年人占多数，拿年龄当攻击点的要从严。\n\n只说「这个游戏还比较粗糙」不违规。",
        6.1, None,
    ),
    (
        "text:有人要破解版的付费素材包吗，我这有全套，私聊发你", "免费分享破解资源同样违规",
        "有人抱怨素材包太贵，被举报人说自己有破解版的全套，可以私聊发。\n\n要点：一、违规的是传播破解 / 盗版资源这件事，收不收钱不影响定性；"
        "二、说了「私聊发你」就算，不需要等到真的发出去；三、他是主动提供，不是被问到才答。\n\n只是问一句「这个有免费的替代品吗」不违规。",
        5.2, None,
    ),
]
# 一条后来被管理员撤下的判例
PRECEDENT_WITHDRAWN = (
    "text:谁有那个付费课程的网盘资源，我出 5 块钱收", "求购盗版资源和传播盗版同样处理",
    "被举报人在群里出钱求一门付费课程的网盘资源。\n\n要点：求购和提供一样，都是在促成盗版传播。",
    10.2, "要旨写得太宽了：只是问一句「有没有」的不该一律按违规处理。先撤下，重写之后再标。", 3.1,
)


async def seed_phase3(s: Seeder) -> None:
    db = s.db
    await s.load()
    admin = await s.user("admin")
    phase1 = json.loads((await db.get(SystemSetting, "jury_demo.cases")).value)
    special: dict[str, Any] = {}
    plaza_id = s.conversation_ids["plaza"]

    # ---- 三期新增的四个账号：两个被处罚的用户、两位今天还没投票的见习委员（一、二期的库升级上来时也在这里补建）
    password_hash = hash_password(PASSWORD)
    for offset, (username, days, bound) in enumerate([("appealing", 95, True), ("punished", 150, False), ("dayfirst", 80, True), ("streak6", 70, False)]):
        if username in s.user_ids:
            continue
        nickname = next(row[1] for row in ACCOUNTS if row[0] == username)
        user = User(
            username=username, hashed_password=password_hash, nickname=nickname, bio="风纪系统联调用的演示账号", platform_role=PlatformRole.USER,
            ccw_student_oid=f"demo{900 + offset:020d}" if bound else None, ccw_name=nickname if bound else None, created_at=s.ago(days=days), updated_at=s.ago(days=days),
        )
        db.add(user)
        await db.flush()
        db.add(ConversationMember(conversation_id=plaza_id, user_id=user.id, role=MemberRole.MEMBER, joined_at=s.ago(days=days - 5)))
    await db.commit()
    await s.load()

    # ---- 演示库里打开两个生产默认关闭的开关：申诉先进资深复审、机器拿不准的内容作为案源
    await jury_settings.update_switches(db, admin.id, {"appeals": True, "machine_review": True})
    jury_settings.reset_cache()
    await s.stamp_new(s.ago(days=14))
    await db.commit()

    # ---- 追加 6 位委员（走后台的任命流程）。在任天数留到最后再改，免得灌数据的过程中被自动晋升
    for username, tier, days, term_left in BG_JURORS:
        member = await jury_service.review_member(db, s.uid(username), action="approve", actor_id=admin.id, note="三期上线前补充任命", tier=tier)
        member.statement = "想参与评审"
        member.applied_at = s.ago(days=days + 1)
        member.reviewed_at = s.ago(days=days)
        member.term_ends_at = s.now + timedelta(days=term_left)
        await s.stamp_new(s.ago(days=days))
        await db.commit()

    async def queued(case_id: int, at: datetime) -> int:
        await s.triage(case_id, at + timedelta(minutes=1))
        return case_id

    async def report_of(case_id: int, reporter: str) -> Any:
        rows = await db.execute(select(UserReport).where(UserReport.case_id == case_id, UserReport.reporter_id == s.uid(reporter)).order_by(UserReport.id.asc()))
        return rows.scalars().first()

    # ================================================================== 资深合议
    # 原审都由追加的那几位委员投（没有资深委员参加），所以三位资深委员都能进合议庭。

    # 合议已定案：五票 3 比 2 转合议，两位资深委员都认定违规。主账号在原审里投过票，详情里能看到 tally.panel
    reported_at = s.ago(days=6)
    settled = await queued(
        await s.message_case(
            conversation="plaza", offender="m164", reporters=["reporter3", "m221"], reason="辱骂骚扰", reported_at=reported_at, pad_before=5, pad_after=2,
            target=DEMO_TARGETS["panel_settled"],
            before=[("A", "这次活动的排行榜我进前十了！"), ("B", "厉害厉害")], after=[("A", "我一局一局打上去的，凭什么说我刷分"), ("C", "酸了酸了")],
        ),
        reported_at,
    )
    await s.trial(
        settled,
        [
            ("juror", "l2", "没有证据就说别人刷分，后半句是冲着人去的"), ("m011", "none", "只是质疑成绩，语气差了点"), ("m012", "l1"),
            ("m013", "none"), ("m014", "l2"),
        ],
        reported_at, s.ago(days=5, hours=6),
    )
    await s.to_panel(settled, s.ago(days=5, hours=6), "no_consensus")
    await s.panel_vote(settled, "senior1", "l2", s.ago(days=5), "质疑成绩可以，「没实力就别出来显摆」是在贬低人。同意违规，一般。", category="harassment")
    result = await s.panel_vote(settled, "senior2", "l1", s.ago(days=4, hours=8), "违规，但只有这一句，我给轻微")
    if not result["settled"]:
        raise RuntimeError("两张合议票都是违规，应该当场定案")
    await s.stamp_closed(settled, s.ago(days=4, hours=8))
    juror_note = await s.note_id(settled, "juror")
    await s.opinion(settled, "senior2", "合议时我们看的主要是后半句。只说「全靠刷分的吧」的话，我会投不违规。", s.ago(days=4, hours=7), juror_note)
    await s.opinion(settled, "juror", "明白了，以后这种我会把前后两半分开看", s.ago(days=4, hours=5), juror_note)
    special["panel_settled"] = settled

    # 合议超时：一位资深委员投了票，48 小时没等到第二张，由定时任务的同一段逻辑转给管理员
    reported_at = s.ago(days=4)
    stale = await queued(
        await s.message_case(
            conversation="plaza", offender="m165", reporters=["m222"], reason="垃圾广告", reported_at=reported_at, pad_before=4, pad_after=1,
            target="组队可以，进队先交 10 块钱押金，比赛完了退，不交的别来",
            before=[("A", "有没有人一起组队参加下个月的比赛")], after=[("B", "还要交钱？"), ("X", "怕有人中途跑路")],
        ),
        reported_at,
    )
    await s.trial(stale, [("m013", "l1", "收押金就是变相收费"), ("m015", "none"), ("m016", "none", "只是组队的条件，不是广告"), ("m012", "l1"), ("m014", "l1")], reported_at, s.ago(60))
    await s.to_panel(stale, s.ago(60), "no_consensus")
    await s.panel_vote(stale, "senior3", "l1", s.ago(50), "不算广告，但在群里收钱的做法有风险，我倾向轻微")
    if await panel_service.sweep(db) != 1:
        raise RuntimeError("合议超时转出的案件数不对：此刻合议队列里应该只有这一个超过 48 小时")
    case = await s.case(stale)
    if (case.queue, case.panel_outcome) != ("admin", "timeout"):
        raise RuntimeError("合议超时的案件应该已经转给管理员")
    case.updated_at = s.ago(12)
    await db.commit()
    special["panel_timeout"] = stale

    # 合议中、还没人投：三人举报，优先级最高，资深委员取到的第一件就是它。主账号在原审里投过票
    reported_at = s.ago(34)
    fresh = await queued(
        await s.message_case(
            conversation="plaza", offender="m161", reporters=["reporter2", "m216", "m217"], reason="辱骂骚扰", reported_at=reported_at, pad_before=5, pad_after=2,
            target=DEMO_TARGETS["panel_fresh"],
            before=[("A", "我的塔防游戏更新了第二章，欢迎来玩"), ("B", "玩了，第三关有点难")],
            after=[("A", "可以说哪里不好，没必要这么说吧"), ("X", "我说的是游戏又不是你"), ("C", "这话确实难听")],
        ),
        reported_at,
    )
    await s.trial(
        fresh,
        [
            ("juror", "l1", "说的是作品，不过「跟屎一样」已经是在羞辱作者了，我给轻微"), ("m011", "l1"), ("m014", "none", "骂的是游戏不是人，算不上辱骂骚扰"),
            ("m012", "none"), ("m015", "l2", "作者就在场，这么说和骂人没区别"),
        ],
        reported_at, s.ago(6),
    )
    await s.to_panel(fresh, s.ago(6), "no_consensus")
    special["panel_fresh"] = fresh

    # 合议中、已有一张违规票：再来一张违规票就定案
    reported_at = s.ago(44)
    one_vote = await queued(
        await s.post_case(author="m162", reporters=["m218", "m219"], reason="垃圾广告", reported_at=reported_at, images=0, content=DEMO_TARGETS["panel_one_vote"]),
        reported_at,
    )
    await s.trial(
        one_vote,
        [("m012", "none", "自己的作品找人测试，给点报酬很正常"), ("m013", "l1", "用红包换评价，等于花钱刷好评"), ("m015", "none"), ("m016", "l2"), ("m011", "l1")],
        reported_at, s.ago(20),
    )
    await s.to_panel(one_vote, s.ago(20), "no_consensus")
    await s.panel_vote(one_vote, "senior2", "l1", s.ago(15), "有偿换评价会误导别人，但规模很小，我倾向轻微广告", category="advertising")
    special["panel_one_vote"] = one_vote

    # 因「上下文不足」进合议：两张 need_context
    reported_at = s.ago(14)
    unclear = await queued(
        await s.message_case(
            conversation="plaza", offender="m163", reporters=["m220"], reason="辱骂骚扰", reported_at=reported_at, pad_before=3,
            target=DEMO_TARGETS["panel_need_context"], before=[("B", "昨天的比赛结果出来了")], after=[("C", "又在说谁啊")],
        ),
        reported_at,
    )
    await s.trial(unclear, [("m014", "need_context", "不知道在说谁、指的是哪件事"), ("m013", "need_context")], reported_at, s.ago(10))
    await s.to_panel(unclear, s.ago(10), "need_context")
    special["panel_need_context"] = unclear
    # 因超时进合议的那一件是一期造的（timeout_open）；凑不齐合议庭、直接转管理员的是一期的 no_consensus_open（原审里有两位资深委员）
    special["panel_timeout_in"], special["panel_unavailable"] = phase1["timeout_open"], phase1["no_consensus_open"]

    # ================================================================== 申诉复审

    async def jury_violation(offender: str, *, target: str, reason: str, closed_at: datetime, ballots: list[tuple], reporters: list[str], details: str | None = None, **lines: Any) -> tuple[int, int]:
        """委员定案的一条处罚（大厅里的消息）。返回 (案件编号, 处罚记录 id)。"""
        reported = closed_at - timedelta(hours=9)
        case_id = await queued(
            await s.message_case(conversation="plaza", offender=offender, reporters=reporters, reason=reason, reported_at=reported, target=target, details=details, pad_before=4, pad_after=1, **lines),
            reported,
        )
        if not (await s.trial(case_id, ballots, reported, closed_at))["settled"]:
            raise RuntimeError(f"案件 {case_id} 应该在最后一票定案")
        return case_id, (await s.stamp_closed(case_id, closed_at)).violation_id

    async def machine_violation(username: str, *, content: str, category: str, level: int, at: datetime, reason: str) -> int:
        """机器自动处罚的一条记录：一条大厅消息被判违规并撤回。没有案件，只有审查记录。"""
        item = await s.message("plaza", username, content, at)
        item.recalled_at = at + timedelta(seconds=3)
        record = ContentModerationRecord(
            source=ModerationSource.MESSAGE_AUTO, message_id=item.id, conversation_id=plaza_id, user_id=s.uid(username), content_type="text", content_text=content,
            decision="block", severity=level, categories=json.dumps([category]), confidence=0.93, reason=reason,
            raw_response=json.dumps({"decision": "block", "category": category, "level": level, "confidence": 0.93, "reason": reason}, ensure_ascii=False),
            action_taken=ModerationAction.RECALLED, created_at=at + timedelta(seconds=2), acted_at=at + timedelta(seconds=3),
        )
        db.add(record)
        await db.flush()
        outcome = await penalty_service.record_violation(
            db, user_id=s.uid(username), category=category, level=level, source="auto_ai", target_type="message", target_id=item.id, conversation_id=plaza_id,
            moderation_record_id=record.id, confidence=0.93, summary=content, reason=reason,
        )
        await db.commit()
        await penalty_service.flush_notifications(db)
        await s.stamp_violation(outcome.violation, at + timedelta(seconds=3))
        return outcome.violation.id

    # ---- punished：三条申诉都已经由资深复审出了结果（维持 / 减轻 / 撤销），外加一条还没申诉的
    _case, upheld_id = await jury_violation(
        "punished", reason="垃圾广告", closed_at=s.ago(days=13), reporters=["m223", "m224"],
        target="出自制的刷金币脚本，全自动，20 一份，要的加我好友", before=[("A", "这个游戏的金币也太难攒了")], after=[("B", "卖挂的？"), ("C", "举报了")],
        ballots=[("m011", "l2", "卖外挂脚本，明码标价"), ("m013", "l2"), ("m015", "l2")],
    )
    review = await s.appeal("punished", upheld_id, "脚本是我自己写的，只是想分享给需要的人，没有骗任何人", s.ago(days=12, hours=18))
    await s.review_vote(review.id, "senior1", "uphold", s.ago(days=12, hours=12), "卖的是外挂脚本，广告和违规内容都占了，维持")
    await s.review_vote(review.id, "senior2", "uphold", s.ago(days=12, hours=6))
    _case, reduced_id = await jury_violation(
        "punished", reason="辱骂骚扰", closed_at=s.ago(days=10), reporters=["m225"],
        target="你这种人也配当组长？脑子进水了才会选你", before=[("A", "小组分工我排好了，大家看一下"), ("X", "凭什么又让我做最累的")], after=[("A", "有意见可以商量，别骂人"), ("X", "……刚才话说重了，对不起")],
        ballots=[("m012", "l2", "直接骂人了"), ("m014", "l2"), ("m016", "l2")],
    )
    review = await s.appeal("punished", reduced_id, "当时在气头上，说完马上就道歉了，对方也接受了。禁言一天我觉得太重了", s.ago(days=9, hours=20))
    await s.review_vote(review.id, "senior2", "reduce", s.ago(days=9, hours=14), "确实骂了人，但当场道了歉。记录保留，禁言可以免")
    await s.review_vote(review.id, "senior3", "reduce", s.ago(days=9, hours=8))
    revoked_case, revoked_id = await jury_violation(
        "punished", reason="辱骂骚扰", closed_at=s.ago(days=7), reporters=["m226"],
        target="笑死，这配色是色盲选的吧，辣眼睛", before=[("B", "新界面做好了，大家看看配色"), ("B", "我自己都觉得有点花")], after=[("B", "哈哈哈确实，我再调调"), ("X", "调好了我帮你测")],
        ballots=[("m013", "l2", "嘲讽别人是色盲"), ("m015", "l2"), ("m016", "l1")],
    )
    review = await s.appeal("punished", revoked_id, "我和他是同班同学，平时就这么互相开玩笑。他自己都接梗了，是路过的人举报的", s.ago(days=6, hours=20))
    await s.review_vote(review.id, "senior1", "revoke", s.ago(days=6, hours=12), "被说的人自己在接梗，前后文是朋友间互损，不该处罚")
    await s.review_vote(review.id, "senior3", "revoke", s.ago(days=6, hours=4), "同意，举报人不是当事人")
    _case, open_id = await jury_violation(
        "punished", reason="其他", details="同一句话连着刷了十几遍，把别人的求助都顶上去了", closed_at=s.ago(days=2), reporters=["m227"],
        target="签到打卡签到打卡签到打卡（第 15 遍）", before=[("A", "有人能帮我看看这个报错吗"), ("X", "签到打卡签到打卡签到打卡")], after=[("A", "能不能别刷了")],
        ballots=[("m011", "l1", None, "flooding"), ("m012", "l1", None, "flooding"), ("m014", "l1", None, "flooding")],
    )
    special.update(appeal_upheld=upheld_id, appeal_reduced=reduced_id, appeal_revoked=revoked_id, appeal_revoked_case=revoked_case, appeal_open=open_id)

    # ---- appealing：四条处罚的申诉都还没有结果
    # 1) 机器处罚（轻微），申诉进了资深复审，72 小时没人投完：由定时任务的同一段逻辑转给管理员
    timeout_id = await machine_violation("appealing", content="卧槽这什么垃圾匹配机制，气死我了，傻 X 游戏", category="profanity", level=1, at=s.ago(days=6), reason="脏话")
    review = await s.appeal("appealing", timeout_id, "我骂的是游戏的匹配机制，不是在骂人", s.ago(days=5, hours=18))
    if await review_service.sweep(db) != 1:
        raise RuntimeError("复审超时转出的件数不对：此刻应该只有这一件过了 72 小时")
    await db.refresh(review)
    review.escalated_at = review.expires_at
    await s.stamp_new(penalty_service.aware(review.expires_at))
    await db.commit()
    # 2) 私聊里的举报由管理员定案：这种处罚的申诉不进资深复审，直接到管理员
    left, right = sorted((s.uid("reporter4"), s.uid("appealing")))
    direct = Conversation(type=ConversationType.DIRECT, direct_key=f"{left}:{right}", created_by_id=s.uid("appealing"), created_at=s.ago(days=9), updated_at=s.ago(days=4))
    db.add(direct)
    await db.flush()
    for username in ("reporter4", "appealing"):
        db.add(ConversationMember(conversation_id=direct.id, user_id=s.uid(username), role=MemberRole.MEMBER, joined_at=s.ago(days=9)))
    await db.commit()
    reported_at = s.ago(days=4, hours=6)
    private = await queued(
        await s.message_case(
            conversation=direct.id, offender="appealing", reporters=["reporter4"], reason="辱骂骚扰", reported_at=reported_at,
            target="再不回消息，我就天天去你作品下面刷差评", before=[("X", "上次说好一起做的游戏还做不做了"), ("X", "？")], after=[("A", "最近在考试，考完再说")],
        ),
        reported_at,
    )
    case = await s.admin_resolve(private, s.ago(days=4), upheld=True, category="harassment", level=2, note="私聊里威胁刷差评，按一般处理")
    direct_id = case.violation_id
    review = await s.appeal("appealing", direct_id, "是他先不回消息的，我只是说气话，没有真的去刷", s.ago(days=3, hours=12))
    if review is not None:
        raise RuntimeError("私聊相关的处罚不应该进资深复审")
    # 3) 机器处罚（一般），5 小时前申诉，正在资深复审里：已经有一位资深委员投了「维持」
    machine_id = await machine_violation("appealing", content=DEMO_TARGETS["appeal_machine"], category="advertising", level=2, at=s.ago(days=3), reason="刷量广告")
    review = await s.appeal("appealing", machine_id, "我是帮朋友转发的，不知道这也算广告", s.ago(5))
    await s.review_vote(review.id, "senior3", "uphold", s.ago(3), "不管帮谁发的，内容就是刷量广告")
    special["review_machine"] = review.id
    # 4) 委员定案的处罚：累计记分跨过了 9 分，禁言 72 小时（现在还在禁言中）。26 小时前申诉，正在资深复审里，还没人投
    appeal_case, jury_id = await jury_violation(
        "appealing", reason="辱骂骚扰", closed_at=s.ago(30), reporters=["reporter1", "m228"],
        target=DEMO_TARGETS["appeal_jury"], before=[("A", "克隆体那个问题还是没解决，有人能再帮我看看吗")], after=[("A", "我是真的没弄明白……"), ("B", "不想答可以不答，别骂人")],
        ballots=[("m011", "l2", "别人只是在求助，他上来就骂「有病」"), ("m012", "l2", "嫌人问得多可以不理，不能骂人"), ("m016", "l1")],
    )
    review = await s.appeal("appealing", jury_id, "他同一个问题在群里问了很多遍，我是想提醒他别刷屏，语气重了一点。禁言三天太久了", s.ago(26))
    special.update(review_jury=review.id, appeal_jury_case=appeal_case, appeal_timeout=timeout_id, appeal_direct=direct_id, appeal_machine=machine_id, appeal_jury=jury_id)

    # ================================================================== 机器来源的案件（没有举报人）

    async def machine_case(kind: str, author: str, content: str, category: str, at: datetime, *, before: list[tuple[str, str]] | None = None, after: list[tuple[str, str]] | None = None, hint: str = "") -> int:
        """机器判为「拿不准」的一条内容：走 jury_machine 的建案函数（和审核任务最后一步调的是同一个）。"""
        raw = {"decision": "review", "category": category, "categories": [category], "confidence": 0.62, "reason": hint or "疑似违规，拿不准"}
        fields = dict(source=ModerationSource.MESSAGE_AUTO, user_id=s.uid(author), content_type="text", content_text=content, decision="review", severity=1,
                      categories=json.dumps([category]), confidence=0.62, reason=raw["reason"], raw_response=json.dumps(raw, ensure_ascii=False), created_at=at + timedelta(seconds=2))
        if kind == "post":
            item = await s.post(author, content, 0, at)
            post_id = item.id
            # 和线上一样：建案时动态是从库里读出来的（带着作者）
            db.expire(item)
            post = (await db.execute(select(Post).options(joinedload(Post.author)).where(Post.id == post_id))).scalar_one()
            record = ContentModerationRecord(post_id=post_id, **fields)
            db.add(record)
            await db.flush()
            case = await machine_service.offer_post(db, post, [record], ModerationAction.NONE)
        else:
            # 快照取被判的消息之前的 10 条：自己把上下文补满 10 条，免得接上会话里更早、时间却更晚的消息
            lines = s.filler(10 - len(before or []), PLAZA_CHATTERS) + [(s.rng.choice(PLAZA_CHATTERS) if who != "X" else author, text) for who, text in before or []]
            created = [await s.message("plaza", who, text, at - timedelta(seconds=41 * (len(lines) - index))) for index, (who, text) in enumerate(lines)]
            created.append(await s.message("plaza", author, content, at))
            target_id = created[-1].id
            for index, (who, text) in enumerate(after or []):
                created.append(await s.message("plaza", s.rng.choice(PLAZA_CHATTERS) if who != "X" else author, text, at + timedelta(seconds=36 * (index + 1))))
            await db.commit()
            # 和线上一样：生成快照时消息都是从库里读出来的
            for item in created:
                db.expire(item)
            target = (await db.execute(select(Message).options(joinedload(Message.sender)).where(Message.id == target_id))).scalar_one()
            record = ContentModerationRecord(message_id=target_id, conversation_id=plaza_id, **fields)
            db.add(record)
            await db.flush()
            case = await machine_service.offer_message(db, target, [record], ModerationAction.NONE, ModerationSource.MESSAGE_AUTO)
        if case is None:
            raise RuntimeError(f"机器来源的案件没有建出来：{content}")
        queued_at = at + timedelta(seconds=6)
        case.created_at = case.updated_at = case.queued_at = queued_at
        await db.commit()
        return case.id

    # 已定案、进了公示栏：公开动态，三位委员一致认定违规（轻微）
    published = await machine_case("post", "m173", "加我领取免费皮肤兑换码，先到先得，回复「想要」我私信你链接", "fraud", s.ago(days=6), hint="疑似诱导私聊的钓鱼信息")
    if not (await s.trial(published, [("m011", "l1", "没说是什么活动的兑换码，引导私聊，像钓鱼", "fraud"), ("m013", "l2", None, "fraud"), ("m015", "l1", None, "fraud")], s.ago(days=6), s.ago(days=5)))["settled"]:
        raise RuntimeError("机器来源的案件应该由三张一致的票定案")
    await s.stamp_closed(published, s.ago(days=5))
    # 「未作处理」：两张「上下文不足」，其中一张是资深委员投的，凑不齐合议庭；没有举报人在等结果，所以不转管理员，直接结案
    unhandled = await machine_case(
        "message", "m174", "懂的都懂，那个谁最近又在搞小动作了", "harassment", s.ago(60), before=[("B", "今天群里好安静")], after=[("C", "？")], hint="疑似影射他人",
    )
    result = await s.trial(unhandled, [("juror", "need_context", "没头没尾，看不出在说谁、说的是什么事"), ("senior2", "need_context")], s.ago(60), s.ago(50))
    case = await s.case(unhandled)
    if result["settled"] or (case.status, case.verdict_source, case.panel_outcome) != ("dismissed", "auto", "unavailable"):
        raise RuntimeError("凑不齐合议庭的机器案件应该以「未作处理」结案")
    await s.stamp_closed(unhandled, s.ago(50))
    # 委员队列里的三件：排在所有举报案件后面
    waiting = [
        await machine_case("message", "m170", DEMO_TARGETS["machine_1"], "harassment", s.ago(5), before=[("B", "本周的推荐作品出来了")], after=[("C", "我觉得还行啊")], hint="疑似辱骂"),
        await machine_case("message", "m171", DEMO_TARGETS["machine_2"], "advertising", s.ago(3), before=[("B", "第七关怎么过啊，卡了一晚上")], after=[("B", "还收钱？")], hint="疑似有偿服务广告"),
        await machine_case("post", "m172", DEMO_TARGETS["machine_3"], "harassment", s.ago(2), hint="疑似点名攻击"),
    ]
    special.update(machine_published=published, machine_unhandled=unhandled, machine_waiting=waiting)

    # ================================================================== 举报人复议

    async def cleared(reporter: str, closed_at: datetime, ballots: list[tuple], *, kind: str = "msg", **content: Any) -> tuple[int, int]:
        """委员一致认定不违规的案件。返回 (案件编号, 这位举报人的举报 id)。"""
        reported = closed_at - timedelta(hours=10)
        if kind == "post":
            case_id = await s.post_case(reporters=[reporter], reported_at=reported, **content)
        else:
            case_id = await s.message_case(reporters=[reporter], reported_at=reported, **content)
        await queued(case_id, reported)
        if not (await s.trial(case_id, ballots, reported, closed_at))["settled"]:
            raise RuntimeError(f"案件 {case_id} 应该在最后一票定案")
        case = await s.stamp_closed(case_id, closed_at)
        if (case.status, case.verdict_source) != ("dismissed", "jury"):
            raise RuntimeError(f"案件 {case_id} 应该由委员认定不违规")
        return case_id, (await report_of(case_id, reporter)).id

    # 已改判：两位资深委员都投了「改判」（等级取较低的那个）
    overturned, report_id = await cleared(
        "reporter4", s.ago(days=6, hours=12), [("m012", "none", "只是分享存档"), ("m013", "none"), ("m015", "none")],
        conversation="plaza", offender="m168", reason="违法违规", pad_before=4, pad_after=1,
        target="想要这个游戏破解存档的私聊我，数值都改好了，直接满级", before=[("A", "这游戏后面的关卡要付费解锁，好贵")], after=[("B", "破解存档不就是盗版吗")],
    )
    review = await s.reconsider("reporter4", report_id, "这个游戏后面的关卡是付费解锁的，他发的破解存档等于帮人绕过付费，应该算传播盗版", s.ago(days=6))
    await s.review_vote(review.id, "senior2", "overturn", s.ago(days=5, hours=18), "改了数值、绕过付费的存档，属于破解资源", level=1, category="illegal")
    await s.review_vote(review.id, "senior3", "overturn", s.ago(days=5, hours=12), None, level=2, category="illegal")
    special["reconsider_overturned"] = overturned
    # 已维持：两位资深委员都投了「维持」
    upheld, report_id = await cleared(
        "reporter3", s.ago(days=5), [("m011", "none", "是在发牢骚，没有指向具体的人"), ("m014", "none"), ("m016", "none")],
        kind="post", author="m167", reason="辱骂骚扰", images=0, content="个人观点：今年官方比赛的评审有黑幕，懂的都懂",
    )
    review = await s.reconsider("reporter3", report_id, "这是在没有任何证据的情况下造谣，会误导其他参赛的人，希望能再看一次", s.ago(days=4, hours=12))
    await s.review_vote(review.id, "senior1", "uphold", s.ago(days=4, hours=6), "表达不满和质疑不等于造谣，也没有指向具体的人")
    await s.review_vote(review.id, "senior2", "uphold", s.ago(days=4))
    special["reconsider_upheld"] = upheld
    # 转给管理员待处理：原审里有一位资深委员，申请复议时凑不齐三位
    to_admin, report_id = await cleared(
        "reporter1", s.ago(days=3, hours=18), [("senior1", "none", "不耐烦，但没有骂人"), ("m013", "none"), ("m016", "none")],
        conversation="plaza", offender="m169", reason="辱骂骚扰", pad_before=4,
        target="新人别问了，群文件里都有，不会自己看吗，真服了", before=[("A", "请问作品怎么导出成网页呀")], after=[("A", "我找了没找到才问的")],
    )
    review = await s.reconsider("reporter1", report_id, "他对新人一直是这个态度，已经不止一次了，希望能结合他平时的发言再看看", s.ago(days=1))
    if review.stage != "admin" or review.escalation != "unavailable":
        raise RuntimeError("原审里有资深委员的案件，复议应该因为凑不齐人直接转管理员")
    special["reconsider_admin"] = to_admin
    # 复议进行中：10 小时前申请，等资深委员投票
    pending, report_id = await cleared(
        "reporter2", s.ago(days=3), [("m012", "none"), ("m014", "none"), ("m016", "none", "话很难听，但说的是作品")],
        conversation="plaza", offender="m166", reason="辱骂骚扰", pad_before=5, pad_after=1,
        target=DEMO_TARGETS["reconsider_pending"], before=[("A", "我的平台跳跃游戏做完第一关了，求试玩"), ("B", "跳跃手感有点飘")], after=[("A", "我会继续改的")],
    )
    review = await s.reconsider("reporter2", report_id, "他不是第一次这样说我了，上周在别的群也嘲讽过我的作品。希望能结合他之前的发言再看一次", s.ago(10))
    if review.stage != "senior":
        raise RuntimeError("这件复议应该在资深复审里")
    special.update(reconsider_pending=pending, review_reconsider=review.id)
    # 可以申请复议：周末联机群里的一句话，一天半前被委员认定不违规。别人再举报同一句话会得到 409 already_reviewed
    open_case, report_id = await cleared(
        "reporter1", s.ago(36), [("m011", "none", "只是吐槽打得不好，没有辱骂"), ("m013", "none"), ("m015", "none")],
        conversation="weekend", offender="m066", reason="辱骂骚扰",
        target=DEMO_TARGETS["reconsider_open"], before=[("B", "昨晚那把谁赢了"), ("C", "我们队输了")], after=[("A", "……我第一次玩射手")],
    )
    special.update(reconsider_open=open_case, reconsider_open_report=report_id, already_reviewed_message=(await s.case(open_case)).message_id)

    # ================================================================== 判例
    seniors = {s.uid(name): name for name in ("senior1", "senior2", "senior3")}

    async def find_case(locator: str) -> Any:
        """按一期记下的名字，或者按被举报的那句原文，找一个已定案、没被改判的案件。原文重复出现时优先取在公示栏里、又不是计分题的那个。"""
        if locator.startswith("key:"):
            return await s.case(phase1[locator[4:]])
        text = locator[5:]
        messages = select(Message.id).where(Message.content == text)
        posts = select(Post.id).where(Post.content == text)
        rows = await db.execute(
            select(ReportCase).where(
                ReportCase.status != "open", ReportCase.overturned_at.is_(None), ReportCase.verdict_source == "jury",
                or_(ReportCase.message_id.in_(messages), (ReportCase.target_type == "post") & ReportCase.target_id.in_(posts)),
            )
        )
        cases = list(rows.scalars().all())
        if not cases:
            raise RuntimeError(f"没有找到可以标为判例的案件：{text}")
        shown = await case_service.published_case_ids(db, [case.id for case in cases])
        taken = set((await db.execute(select(JuryPrecedent.case_id))).scalars().all())
        return sorted(cases, key=lambda case: (case.id in taken, case.id not in shown, bank_service.question_pool(case) == "scored", case.id))[0]

    async def mark_precedent(locator: str, title: str, summary: str, days: float, by: str | None) -> tuple[int, int, str]:
        case = await find_case(locator)
        if by is None:
            voters = (await db.execute(select(JuryVote.juror_id).where(JuryVote.case_id == case.id, JuryVote.voted_at.is_not(None), JuryVote.is_quality_check.is_(False)))).scalars().all()
            by = next((seniors[juror_id] for juror_id in sorted(voters) if juror_id in seniors), "admin")
        if by == "admin":
            view = await precedent_service.admin_create(db, admin, case.id, title, summary)
        else:
            user = await s.user(by)
            view = await precedent_service.create_by_senior(db, user, await points_service.review_id(db, user.id, case.id), title, summary)
        at = max(s.ago(days=days), penalty_service.aware(case.closed_at) + timedelta(hours=3))
        await db.execute(update(JuryPrecedent).where(JuryPrecedent.id == view["id"]).values(created_at=at, updated_at=at))
        await s.stamp_new(at)
        await db.commit()
        return view["id"], case.id, by

    marked = [await mark_precedent(*item) for item in PRECEDENTS]
    locator, title, summary, days, withdraw_note, withdrawn_days = PRECEDENT_WITHDRAWN
    withdrawn_id, withdrawn_case, _by = await mark_precedent(locator, title, summary, days, None)
    await precedent_service.admin_withdraw(db, admin, withdrawn_id, withdraw_note)
    await db.execute(update(JuryPrecedent).where(JuryPrecedent.id == withdrawn_id).values(withdrawn_at=s.ago(days=withdrawn_days), updated_at=s.ago(days=withdrawn_days)))
    await s.stamp_new(s.ago(days=withdrawn_days))
    await db.commit()
    special.update(precedents=[{"id": item[0], "case": item[1], "by": item[2]} for item in marked], precedent_withdrawn={"id": withdrawn_id, "case": withdrawn_case})

    # ================================================================== 议事厅
    hall: dict[str, int] = {}

    async def hall_post(key: str, author: str, kind: str, title: str, content: str, at: datetime, votes: dict[str, str] | None = None) -> int:
        """发帖（提案的话接着表决），再把时间改到 at。表决要在帖子还「开着」的时候投，所以先投票、后改时间。"""
        if kind == "announcement":
            post_id = (await hall_service.admin_create_announcement(db, admin, title, content, key == "pinned"))["id"]
        else:
            post_id = (await hall_service.create_post(db, await s.user(author), kind, title, content))["id"]
        for name, vote in (votes or {}).items():
            await hall_service.cast_vote(db, await s.user(name), post_id, vote)
        post = await db.get(JuryHallPost, post_id)
        await db.refresh(post)
        post.created_at = post.updated_at = post.last_activity_at = at
        if kind == "proposal":
            post.vote_ends_at = at + timedelta(days=hall_service.VOTE_DAYS)
            span = min(timedelta(days=hall_service.VOTE_DAYS - 1), s.now - at - timedelta(minutes=30))
            rows = (await db.execute(select(JuryHallVote).where(JuryHallVote.post_id == post_id).order_by(JuryHallVote.id.asc()))).scalars().all()
            for index, row in enumerate(rows):
                row.created_at = row.updated_at = at + span * ((index + 1) / (len(rows) + 1))
        await s.stamp_new(at)
        await db.commit()
        hall[key] = post_id
        return post_id

    async def hall_comments(post_key: str, items: list[tuple]) -> dict[str, int]:
        """按顺序发评论：(名字, 作者, 内容, 距今小时数, 回复哪一条的名字)。返回 名字 -> 评论 id。"""
        ids: dict[str, int] = {}
        latest: datetime | None = None
        for name, author, content, hours, *parent in items:
            view = await hall_service.create_comment(db, await s.user(author), hall[post_key], content, ids[parent[0]] if parent else None)
            at = s.ago(hours)
            await db.execute(update(JuryHallComment).where(JuryHallComment.id == view["id"]).values(created_at=at))
            await db.commit()
            ids[name] = view["id"]
            latest = at if latest is None or at > latest else latest
        if latest is not None:
            await db.execute(update(JuryHallPost).where(JuryHallPost.id == hall[post_key]).values(last_activity_at=latest))
            await db.commit()
        return ids

    async def hall_report(target_type: str, target_id: int, reporter: str, reason: str, hours: float, details: str | None = None) -> int:
        await hall_service.create_report(db, await s.user(reporter), target_type, target_id, reason, details)
        row = (
            await db.execute(select(JuryHallReport).where(JuryHallReport.target_type == target_type, JuryHallReport.target_id == target_id, JuryHallReport.reporter_id == s.uid(reporter)))
        ).scalar_one()
        row.created_at = s.ago(hours)
        await db.commit()
        return row.id

    async def hall_settle(key: str, expected: str) -> Any:
        """表决到期的提案出结果（定时任务里的同一个函数）。"""
        await hall_service.settle_due_proposals(db)
        post = await db.get(JuryHallPost, hall[key])
        await db.refresh(post)
        if post.status != expected:
            raise RuntimeError(f"提案「{post.title}」应该是 {expected}，实际是 {post.status}")
        post.settled_at = post.updated_at = post.vote_ends_at
        await db.commit()
        return post

    async def hall_decide(key: str, action: str, note: str | None, at: datetime) -> None:
        await hall_service.admin_decide(db, admin, hall[key], action, note)
        post = await db.get(JuryHallPost, hall[key])
        await db.refresh(post)
        post.decided_at = post.updated_at = at
        await s.stamp_new(at)
        await db.commit()

    await hall_post(
        "pinned", "admin", "announcement", "议事厅开放说明（发帖前先看这里）",
        "议事厅今天起向所有人开放阅读。这里是风纪委员公开讨论规则和评审方式的地方，实名发言。\n\n"
        "怎么用：\n· 提案：正式、资深委员可以发。在任委员表决 7 天，参与人数和赞成比例都达标就算通过，之后由管理员决定是否采纳，并说明理由。\n"
        "· 讨论：正式、资深委员可以发，所有在任委员（包括见习）都可以评论。\n· 公告：由管理员发布。\n\n"
        "几条要紧的规矩：不要讨论还没结的案件；对事不对人；不要贴没有匿名的聊天记录，也不要猜当事人或评审委员是谁。完整的守则在页面顶部。\n\n"
        "看到违反守则的帖子或评论可以举报，被多名在任委员举报的会先折叠，等管理员处理。",
        s.ago(days=14),
    )
    await hall_post(
        "news", "admin", "announcement", "三期功能上线：资深合议、申诉复审、判例",
        "三期的功能已经全部打开，简单说一下变化：\n\n"
        "一、资深合议。委员没能定案的案件（五票没有共识、两张「上下文不足」、72 小时超时）先交给三位资深委员，两票一致就定案；48 小时没有结果再转管理员。\n"
        "二、申诉复审。委员定案的处罚和机器自动处罚，申诉先由三位资深委员复审：维持、减轻或撤销，两票相同出结果。私聊相关的、超过 7 天的封禁仍然直接到管理员。\n"
        "三、判例。资深委员可以把自己投过票的已定案件标成判例，写上标题和裁判要旨，全站可读。拿不准的时候先翻翻判例。\n"
        "四、举报人复议。举报被委员认定不违规的，举报人可以在 7 天内申请一次复议。\n\n有问题直接在议事厅发讨论帖。",
        s.ago(days=3),
    )
    # 已被采纳的提案
    await hall_post(
        "adopted", "senior3", "proposal", "建议在投票页直接显示举报理由对应的类别说明",
        "现在的情况：投违规票时要选类别，但页面上只有类别的名字。「辱骂骚扰」和「脏话」、「垃圾广告」和「诈骗」的界线，新委员经常选错。\n\n"
        "建议：在类别的选项旁边加一行说明（各一句话，带一个例子），不用跳出去查。\n\n理由：选错类别不影响违规与否，但影响定案的类别和默认等级，最后会影响处罚轻重。",
        s.ago(days=13), {"senior1": "yes", "juror": "yes", "regular2": "yes", "trainee1": "yes", "m011": "yes", "senior2": "no"},
    )
    await hall_settle("adopted", "passed")
    await hall_decide("adopted", "adopt", "采纳。类别说明会放进 /jury/config 的 categories 里，随下一个版本下发，客户端在选项旁边展示。", s.ago(days=5))
    # 通过了、但没有被采纳的提案
    await hall_post(
        "declined", "senior2", "proposal", "建议取消每天 30 件的评审上限",
        "现在的情况：每位委员最近 24 小时最多评审 30 件。周末案件多的时候，我经常下午就用完了。\n\n建议：取消这个上限，或者至少对资深委员取消。\n\n"
        "理由：愿意多评的人被拦住，积压的案件只能等超时。",
        s.ago(days=12), {"senior1": "yes", "senior3": "yes", "regular2": "yes", "m012": "yes", "m013": "yes", "trainee2": "yes", "juror": "no", "m011": "no"},
    )
    await hall_settle("declined", "passed")
    await hall_decide("declined", "decline", "暂不采纳。上限是为了保证每一票的质量：连续评审超过 30 件之后，质控题的正确率有明显下降。会评估对资深委员放宽到 50 件，有结论了另行公告。", s.ago(days=4))
    # 没有通过的提案
    await hall_post(
        "rejected", "regular2", "proposal", "建议见习委员的票只算半票",
        "现在的情况：见习、正式、资深委员的票分量一样。\n\n建议：见习委员的票按 0.5 张计入表决。\n\n理由：见习委员经验少，容易投错。",
        s.ago(days=11), {"regular2": "yes", "m013": "yes", "juror": "no", "senior1": "no", "senior2": "no", "trainee1": "no", "trainee2": "no", "m014": "no"},
    )
    await hall_settle("rejected", "rejected")
    # 通过了、等管理员决定是否采纳的提案
    await hall_post(
        "passed", "senior1", "proposal", "建议「上下文不足」转合议时，把委员写的备注一起带上",
        "现在的情况：两张「上下文不足」的案件会转给资深合议，但合议委员只能看到票数，看不到原审委员觉得缺的是什么。\n\n"
        "建议：投「上下文不足」时写的备注，在合议页面的「原审观点」里优先展示。\n\n理由：合议委员拿到的上下文和原审是同一份，知道原审卡在哪里，才好判断是真的缺上下文，还是可以直接下结论。",
        s.ago(days=10), {"juror": "yes", "regular2": "yes", "senior2": "yes", "senior3": "yes", "trainee1": "yes", "m011": "yes", "m013": "no"},
    )
    await hall_settle("passed", "passed")
    # 表决中：主账号还没投。现在 3 赞成 1 反对，法定人数 5 人 —— 再有一张赞成票，人数和比例就都够了
    await hall_post(
        "open_close", "senior2", "proposal", "建议把「刷屏」的默认等级从轻微调到一般",
        "现在的情况：「刷屏」的默认等级是轻微（记 1 分，只给提示）。\n\n建议：调到一般（记 3 分，警告）。\n\n"
        "理由：最近的刷屏案件里，大部分是故意把别人的求助顶上去，一句提示基本没有约束力。真的只是手滑多发了几条的，委员可以手动选轻微。",
        s.ago(days=5), {"senior1": "yes", "regular2": "yes", "m011": "yes", "trainee2": "no"},
    )
    # 表决中：主账号投了赞成。2 比 2，离法定人数还差一人
    await hall_post(
        "open_voted", "regular2", "proposal", "建议质控题答错之后，给出同类判例的链接",
        "现在的情况：质控题答错只显示标准答案，有讲解的显示讲解，很多题没有讲解。\n\n建议：答错时在标准答案下面列出同类别的判例（有的话）。\n\n理由：判例里的要旨比一句讲解有用，也能让大家多翻翻判例。",
        s.ago(days=2), {"juror": "yes", "senior1": "yes", "senior3": "no", "m012": "no"},
    )
    # 讨论帖：评论区里什么情况都有
    await hall_post(
        "discussion", "senior1", "discussion", "「开玩笑」和「辱骂」的界线，大家平时怎么判断？",
        "最近合议里经常遇到这类案件：话本身不好听，但被举报人说是在开玩笑。原审的票基本是对半开。\n\n我自己的习惯是看三样：被说的人什么反应、两个人之前有没有互动、"
        "这句话去掉「哈哈哈」之后还剩什么。想听听大家是怎么判断的，尤其是被说的人没有回应的时候。",
        s.ago(days=8),
    )
    comments = await hall_comments(
        "discussion",
        [
            ("c1", "juror", "我一般先看被说的那个人什么反应。他接话了、笑了，基本就是玩笑；他说「没必要这么说吧」，那就不是。", 190),
            ("c1a", "trainee1", "同意。不过大厅里很多人被骂了也不回，不回不等于不介意。", 188, "c1"),
            ("c1b", "senior1", "对，所以没有反应的时候，我会再看这两个人之前有没有正常的互动。", 186, "c1a"),
            ("c1c", "juror", "这个办法好，我之前没注意过。上下文里看不出来的话是不是就该投「上下文不足」？", 185, "c1b"),
            ("c2", "regular2", "我会把那句话里的「哈哈哈」「狗头」去掉再读一遍。去掉之后还是在骂人的，就按骂人算。", 180),
            ("c2a", "hesitant", "那「笨蛋哈哈哈积木拖反了」这种呢？去掉之后也是骂人，但明显是在帮忙。", 176, "c2"),
            ("c2b", "regular2", "看后半句。后面跟着的是答案，就不是冲着人去的。这个有判例，可以去翻。", 174, "c2a"),
            ("c3", "senior3", "我觉得关键是有没有具体的对象。泛泛地吐槽「现在的新人都不看教程」，和指着一个人说，是两回事。", 170),
            ("c3a", "trainee2", "刚看到上面那条删掉了。不过里面说的「看有没有具体对象」我觉得是对的。", 160, "c3"),
            ("c4", "m014", "昨天分到一个大厅里骂新手的案子，被举报的那个人我大概猜得到是谁，就是头像是蓝色小恐龙的那个，他平时就这样。", 150),
            ("c5", "cooldown", "说个题外话，有人知道这周的社区比赛什么时候截止吗？", 140),
            ("c6", "trainee1", "发错地方了", 130),
            ("c7", "m015", "插一句：我朋友开了个代肝工作室，价格很便宜，需要的可以找我。", 120),
            ("c8", "senior2", "补充一个角度：举报人是不是当事人。路过的人替别人举报的，要多看一眼当事人自己的反应。", 110),
            ("c8a", "m012", "这个我有体会，上周复议的时候就碰到一件。", 100, "c8"),
        ],
    )
    await hall_service.delete_comment(db, await s.user("senior3"), comments["c3"])   # 根评论，下面有回复：留一个占位
    await hall_service.delete_comment(db, await s.user("trainee1"), comments["c6"])  # 根评论，没有回复：不再出现
    await hall_service.delete_comment(db, await s.user("m012"), comments["c8a"])     # 回复：不再出现
    await db.execute(update(JuryHallComment).where(JuryHallComment.id.in_([comments["c3"], comments["c6"], comments["c8a"]])).values(deleted_at=s.ago(95)))
    await db.commit()
    # 被三名在任委员举报：自动折叠，等管理员处理
    for reporter, reason, hours in (("senior1", "讨论未结案件", 148), ("regular2", "泄露身份或隐私", 146), ("trainee1", "讨论未结案件", 141)):
        await hall_report("comment", comments["c4"], reporter, reason, hours)
    # 不是委员的用户举报：照常记下来给管理员看，但不计入折叠
    await hall_report("comment", comments["c5"], "eligible", "与风纪无关", 90)
    # 已经处理过的两条举报：一条删除了评论，一条驳回
    handled = await hall_report("comment", comments["c7"], "juror", "与风纪无关", 118)
    await hall_service.admin_handle_report(db, admin, handled, "delete", "广告，删除")
    dismissed = await hall_report("comment", comments["c2"], "trainee2", "其他", 172, "感觉在针对新人")
    await hall_service.admin_handle_report(db, admin, dismissed, "dismiss", "正常讨论，举报不成立")
    await db.execute(update(JuryHallReport).where(JuryHallReport.id.in_([handled, dismissed])).values(handled_at=s.ago(96)))
    await db.execute(update(JuryHallComment).where(JuryHallComment.id == comments["c7"]).values(deleted_at=s.ago(96)))
    await s.stamp_new(s.ago(96))
    await db.commit()
    special["hall_comments"] = comments
    # 被管理员关闭的讨论帖：还能看，不能再评论
    await hall_post(
        "closed", "senior2", "discussion", "合议的 48 小时期限是不是太短了？",
        "这周有一件合议超时转给了管理员：我投了票，另外两位一直没上线。三个人里只要有一个人周末不在，48 小时就过去了。\n\n想问问大家：是期限太短，还是应该在快到期的时候提醒一下？",
        s.ago(days=9),
    )
    await hall_comments(
        "closed",
        [
            ("d1", "senior3", "我觉得不是期限的问题，是没有提醒。分到手的合议案件我经常是第二天才看到。", 210),
            ("d2", "senior1", "加提醒 +1。另外席位空着的时候能不能自动换人？", 206),
            ("d3", "regular2", "虽然我还不是资深，但从被处理的人的角度看，48 小时已经不短了。", 200, "d2"),
        ],
    )
    await hall_decide("closed", "close", "这个问题已经记下了：合议快到期时会给还没投的委员发提醒，排在下个版本。讨论先到这里。", s.ago(days=7))
    # 主账号发的讨论帖
    await hall_post(
        "mine", "juror", "discussion", "质控题答错了，觉得标准答案有问题，可以反馈吗？",
        "前几天遇到一道质控题：有人说「这个 bug 气死我了，垃圾代码」，后面括号里写着是自己写的。我投了不违规，标准答案也是不违规，这道没问题。\n\n"
        "但另一道我投了轻微违规，标准答案是不违规，讲解只有一句「语气词而已」。我不是想改分，只是想知道这种情况应该找谁反馈，题目会不会重新核对。",
        s.ago(days=4),
    )
    await hall_comments(
        "mine",
        [
            ("e1", "senior2", "可以的。在这里发帖就行，说清楚是哪一类题（不用贴原文），管理员会去题库里重新核对。", 90),
            ("e2", "trainee2", "原来可以反馈，我一直以为答错就是答错了。", 84),
            ("e3", "juror", "好的，谢谢。那我整理一下再发。", 80, "e1"),
            ("e4", "m016", "同问，我也遇到过一道讲解和判例对不上的。", 30),
        ],
    )
    # 被三名在任委员举报后折叠的帖子：列表里只剩占位的标题
    await hall_post(
        "hidden", "m012", "discussion", "吐槽：有些人就是专门来举报别人的",
        "不点名，但大厅里那个 ID 带「小艾」的，一天能举报十几条，十条里有八条都是不违规。我们的时间不是时间吗？这种人能不能直接禁止举报。",
        s.ago(days=6),
    )
    for reporter, reason, hours in (("senior1", "人身攻击", 140), ("senior3", "泄露身份或隐私", 136), ("trainee2", "人身攻击", 131)):
        await hall_report("post", hall["hidden"], reporter, reason, hours)
    # 刚发的讨论帖，作者是追加的那几位委员之一
    await hall_post(
        "latest", "m011", "discussion", "「拿不准」和「上下文不足」到底该怎么选？",
        "我的理解：上下文看全了，还是判断不了，投「拿不准」；觉得再多看几句就能判断，投「上下文不足」。\n\n不知道对不对。另外「上下文不足」攒够两张会转合议，这种时候我是不是应该尽量写备注？",
        s.ago(20),
    )
    await hall_comments("latest", [("f1", "hesitant", "我也经常分不清，等一个资深委员的回答。", 16), ("f2", "senior3", "理解是对的。备注一定要写：现在合议委员只看得到票数（有个提案就是在解决这件事）。", 9, "f1")])
    states = dict((await db.execute(select(JuryHallPost.id, JuryHallPost.state))).all())
    if states[hall["hidden"]] != "hidden" or (await db.get(JuryHallComment, comments["c4"])).state != "hidden":
        raise RuntimeError("被三名在任委员举报的帖子和评论应该已经自动折叠")
    special["hall"] = hall

    # ================================================================== 收尾
    # 一致通过、定案满 72 小时的案件自动进题库（定时任务里的同一个函数）：现在做掉，服务起来之后题库就不会马上再变
    await bank_service.auto_include(db)
    await s.stamp_new(s.ago(1))
    await db.commit()
    # 练习场多了几道题（判例、新入库的案件）：exampass 仍然是「做完了一整轮」
    state = (await exam_service.practice_next(db, await s.user("exampass")))["round"]
    extra = state["total"] - state["done"]
    if extra > 15:
        raise RuntimeError(f"练习场一下多了 {extra} 道题，不对劲")
    if extra and await s.practice("exampass", [s.ago(30 - 0.2 * index) for index in range(extra)]) != extra:
        raise RuntimeError("exampass 没能把新增的练习题做完")
    # 公开了身份的委员在大厅里又说了几句：大厅最近的消息里仍然能看到徽章
    for username, text, minutes in [
        ("senior1", "议事厅开了，公告在置顶，大家有空可以去看看", 21), ("m259", "议事厅是什么", 19), ("regular2", "委员讨论规则的地方，谁都能看", 17),
        ("senior2", "新标了几条判例，拿不准的时候可以翻一翻", 12), ("m274", "好的", 10),
    ]:
        await s.message("plaza", username, text, s.ago(minutes=minutes))
    await db.commit()
    # 两位「今天还没投票」的见习委员。放在议事厅之后任命：提案的法定人数是发帖那一刻按在任委员数定下的，不受他们影响
    for username, tier, days, term_left, slots in LATE_JURORS:
        member = await jury_service.review_member(db, s.uid(username), action="approve", actor_id=admin.id, note="三期上线后任命", tier=tier)
        member.statement = "想参与评审"
        member.applied_at = s.ago(days=days + 1)
        member.reviewed_at = s.ago(days=days)
        member.term_ends_at = s.now + timedelta(days=term_left)
        await s.stamp_new(s.ago(days=days))
        await db.commit()
        for days_ago, hour in slots:
            question = (await jury_service._assignable_cases(db, s.uid(username), golden=True, limit=1))[0]
            await s.vote(question.id, username, "l2" if bank_service.question_upheld(question) else "none", s.local_time(days_ago, hour), quality=True)
    # 连续参与天数按实际投票的日子重算；追加的委员的在任天数改成真实的
    await s.rebuild_streaks()
    for username, _tier, days, _term in [*BG_JURORS, *[item[:4] for item in LATE_JURORS]]:
        member = await jury_service.get_member(db, s.uid(username))
        member.serving_since = s.ago(days=days)
    await db.commit()
    db.add(SystemSetting(key="jury_demo.cases3", value=json.dumps(special, ensure_ascii=False)))
    await db.commit()


# ====================================================================== 红包（docs/red-packets.md）

# 假账本给每个绑定了 CCW 的演示账号记多少累计投币
DEMO_COINS = 1000


def install_demo_ledger() -> None:
    """给演示实例一个假的余额来源：凡是演示账号的 CCW 标识（demo 开头）都算累计投了 DEMO_COINS 个币。

    只在这个脚本的进程里生效（integrations.set_coin_ledger），不发任何外部请求；灌数据和之后起的服务是同一个进程，所以都用它。
    """
    from app import integrations
    from app.integrations import CoinLedger, LedgerSource

    class DemoCoinLedger(CoinLedger):
        def configured(self) -> bool:
            return True

        def source(self) -> LedgerSource:
            return LedgerSource(id="jury-demo", setting=None)

        def topup_url(self) -> str | None:
            return None

        async def credited_to(self, account_id: str) -> int:
            return DEMO_COINS if account_id.startswith("demo") else 0

        async def recent_credits(self, limit: int | None = None) -> dict[str, int]:
            credits = await self.all_credits()
            return dict(list(credits.items())[:limit]) if limit is not None else credits

        async def all_credits(self, *, fresh: bool = False) -> dict[str, int]:
            async with AsyncSessionLocal() as db:
                rows = await db.execute(select(User.ccw_student_oid).where(User.ccw_student_oid.like("demo%")))
                return {oid.strip().lower(): DEMO_COINS for oid in rows.scalars().all()}

    integrations.set_coin_ledger(DemoCoinLedger())


async def seed_red_packets(s: Seeder) -> None:
    """打开红包开关，在「深夜灌水小分队」里放三个红包：一个进行中的拼手气（被抢了 3 份）、一个已抢完的拼手气、一个没人动过的普通红包。

    都走服务层（扣款、分份、落消息、领取），所以余额、明细、手气最佳都是真的。时间就是灌数据的时刻，24 小时后过期。
    """
    db = s.db
    await s.load()
    db.add(SystemSetting(key="red_packet.enabled", value="1"))
    await db.commit()
    red_packet_service.reset_cache()
    lounge = s.conversation_ids["lounge"]

    async def send(sender: str, kind: str, amount: int, count: int, greeting: str) -> int:
        result, _message = await red_packet_service.send_red_packet(
            db, await s.user(sender), {"conversation_id": lounge, "kind": kind, "amount": amount, "count": count, "greeting": greeting}
        )
        return result["red_packet"]["id"]

    async def claim(packet_id: int, *usernames: str) -> None:
        for username in usernames:
            await red_packet_service.claim(db, await s.user(username), packet_id)

    finished = await send("senior1", "lucky", 60, 5, "开工大吉")
    await claim(finished, "juror", "trainee1", "trainee2", "regular2", "senior2")
    active = await send("juror", "lucky", 88, 8, "周末愉快，手快有")
    await claim(active, "trainee1", "senior1", "regular2")
    untouched = await send("senior2", "normal", 5, 6, "恭喜发财")
    db.add(SystemSetting(key="jury_demo.red_packets", value=json.dumps({"finished": finished, "active": active, "untouched": untouched})))
    await db.commit()
    # 灌数据时攒下的实时推送（没人在线，发不到哪儿去）在这个事件循环里跑完，别带到服务的事件循环里
    await shop_service.drain()


# ====================================================================== 礼物、生日、限时商品（docs/gifts.md、docs/shop.md 2.5）


async def seed_gifts(s: Seeder) -> None:
    """打开礼物开关，灌四条送礼记录；trainee1 今天过生日；juror 戴着一件两天后到期的限时头像框。

    都走服务层（扣款、记录、心意值、对方的拥有记录、落消息），所以余额、心意榜、礼物墙都是真的。时间就是灌数据的时刻：
    「今天生日」只在灌数据的那一天（UTC+8）成立，放久了要 --reset。
    """
    db = s.db
    await s.load()
    # 商品和礼物目录平时是服务启动时播种的；灌数据在那之前，这里先播一遍（幂等）
    await shop_service.ensure_default_items(db)
    await gift_service.ensure_default_gifts(db)
    db.add(SystemSetting(key="gifts.enabled", value="1"))
    await db.commit()
    gift_service.reset_cache()
    lounge = s.conversation_ids["lounge"]

    # 今天（UTC+8）过生日的账号。年份用闰年，哪一天灌数据日期都合法
    today = datetime.now(UTC).astimezone(timezone(timedelta(hours=8))).date()
    await birthday_service.set_birthday(db, await s.user("trainee1"), {"year": 2008, "month": today.month, "day": today.day, "show_year": False})
    # 另一个设了生日、公开年份、但不是今天的
    other = today - timedelta(days=40)
    await birthday_service.set_birthday(db, await s.user("senior1"), {"year": 2006, "month": other.month, "day": other.day, "show_year": True})

    async def send(sender: str, to: str, conversation_id: int | None, **extra: Any) -> int:
        payload = {"to_user_id": s.uid(to), "conversation_id": conversation_id, "kind": "gift", "count": 1, "message": None, **extra}
        result, _message = await gift_service.send_gift(db, await s.user(sender), payload)
        return result["record"]["id"]

    records = {
        # 生日当天、带留言、在群里（on_birthday = true，群成员都看得到卡片上的留言）
        "birthday": await send("juror", "trainee1", lounge, gift_key="cake", count=3, message="生日快乐！今天你最大"),
        # 送一件头像框（限时 30 天）
        "frame": await send("senior1", "trainee1", lounge, kind="item", item_sku="frame.angel", message="戴上试试"),
        # 从个人空间送的：不落消息，留言只有双方看得到
        "profile": await send("senior2", "juror", None, gift_key="flower", count=9, message="谢谢你上次帮忙看案子"),
        # 全屏动效的那一档，不带留言
        "rocket": await send("regular2", "senior1", lounge, gift_key="rocket"),
    }

    # juror：一件快到期的限时头像框，戴着。先按 30 天送，再把到期时间改到两天后 —— 清扫会发一次 shop.item.expiring，
    # 两天后到期：自动卸下、发 shop.item.expired
    devil = (await db.execute(select(ShopItem).where(ShopItem.sku == "frame.devil"))).scalar_one()
    await shop_service.admin_grant(db, await s.user("admin"), s.uid("juror"), devil.id, "演示：快到期的限时头像框")
    expires_at = datetime.now(UTC) + timedelta(days=2)
    await db.execute(
        update(ShopPurchase)
        .where(ShopPurchase.user_id == s.uid("juror"), ShopPurchase.item_id == devil.id, ShopPurchase.superseded_at.is_(None))
        .values(expires_at=expires_at, expiring_notified_at=None)
    )
    await db.commit()
    await shop_service.set_equipped(db, await s.user("juror"), "frame", "frame.devil")
    db.add(SystemSetting(key="jury_demo.gifts", value=json.dumps({**records, "birthday_user": s.uid("trainee1"), "expiring_user": s.uid("juror")})))
    await db.commit()
    await s.stamp_new(s.now)
    await db.commit()
    # 灌数据时攒下的实时推送（没人在线，发不到哪儿去）在这个事件循环里跑完，别带到服务的事件循环里
    await shop_service.drain()


# 以后再加数据往这里追加（名字, 函数）。已经灌过的期不会重跑。
PHASES: list[tuple[str, Any]] = [
    ("base", seed_base),
    ("phase1", seed_phase1),
    ("phase2", seed_phase2),
    ("phase3", seed_phase3),
    ("red_packets", seed_red_packets),
    ("gifts", seed_gifts),
]


# ====================================================================== 建库与自检


async def _seed_daily_rewards(db: Any, member: Any, now: datetime) -> list[Any]:
    """灌数据时代替 jury_service._daily_rewards。

    服务层按「现在」记每日首票和连续天数；灌的是过去的票，所以每日首票记在这一票应该发生的那天（_SEED_CLOCK），
    连续天数不在这里记，最后由 Seeder.rebuild_streaks 按时间顺序统一重放。
    """
    day = points_service.local_day(_SEED_CLOCK or now)
    return [await points_service.award(db, member, "daily_first", ref_id=day.toordinal(), announce=False)]


async def prepare_database() -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        rows = await db.execute(select(SystemSetting.key, SystemSetting.value).where(SystemSetting.key.like("jury_demo.%")))
        state = dict(rows.all())
    if any(value == "running" for key, value in state.items() if key.startswith("jury_demo.phase.")):
        await engine.dispose()
        die("上一次灌数据中途断了，库是不完整的。请加 --reset 重建。")
    seeded_at = datetime.fromisoformat(state["jury_demo.seeded_at"]) if state.get("jury_demo.seeded_at") else datetime.now(UTC)
    pending = [(name, function) for name, function in PHASES if state.get(f"jury_demo.phase.{name}") != "done"]
    if not pending:
        age = (datetime.now(UTC) - seeded_at).total_seconds() / 3600
        log(f"演示库已存在（生成于 {seeded_at.astimezone().strftime('%Y-%m-%d %H:%M')}，{age:.0f} 小时前），不重复灌数据。")
        if age > 20:
            log("提醒：数据里的时间是按生成时刻算的，放久了会变旧（待评审的案件超时、冷却结束、周榜清空……）。需要干净的初始状态请 --reset。")
    daily_rewards = jury_service._daily_rewards
    jury_service._daily_rewards = _seed_daily_rewards
    try:
        for name, function in pending:
            log(f"灌数据：{name} …")
            started = datetime.now(UTC)
            async with AsyncSessionLocal() as db:
                if "jury_demo.seeded_at" not in state:
                    db.add(SystemSetting(key="jury_demo.seeded_at", value=seeded_at.isoformat()))
                    state["jury_demo.seeded_at"] = seeded_at.isoformat()
                db.add(SystemSetting(key=f"jury_demo.phase.{name}", value="running"))
                await db.commit()
            async with AsyncSessionLocal() as db:
                await function(Seeder(db, seeded_at))
            async with AsyncSessionLocal() as db:
                await db.execute(update(SystemSetting).where(SystemSetting.key == f"jury_demo.phase.{name}").values(value="done"))
                await db.commit()
            log(f"灌数据：{name} 完成（{(datetime.now(UTC) - started).total_seconds():.1f} 秒）")
    finally:
        jury_service._daily_rewards = daily_rewards
        random.seed()
    await print_summary(check=bool(pending))
    # 灌数据和起服务在同一个进程里：服务层留在模块里的节流 / 缓存状态要清掉，
    # 否则刚 --reset 完的头 30 秒收不到 jury.queue.changed。
    case_service._last_queue_event_at = None
    overview_service.reset_overview_cache()
    points_service.reset_leaderboard_cache()
    jury_settings.reset_cache()
    shop_service.reset_runtime_state()
    red_packet_service.reset_runtime_state()
    gift_service.reset_runtime_state()
    # 两者也不在同一个事件循环里：把连接池清掉，服务启动时重新连
    await engine.dispose()


async def print_summary(*, check: bool) -> None:
    """打印库里现在的样子。check 为真（刚灌完数据）时，顺带核对走查依赖的前提，不满足直接报错。"""
    async with AsyncSessionLocal() as db:
        overview_service.reset_overview_cache()
        now = datetime.now(UTC)
        users = {username: user_id for user_id, username in (await db.execute(select(User.id, User.username))).all()}
        done = set((await db.execute(select(SystemSetting.key).where(SystemSetting.key.like("jury_demo.phase.%"), SystemSetting.value == "done"))).scalars().all())
        statuses: dict[str, dict[str, Any]] = {}
        for username, _nickname, _role, _use in ACCOUNTS:
            if username in users:
                statuses[username] = await jury_service.my_status(db, await db.get(User, users[username]))
        status = statuses["juror"]
        normal = await jury_service._assignable_cases(db, users["juror"], golden=False, limit=50)
        golden = await jury_service._assignable_cases(db, users["juror"], golden=True, limit=50)
        member = await jury_service.get_member(db, users["juror"])
        community = await overview_service._community(db)
        _items, board_total = await overview_service.public_cases(db, limit=1)
        _cases, _total, open_counts = await case_service.list_cases(db, limit=1)
        scored = await bank_service.verified_count(db)
        practice = int((await db.execute(select(func.count(ReportCase.id)).where(*bank_service.bank_clauses(pool="practice")))).scalar_one())
        boards = {period: await points_service._ranking(db, period, now) for period in points_service.LEADERBOARD_PERIODS}
        log(f"在任委员：{community['jurors']}")
        log(f"未结案件：{open_counts}；公示栏 {board_total} 件；近 30 天样本 {community['sample']}（共识率 {community['consensus_rate']}）")
        log(f"题库：考试和质控用的 {scored} 道，练习场用的 {practice} 道；排行榜上榜人数 周 {len(boards['week'])} / 月 {len(boards['month'])} / 总 {len(boards['all'])}")
        log(
            f"主账号 juror：{status['tier']}，风纪值 {status['points']}，可评审 {status['available_cases']} 件（另有没做过的质控题 {len(golden)} 道），"
            f"累计 {member.votes_total} 票，一致率 {status['stats']['agreement_rate']}，连续 {points_service.current_streak(member, points_service.local_day(now))} 天，"
            f"距上一道质控题 {member.votes_since_quality} 票"
        )
        log("主账号的待评审案件（后台的真实编号/类型，按分案顺序）：" + "、".join(f"#{case.id}/{case.target_type}" for case in normal))
        if "jury_demo.phase.phase3" in done:
            await _phase3_summary(db, statuses)
        if not check:
            return
        problems: list[str] = []
        if status["status"] != "active" or status["restriction"] is not None:
            problems.append(f"主账号状态不对：{status['status']}")
        if len(normal) < 6 or len(golden) < 3:
            problems.append(f"主账号能拿到的案件不够：普通 {len(normal)}，质控 {len(golden)}")
        if not community["sample_enough"] or community["sample"] < 5:
            problems.append("总览的样本不够，比率出不来")
        expected = {
            "trainee1": "active", "trainee2": "active", "regular2": "active", "senior1": "active", "senior2": "active", "senior3": "active", "mutedjuror": "active",
            "hesitant": "active", "cooldown": "active", "suspended": "suspended", "expired": "expired", "applicant": "applied",
            "eligible": "none", "eligible2": "none", "examfail": "none", "exampass": "none", "examout": "none", "newbie": "none",
        }
        for username, wanted in expected.items():
            other = statuses[username]
            if other["status"] != wanted:
                problems.append(f"{username} 的委员状态是 {other['status']}，应该是 {wanted}")
            if username != "newbie" and wanted == "none" and not other["eligible"]:
                problems.append(f"{username} 应该满足全部申请条件")
        if statuses["newbie"]["eligible"]:
            problems.append("newbie 不应该满足申请条件")
        if (statuses["mutedjuror"]["restriction"] or {}).get("reason") != "muted":
            problems.append("mutedjuror 应该处于禁言受限状态")
        for name in ("senior1", "senior2", "senior3"):
            if statuses[name]["tier"] != "senior":
                problems.append(f"{name} 应该还是资深委员，现在是 {statuses[name]['tier']}")
        # 每个案件快照里的消息时间都要随顺序递增
        snapshots = await db.execute(select(UserReport.id, UserReport.case_id, UserReport.target_snapshot).where(UserReport.target_snapshot.is_not(None)))
        for report_id, case_id, text in snapshots.all():
            times = [item.get("created_at") for item in (json.loads(text).get("messages") or []) if isinstance(item, dict)]
            if any(left > right for left, right in zip(times, times[1:])):
                problems.append(f"案件 #{case_id}（举报 {report_id}）的上下文消息时间不是递增的")
        for case_id, text in (await db.execute(select(ReportCase.id, ReportCase.snapshot).where(ReportCase.snapshot.is_not(None)))).all():
            times = [item.get("created_at") for item in (json.loads(text).get("messages") or []) if isinstance(item, dict)]
            if any(left > right for left, right in zip(times, times[1:])):
                problems.append(f"机器来源的案件 #{case_id} 的上下文消息时间不是递增的")
        if "jury_demo.phase.phase2" in done:
            problems.extend(await _phase2_problems(db, users, statuses, member, golden, scored, practice, boards, now))
        if "jury_demo.phase.phase3" in done:
            problems.extend(await _phase3_problems(db, users, statuses, normal, open_counts, now))
        if problems:
            die("演示数据没有达到预期：\n  - " + "\n  - ".join(problems))


async def _phase2_problems(
    db: Any, users: dict[str, int], statuses: dict[str, dict[str, Any]], member: Any, golden: list[Any], scored: int, practice: int, boards: dict[str, list[dict[str, Any]]], now: datetime
) -> list[str]:
    """二期的走查依赖的前提。"""
    problems: list[str] = []
    if scored < 15 or practice < 12:
        problems.append(f"题库不够：考试和质控用的 {scored} 道（要 ≥ 15），练习场用的 {practice} 道（要 ≥ 12）")
    juror = statuses["juror"]
    oks = [item["ok"] for item in (juror["tier_progress"] or {}).get("requirements", [])]
    if juror["tier"] != "regular" or not (any(oks) and not all(oks)) or juror["warnings"]:
        problems.append(f"主账号应该是正式委员、离资深差一部分条件、没有预警：tier={juror['tier']} 条件={oks} warnings={[item['code'] for item in juror['warnings']]}")
    # 距上一道质控题满 10 票后强制出题：现在 ≥ 5，前 6 次取案里一定有质控题
    if (member.votes_since_quality or 0) < 5 or points_service.current_streak(member, points_service.local_day(now)) < 7:
        problems.append(f"主账号距上一道质控题 {member.votes_since_quality} 票（要 ≥ 5），连续 {member.streak_days} 天（要 ≥ 7）")
    trainee = statuses["trainee1"]
    if trainee["tier"] != "trainee" or not all(item["ok"] for item in trainee["tier_progress"]["requirements"]):
        problems.append(f"trainee1 应该还是见习、但晋升条件已经全部满足：{trainee['tier']} {trainee['tier_progress']}")
    if await points_service._below_demote_line(db, users["trainee1"], points_service.TIER_RULES["regular"]["demote_below"]):
        problems.append("trainee1 最近的一致率低于正式委员的降级线，定案之后不会晋升")
    for name in ("trainee2", "hesitant", "cooldown", "mutedjuror"):
        other = statuses[name]
        if other["tier"] != "trainee" or all(item["ok"] for item in other["tier_progress"]["requirements"]):
            problems.append(f"{name} 不应该也满足全部晋升条件（「再定一案就晋升」的只应该有 trainee1）")
    if statuses["regular2"]["tier"] != "regular":
        problems.append("regular2 应该已经由系统自动晋升为正式委员")
    wanted = {"senior3": "demote_risk", "hesitant": "high_abstain"}
    for name, code in wanted.items():
        if code not in [item["code"] for item in statuses[name]["warnings"]]:
            problems.append(f"{name} 应该有 {code} 预警：{statuses[name]['warnings']}")
    if statuses["hesitant"]["cooldown_until"] is not None or statuses["cooldown"]["cooldown_until"] is None:
        problems.append(f"弃权冷却不对：hesitant={statuses['hesitant']['cooldown_until']} cooldown={statuses['cooldown']['cooldown_until']}")
    for period, rows in boards.items():
        if len(rows) < 8 or users["juror"] not in [row["user_id"] for row in rows] or not any(row["user"] for row in rows) or all(row["user"] for row in rows):
            problems.append(f"{period} 榜不对：{len(rows)} 人上榜（要 ≥ 8、主账号在榜、有公开的也有匿名的）")
    exams = {name: statuses[name]["exam"] for name in ("eligible", "eligible2", "examfail", "exampass", "examout", "applicant")}
    if not exams["eligible"]["available"] or exams["eligible"]["attempts_used"] or exams["eligible2"]["attempts_used"]:
        problems.append(f"eligible / eligible2 应该可以直接开考：{exams['eligible']}")
    if exams["examfail"]["passed"] or exams["examfail"]["next_attempt_at"] is None or exams["examfail"]["attempts_used"] != 1:
        problems.append(f"examfail 应该在考试冷却中：{exams['examfail']}")
    if not exams["exampass"]["passed"] or not exams["applicant"]["passed"]:
        problems.append("exampass / applicant 应该已经通过考试")
    if exams["examout"]["attempts_used"] != 3 or exams["examout"]["passed"]:
        problems.append(f"examout 应该已经用完三次机会：{exams['examout']}")
    kinds = set((await db.execute(select(JuryPointLog.kind).distinct())).scalars().all())
    missing = {"vote_agreed", "vote_disagreed", "quality_correct", "quality_wrong", "daily_first", "streak_7", "opinion_liked", "exam_passed", "overturn_adjust", "admin_adjust"} - kinds
    if missing:
        problems.append(f"风纪值流水里没有出现这些种类：{sorted(missing)}")
    notified = set((await db.execute(select(JuryNotification.kind).distinct())).scalars().all())
    if not {"promoted", "suspended", "term_expired"} <= notified:
        problems.append(f"通知中心的风纪通知不全：{sorted(notified)}")
    earned = int((await db.execute(select(func.count(JuryAchievement.id)).where(JuryAchievement.user_id == users["juror"]))).scalar_one())
    if not 3 <= earned < len(points_service.ACHIEVEMENTS):
        problems.append(f"主账号已获得的成就数不对：{earned}")
    badges = dict((await db.execute(select(User.username, User.jury_badge).where(User.jury_badge.is_not(None)))).all())
    if badges != {"senior1": "senior", "senior2": "senior", "regular2": "regular"}:
        problems.append(f"公开展示身份的委员不对：{badges}")
    return problems


async def _phase3_summary(db: Any, statuses: dict[str, dict[str, Any]]) -> None:
    special = json.loads((await db.get(SystemSetting, "jury_demo.cases3")).value)
    switches = await jury_settings.get_switches(db, fresh=True)
    log("三期：打开的开关 " + "、".join(name for name, value in switches.items() if value) + "（生产默认都是关的）")
    log("三期：今天（UTC+8）还没投过票的在任委员 —— dayfirst（看 daily_first）、streak6（已连续 6 天，今天投了就是第 7 天，看 streak_7）。隔天就不成立了，要重建。")
    log("三期：资深委员现在能取到的件数（合议 / 复审）：" + "，".join(f"{name} {(statuses[name]['senior'] or {}).get('panel')} / {(statuses[name]['senior'] or {}).get('appeals')}" for name in ("senior1", "senior2", "senior3")))
    reviews = Counter((kind, stage, status, outcome) for kind, stage, status, outcome in (await db.execute(select(JuryReview.kind, JuryReview.stage, JuryReview.status, JuryReview.outcome))).all())
    log("三期：复审 " + "；".join(f"{kind}/{stage}/{status}/{outcome or '-'} × {count}" for (kind, stage, status, outcome), count in sorted(reviews.items(), key=str)))
    precedents = (await db.execute(select(JuryPrecedent.status, JuryPrecedent.created_by_role, JuryPrecedent.bank_added))).all()
    log(
        f"三期：判例 {sum(1 for status, _role, _added in precedents if status == 'active')} 条有效（资深委员标的 {sum(1 for status, role, _a in precedents if status == 'active' and role == 'senior')}、"
        f"管理员标的 {sum(1 for status, role, _a in precedents if status == 'active' and role == 'admin')}，因此进题库的 {sum(1 for _s, _r, added in precedents if added)} 道），已撤下 {sum(1 for status, _r, _a in precedents if status == 'withdrawn')} 条"
    )
    posts = Counter((kind, status if state == "visible" else state) for kind, status, state in (await db.execute(select(JuryHallPost.kind, JuryHallPost.status, JuryHallPost.state))).all())
    pending = int((await db.execute(select(func.count(JuryHallReport.id)).where(JuryHallReport.status == "pending"))).scalar_one())
    log("三期：议事厅 " + "，".join(f"{kind}/{status} × {count}" for (kind, status), count in sorted(posts.items())) + f"；待处理的举报 {pending} 条")
    log(
        "三期：案件的真实编号 —— "
        + "，".join(f"{label} #{special[key]}" for label, key in (
            ("合议中没人投", "panel_fresh"), ("合议中已有一票", "panel_one_vote"), ("上下文不足进合议", "panel_need_context"), ("超时进合议", "panel_timeout_in"),
            ("合议已定案", "panel_settled"), ("合议超时转管理员", "panel_timeout"), ("凑不齐合议庭", "panel_unavailable"),
            ("机器案件已公示", "machine_published"), ("机器案件未作处理", "machine_unhandled"), ("可申请复议", "reconsider_open"), ("复议中", "reconsider_pending"),
            ("复议已维持", "reconsider_upheld"), ("复议已改判", "reconsider_overturned"), ("复议转管理员", "reconsider_admin"),
        ))
        + f"，机器案件待评审 {'、'.join('#' + str(item) for item in special['machine_waiting'])}"
    )


async def _phase3_problems(db: Any, users: dict[str, int], statuses: dict[str, dict[str, Any]], normal: list[Any], open_counts: dict[str, int], now: datetime) -> list[str]:
    """三期的走查依赖的前提。"""
    problems: list[str] = []
    special = json.loads((await db.get(SystemSetting, "jury_demo.cases3")).value)
    switches = await jury_settings.get_switches(db, fresh=True)
    if not (switches["appeals"] and switches["machine_review"]) or switches["auto_appoint"] or switches["exam_required"]:
        problems.append(f"开关不对：{switches}")
    if open_counts.get("panel") != 4:
        problems.append(f"合议队列里应该有 4 件，实际 {open_counts.get('panel')}")
    for name, wanted in {"senior1": (4, 3), "senior2": (3, 3), "senior3": (4, 2)}.items():
        senior = statuses[name]["senior"] or {}
        if (senior.get("panel"), senior.get("appeals")) != wanted:
            problems.append(f"{name} 能取到的合议 / 复审件数应该是 {wanted}，实际是 {senior}")
    if statuses["juror"]["senior"] is not None:
        problems.append("主账号不是资深委员，/jury/me.senior 应该是 null")
    if [case.id for case in normal[-3:]] != special["machine_waiting"] or any(case.case_origin != "report" for case in normal[:-3]):
        problems.append(f"主账号队列的最后三件应该是机器来源的案件 {special['machine_waiting']}，实际队列 {[case.id for case in normal]}")
    reviews = Counter(
        (await db.execute(select(JuryReview.kind, JuryReview.stage, JuryReview.status, JuryReview.outcome, JuryReview.decided_by, JuryReview.escalation))).all()
    )
    expected = Counter({
        ("appeal", "senior", "open", None, None, None): 2, ("appeal", "admin", "open", None, None, "timeout"): 1,
        ("appeal", "senior", "closed", "uphold", "senior", None): 1, ("appeal", "senior", "closed", "reduce", "senior", None): 1, ("appeal", "senior", "closed", "revoke", "senior", None): 1,
        ("reconsider", "senior", "open", None, None, None): 1, ("reconsider", "admin", "open", None, None, "unavailable"): 1,
        ("reconsider", "senior", "closed", "uphold", "senior", None): 1, ("reconsider", "senior", "closed", "overturn", "senior", None): 1,
    })
    if Counter({tuple(key): count for key, count in reviews.items()}) != expected:
        problems.append(f"复审记录不对：{dict(reviews)}")
    fields = {
        violation.id: (violation.status, violation.appeal_status, violation.appeal_stage, violation.appeal_result, violation.appeal_decided_by, violation.penalty_kind)
        for violation in (await db.execute(select(UserViolation).where(UserViolation.user_id.in_([users["appealing"], users["punished"]])))).scalars().all()
    }
    wanted_fields = {
        "appeal_upheld": ("active", "rejected", None, "upheld", "senior", "warning"), "appeal_reduced": ("active", "accepted", None, "reduced", "senior", "warning"),
        "appeal_revoked": ("revoked", "accepted", None, "revoked", "senior", "mute"), "appeal_open": ("active", "none", None, None, None, "notice"),
        "appeal_timeout": ("active", "pending", "admin", None, None, "notice"), "appeal_direct": ("active", "pending", "admin", None, None, "warning"),
        "appeal_machine": ("active", "pending", "senior", None, None, "mute"), "appeal_jury": ("active", "pending", "senior", None, None, "mute"),
    }
    for key, wanted in wanted_fields.items():
        if fields.get(special[key]) != wanted:
            problems.append(f"处罚 {key}（#{special[key]}）应该是 {wanted}，实际是 {fields.get(special[key])}")
    appealing, punished = await db.get(User, users["appealing"]), await db.get(User, users["punished"])
    if penalty_service.mute_state(appealing, now) is None or penalty_service.mute_state(punished, now) is not None or appealing.status != punished.status:
        problems.append(f"appealing 应该在禁言中、punished 不在：{appealing.muted_until} / {punished.muted_until}")
    precedents = (await db.execute(select(JuryPrecedent))).scalars().all()
    active = [item for item in precedents if item.status == "active"]
    if not 6 <= len(active) <= 8 or {item.created_by_role for item in active} != {"senior", "admin"} or sum(1 for item in active if item.created_by_id == users["senior1"]) < 1 or len(precedents) - len(active) != 1:
        problems.append(f"判例不对：有效 {len(active)} 条，创建人 {[item.created_by_role for item in active]}")
    shown = await case_service.published_case_ids(db, [item.case_id for item in active])
    cases = {case.id: case for case in (await db.execute(select(ReportCase).where(ReportCase.id.in_([item.case_id for item in active])))).scalars().all()}
    if not any(case.id in shown and case.target_type == "post" and case.status == "resolved" for case in cases.values()) or not any(case.target_type == "message" for case in cases.values()):
        problems.append("判例里应该既有全站能看到内容的公开动态，也有群聊消息")
    posts = Counter((await db.execute(select(JuryHallPost.kind, JuryHallPost.status, JuryHallPost.state, JuryHallPost.pinned))).all())
    wanted_posts = Counter({
        ("announcement", "open", "visible", True): 1, ("announcement", "open", "visible", False): 1, ("proposal", "open", "visible", False): 2, ("proposal", "passed", "visible", False): 1,
        ("proposal", "adopted", "visible", False): 1, ("proposal", "declined", "visible", False): 1, ("proposal", "rejected", "visible", False): 1,
        ("discussion", "open", "visible", False): 3, ("discussion", "closed", "visible", False): 1, ("discussion", "open", "hidden", False): 1,
    })
    if Counter({tuple(key): count for key, count in posts.items()}) != wanted_posts:
        problems.append(f"议事厅的帖子不对：{dict(posts)}")
    reports = Counter((await db.execute(select(JuryHallReport.status))).scalars().all())
    if reports != Counter({"pending": 7, "handled": 1, "dismissed": 1}):
        problems.append(f"议事厅的举报不对：{dict(reports)}")
    trainees = {user_id for user_id, tier in (await db.execute(select(JuryMember.user_id, JuryMember.tier))).all() if tier == "trainee"}
    authors = set((await db.execute(select(JuryHallPost.author_id).where(JuryHallPost.kind != "announcement"))).scalars().all())
    commenters = set((await db.execute(select(JuryHallComment.author_id))).scalars().all())
    if authors & trainees or not commenters & trainees:
        problems.append("议事厅里见习委员应该只有评论、没有发帖")
    machine = Counter((await db.execute(select(ReportCase.status, ReportCase.verdict_source).where(ReportCase.case_origin == "machine"))).all())
    if Counter({tuple(key): count for key, count in machine.items()}) != Counter({("open", None): 3, ("resolved", "jury"): 1, ("dismissed", "auto"): 1}):
        problems.append(f"机器来源的案件不对：{dict(machine)}")
    if special["machine_published"] not in await case_service.published_case_ids(db, [special["machine_published"]]):
        problems.append("已定案的机器案件应该在公示栏里")
    flags = {}
    for key, reporter in (("reconsider_open", "reporter1"), ("reconsider_pending", "reporter2"), ("reconsider_upheld", "reporter3"), ("reconsider_overturned", "reporter4"), ("reconsider_admin", "reporter1")):
        report = (await db.execute(select(UserReport).where(UserReport.case_id == special[key], UserReport.reporter_id == users[reporter]))).scalars().first()
        value = await review_service.report_fields(db, report, users[reporter])
        flags[key] = (value["can_reconsider"], value["reconsider_status"])
    if flags != {"reconsider_open": (True, "none"), "reconsider_pending": (False, "pending"), "reconsider_upheld": (False, "upheld"), "reconsider_overturned": (False, "overturned"), "reconsider_admin": (False, "pending")}:
        problems.append(f"举报人看到的复议状态不对：{flags}")
    # 今天（UTC+8）还没投过票、也没有当天 daily_first 流水的在任委员：dayfirst 用来看 daily_first，streak6 还要正好连续 6 天
    today = points_service.local_day(now)
    for name, streak in (("dayfirst", 0), ("streak6", 6)):
        voted = [penalty_service.aware(value) for value in (await db.execute(select(JuryVote.voted_at).where(JuryVote.juror_id == users[name], JuryVote.voted_at.is_not(None)))).scalars().all()]
        first = int((await db.execute(select(func.count(JuryPointLog.id)).where(JuryPointLog.user_id == users[name], JuryPointLog.kind == "daily_first", JuryPointLog.ref_id == today.toordinal()))).scalar_one())
        member = await jury_service.get_member(db, users[name])
        status = statuses[name]
        if any(points_service.local_day(value) == today for value in voted) or first or status["status"] != "active" or status["restriction"] is not None or not status["available_cases"]:
            problems.append(f"{name} 应该是今天还没投过票、没有当天 daily_first 流水、有案件可评的在任委员：今天的票 {sum(1 for value in voted if points_service.local_day(value) == today)} 张，daily_first {first} 条，可评 {status['available_cases']} 件")
        if points_service.current_streak(member, today) != streak or (streak and member.streak_last_day != today - timedelta(days=1)):
            problems.append(f"{name} 的连续参与天数应该是 {streak}（最后一天是昨天），实际是 {points_service.current_streak(member, today)}，最后一天 {member.streak_last_day}")
    kinds = set((await db.execute(select(JuryPointLog.kind).distinct())).scalars().all())
    if not {"panel_agreed", "review_vote"} <= kinds:
        problems.append(f"风纪值流水里应该有合议和复审的加分：{sorted(kinds)}")
    return problems


# ====================================================================== 入口


def main() -> None:
    parser = argparse.ArgumentParser(description="风纪系统 v2 的本地联调实例（数据在系统临时目录，不碰任何真实环境）")
    parser.add_argument("--reset", action="store_true", help="删掉演示库和上传目录，重新灌数据")
    parser.add_argument("--seed-only", action="store_true", help="只建库灌数据，不起服务")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"监听地址，默认 {DEFAULT_HOST}（只有本机能访问）")
    parser.add_argument(
        "--db-dir", default=None, metavar="DIR", help="演示数据放在哪个目录，默认是系统临时目录下的 kukechat-jury-demo；写相对路径时相对系统临时目录，必须在临时目录里"
    )
    parser.add_argument("--cors-origin", action="append", default=[], metavar="ORIGIN", help="额外放行的前端来源，可重复；localhost / 127.0.0.1 的任意端口默认已放行")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    configure_environment(args.cors_origin, args.db_dir)
    if not args.seed_only and port_in_use(args.host, args.port):
        die(f"端口 {args.port} 已经被占用（是不是演示服务已经在跑了？）。换一个 --port，或者先停掉它。")
    if args.reset:
        reset_demo_files()
    _bind_app_modules()
    verify_settings()
    # 红包要有余额才能发：演示实例用假账本（只在这个进程里）
    install_demo_ledger()
    asyncio.run(prepare_database())
    print_accounts(args.host, args.port)
    if args.seed_only:
        return
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        log(f"注意：监听在 {args.host}，局域网里的其他设备也能访问这个演示库（账号密码是公开的）。")

    import uvicorn

    from app.main import app as application

    uvicorn.run(application, host=args.host, port=args.port, workers=1, log_level="info")


if __name__ == "__main__":
    main()
