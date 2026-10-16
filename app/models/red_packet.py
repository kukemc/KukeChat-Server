"""红包的表（docs/red-packets.md）：红包本身，和创建时就预先分好的每一份。

钱只记在这两张表里，没有另外的余额列：一个人发出了多少 = 他名下红包的 total 之和，退回了多少 = refunded_amount 之和，
抢到了多少 = 记在他名下的那些份的 amount 之和。余额公式（services/shop.py 的 _balance）直接对这两张表求和。

这里的 id 列都不建外键：消息会被清理（每个会话只留最近 1000 条）、群会被解散，
红包和领取记录是账，不能跟着被级联删掉。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class RedPacket(Base):
    """一个红包。发出时整包金额立刻从发送者的余额里扣掉（total 计入「发出」）。"""

    __tablename__ = "red_packets"
    __table_args__ = (
        Index("ix_red_packets_sender_created", "sender_id", "created_at"),
        Index("ix_red_packets_status_expires", "status", "expires_at"),
        Index("ix_red_packets_conversation", "conversation_id"),
        Index("ix_red_packets_ccw_oid", "ccw_oid"),
        Index("ix_red_packets_message", "message_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sender_id: Mapped[int] = mapped_column(Integer)
    conversation_id: Mapped[int] = mapped_column(Integer)
    # direct / group。发的时候记下来：会话之后被删掉了，后台也还知道这是不是私聊红包（私聊红包的祝福语不给后台看）
    conversation_type: Mapped[str] = mapped_column(String(12))
    # 会话里那条 type=red_packet 的消息
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # normal 每份一样多 / lucky 拼手气
    kind: Mapped[str] = mapped_column(String(12))
    greeting: Mapped[str] = mapped_column(String(60))
    # 整包金额、份数。创建后不变
    total: Mapped[int] = mapped_column(Integer)
    count: Mapped[int] = mapped_column(Integer)
    # 已经被领走几份、多少币。和 red_packet_shares 在同一个事务里一起改，对账时会核对
    claimed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    claimed_amount: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 结束时没被领走、退回给发送者的部分。active 和 finished 的红包恒为 0
    refunded_amount: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # active 进行中 / finished 抢完了 / expired 到期，未领的已退回 / revoked 提前结束（消息被撤回、后台撤销），未领的已退回
    status: Mapped[str] = mapped_column(String(12), default="active", server_default="active")
    # 发的时候绑定的 CCW 账号。同一个 CCW 账号的币换绑到别的 KukeChat 账号上不能再发一遍，靠它算（和 shop_purchases.ccw_oid 一个用途）
    ccw_oid: Mapped[str | None] = mapped_column(String(24), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    # 抢完、到期或被撤销的时间
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 后台撤销时是谁、为什么；消息被撤回导致的撤销这两项为空
    revoked_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class RedPacketShare(Base):
    """红包里的一份。创建红包时就把每一份的金额分好存下来，领取就是把其中一份记到某个人名下。

    user_id 为空表示还没被领。(packet_id, user_id) 唯一：一个人在一个红包里最多占一份
    （没被领的行 user_id 都是 NULL，唯一索引不管 NULL）。
    """

    __tablename__ = "red_packet_shares"
    __table_args__ = (
        UniqueConstraint("packet_id", "seq", name="uq_red_packet_shares_packet_seq"),
        UniqueConstraint("packet_id", "user_id", name="uq_red_packet_shares_packet_user"),
        Index("ix_red_packet_shares_user_claimed", "user_id", "claimed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    packet_id: Mapped[int] = mapped_column(Integer)
    # 领取顺序：先来的人拿 seq 小的那一份
    seq: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
