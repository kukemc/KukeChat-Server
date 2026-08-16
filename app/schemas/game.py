from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.schemas.message import MessageRead


def _normalize_oid(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


class GameModeUpdate(BaseModel):
    """群主开启 / 关闭游戏模式并绑定 CCW 作品。"""

    enabled: bool
    creation_oid: str | None = Field(default=None, max_length=64)

    @field_validator("creation_oid")
    @classmethod
    def validate_creation_oid(cls, value: str | None) -> str | None:
        oid = _normalize_oid(value)
        if oid is None:
            return None
        if not oid.isalnum():
            raise ValueError("作品 oid 只能包含字母和数字")
        return oid.lower()


class GameModeRead(BaseModel):
    conversation_id: int
    enabled: bool
    creation_oid: str | None
    title: str | None
    member_count: int


class GameParticipant(BaseModel):
    """游戏模式下暴露的最小用户信息。"""

    id: int
    display_name: str
    avatar_url: str | None = None


class GameSessionRead(BaseModel):
    """作品内接入聊天所需的一切信息。

    未登录的游玩者同样可以拿到这份数据（只读）。
    """

    conversation_id: int
    creation_oid: str
    title: str | None
    member_count: int
    # 当前请求者是否已登录且可发言
    can_send: bool
    # 已登录但还不是群成员时为 True：发第一条消息会自动入群
    will_auto_join: bool
    viewer: GameParticipant | None = None
    messages: list[MessageRead] = Field(default_factory=list)


class GameMessageCreate(BaseModel):
    conversation_id: int
    creation_oid: str = Field(max_length=64)
    content: str = Field(min_length=1, max_length=500)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("消息内容不能为空")
        return text
