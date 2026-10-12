"""风纪系统 v2 三期：判例和议事厅的表（docs/jury-v2.md 3.4、3.5）。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class JuryPrecedent(Base):
    """判例：一个已定案的案件加上标题和裁判要旨。一个案件最多一条（撤下之后再标会复用这一行）。"""

    __tablename__ = "jury_precedents"
    __table_args__ = (Index("ix_jury_precedents_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # 案件的真实编号（也就是公开编号）
    case_id: Mapped[int] = mapped_column(ForeignKey("report_cases.id", ondelete="CASCADE"), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # senior（资深委员标的）/ admin（管理员在后台标的）
    created_by_role: Mapped[str] = mapped_column(String(12), default="senior", server_default="senior")
    # active / withdrawn（作者或管理员撤下，或案件被改判）
    status: Mapped[str] = mapped_column(String(12), default="active", server_default="active")
    # 这个案件是不是因为成了判例才进的题库；撤下时只移出这种
    bank_added: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 为空表示系统撤下的（案件被改判）
    withdrawn_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    withdraw_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class JuryHallPost(Base):
    """议事厅的帖子。实名，全站可读。"""

    __tablename__ = "jury_hall_posts"
    __table_args__ = (
        Index("ix_jury_hall_posts_state_activity", "state", "pinned", "last_activity_at"),
        Index("ix_jury_hall_posts_author_created", "author_id", "created_at"),
        Index("ix_jury_hall_posts_kind_status_ends", "kind", "status", "vote_ends_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # announcement（管理员公告）/ proposal（规则提案）/ discussion（讨论）
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # open / passed（提案通过，待管理员采纳）/ rejected / adopted / declined / closed（管理员关闭）
    status: Mapped[str] = mapped_column(String(12), default="open", server_default="open")
    # visible / deleted（本人或管理员删除）/ hidden（多人举报后折叠，等管理员处理）
    state: Mapped[str] = mapped_column(String(12), default="visible", server_default="visible")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 以下四项只有提案有：表决截止时间，发帖时固定下来的最少表决人数和通过线（yes × den >= (yes + no) × num）
    vote_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quorum: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pass_num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pass_den: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 提案出结果（passed / rejected）的时间；出结果之后不能再表决，票数不会再变
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 管理员采纳 / 不采纳 / 关闭时写的说明，对所有人展示
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    # 最后一次编辑 / 状态变化
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    # 发帖或最后一条评论的时间，sort=active 按它排
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 管理员删除时写的说明，只在后台展示
    moderation_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class JuryHallVote(Base):
    """提案的一张表决票。结束前可以改票，所以一人一行。"""

    __tablename__ = "jury_hall_votes"
    __table_args__ = (UniqueConstraint("post_id", "user_id", name="uq_jury_hall_votes_post_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("jury_hall_posts.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # yes / no
    vote: Mapped[str] = mapped_column(String(4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class JuryHallComment(Base):
    """帖子下的评论或回复。"""

    __tablename__ = "jury_hall_comments"
    __table_args__ = (
        Index("ix_jury_hall_comments_post_created", "post_id", "created_at"),
        Index("ix_jury_hall_comments_author_created", "author_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("jury_hall_posts.id", ondelete="CASCADE"))
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # 回复只有一层：parent_id 恒指向根评论，reply_to_id 是实际被回复的那条（回复别人的回复时才有）
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reply_to_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    # visible / deleted（本人或管理员删除）/ hidden（多人举报后折叠，等管理员处理）
    state: Mapped[str] = mapped_column(String(12), default="visible", server_default="visible")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    moderation_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class JuryHallReport(Base):
    """对帖子或评论的举报，两者共用这张表。"""

    __tablename__ = "jury_hall_reports"
    __table_args__ = (
        UniqueConstraint("target_type", "target_id", "reporter_id", name="uq_jury_hall_reports_target_reporter"),
        Index("ix_jury_hall_reports_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # post / comment
    target_type: Mapped[str] = mapped_column(String(12))
    target_id: Mapped[int] = mapped_column(Integer)
    # 目标所在的帖子（举报帖子时就是它自己），方便后台按帖子看
    post_id: Mapped[int] = mapped_column(ForeignKey("jury_hall_posts.id", ondelete="CASCADE"), index=True)
    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(String(40))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    # pending / handled（目标被删除）/ dismissed（举报不成立）
    status: Mapped[str] = mapped_column(String(12), default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    handled_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    handle_note: Mapped[str | None] = mapped_column(Text, nullable=True)
