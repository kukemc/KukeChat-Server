"""CCW 网关：KukeChat 需要从 CCW 社区取的三样数据。这里只有接口，没有实现。

业务层（app/services/ccw.py）只认这个接口：绑定口令怎么生成和匹配、资料怎么同步、
作品卡片怎么拼，都在那边；「怎么向 CCW 要到这些数据」不在本仓库里。实现由部署方提供，
见 README 的「集成点」一节。没有实现时用 NullCcwGateway：available() 为 False，取数一律抛 CcwUnavailable。

三个取数方法返回的都是 CCW 给的 JSON 对象（已解析成 dict），业务层按需读其中的字段：

- 评论：`id`、`content`、`commenter`（其中的 `accountOid` / `name` / `avatar`）。
- 学生资料：两份对象，一份是资料（昵称、头像、简介、主页封面……），一份是主页统计（粉丝数、作品数……），
  各字段见 services/ccw.py 的 normalize_student_profile。
- 作品详情：标题、封面、作者、各项计数，各字段见 services/ccw.py 的 build_preview。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, NamedTuple


class CcwUnavailable(Exception):
    """现在没法从 CCW 取到数据：没有接入、网络不通、对方返回了错误，或者返回的内容看不懂。

    upstream_status：对方明确返回了一个 HTTP 错误码时是那个码；压根没连上、超时、内容解析不了时是 None。
    """

    def __init__(self, message: str = "", *, upstream_status: int | None = None) -> None:
        super().__init__(message)
        self.upstream_status = upstream_status


class StudentPayloads(NamedTuple):
    """一个学生的两份数据；哪份没取到就是 None。"""

    # 资料：昵称、头像、简介、主页封面等
    profile: dict[str, Any] | None
    # 主页统计：关注、粉丝、获赞、收藏、作品数、浏览量等
    detail: dict[str, Any] | None


class CcwGateway(ABC):
    """从 CCW 取数的入口。实现要自己负责不阻塞事件循环（同步的网络请求放进线程池）。

    取不到一律抛 CcwUnavailable，不要把底层的网络异常漏出来，也不要用空结果冒充「没有」。
    """

    def available(self) -> bool:
        """有没有接入 CCW。为 False 时业务层把绑定显示为未开放，资料同步和作品卡片直接跳过。"""
        return True

    @abstractmethod
    async def latest_comments(self, subject_oid: str, subject_type: str) -> list[dict[str, Any]]:
        """某个评论区（subject_type 类型的 subject_oid）最新的一页评论，新的在前。"""

    @abstractmethod
    async def student_payloads(self, student_oid: str) -> StudentPayloads:
        """某个学生的资料和主页统计。"""

    @abstractmethod
    async def creation_detail(self, oid: str, access_key: str = "") -> dict[str, Any]:
        """某个作品的详情。access_key：未公开作品的访问口令，没有就传空串。"""


class NullCcwGateway(CcwGateway):
    """没有接入 CCW 时的占位实现。"""

    def available(self) -> bool:
        return False

    async def latest_comments(self, subject_oid: str, subject_type: str) -> list[dict[str, Any]]:
        raise CcwUnavailable("本服务没有接入 CCW")

    async def student_payloads(self, student_oid: str) -> StudentPayloads:
        raise CcwUnavailable("本服务没有接入 CCW")

    async def creation_detail(self, oid: str, access_key: str = "") -> dict[str, Any]:
        raise CcwUnavailable("本服务没有接入 CCW")
