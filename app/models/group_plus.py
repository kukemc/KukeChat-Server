"""群升级、群文件、群待办的表（docs/group-plus.md），以及精华 / 待办共用的消息快照列，
还有聊天记录搜索索引的建表（见文件末尾的 _after_create）。

这里的 id 列（message_id、uploader_id、user_id）都不建外键：消息会被修剪、用户可能被删，
流水和快照是账，不能跟着被级联删掉。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


class MessageSnapshotMixin:
    """把一条消息固化下来（精华、群待办共用）。撤回之后内容被清掉，只留 snap_recalled_at。"""

    # MessageType 的名字（TEXT / IMAGE …），和 messages.type 一致
    snap_type: Mapped[str] = mapped_column(String(14), default="TEXT", server_default="TEXT")
    snap_content: Mapped[str] = mapped_column(Text, default="", server_default="")
    snap_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    snap_sender_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snap_sender_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    snap_sender_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    snap_sender_avatar: Mapped[str | None] = mapped_column(String(500), nullable=True)
    snap_sender_title: Mapped[str | None] = mapped_column(String(40), nullable=True)
    snap_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snap_recalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 回填时原消息已经不在了，只留空壳
    snapshot_missing: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class GroupPlusRecord(Base):
    """一次群升级的购买 / 后台赠送 / 后台取消。amount 是花掉的币；创建后不变，不退款。"""

    __tablename__ = "group_plus_records"
    __table_args__ = (
        Index("ix_group_plus_records_conversation", "conversation_id", "id"),
        Index("ix_group_plus_records_user", "user_id", "id"),
        Index("ix_group_plus_records_ccw_oid", "ccw_oid"),
        Index("ix_group_plus_records_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer)
    # purchase / grant / revoke
    source: Mapped[str] = mapped_column(String(12), default="purchase", server_default="purchase")
    # new / renew / upgrade / grant / revoke
    action: Mapped[str] = mapped_column(String(12))
    # 购买人；后台赠送为空
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    admin_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tier: Mapped[str] = mapped_column(String(12))
    period: Mapped[str | None] = mapped_column(String(8), nullable=True)
    days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    amount: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    prev_tier: Mapped[str] = mapped_column(String(12), default="free", server_default="free")
    prev_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    new_tier: Mapped[str] = mapped_column(String(12), default="free", server_default="free")
    new_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    forfeited_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 购买时绑定的 CCW 账号：同一个账号的币换绑到别的 KukeChat 账号上不能再花一遍
    ccw_oid: Mapped[str | None] = mapped_column(String(24), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GroupFile(Base):
    __tablename__ = "group_files"
    __table_args__ = (
        Index("ix_group_files_conversation", "conversation_id", "id"),
        Index("ix_group_files_expires", "expires_at"),
        Index("ix_group_files_folder", "conversation_id", "folder_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer)
    uploader_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(160))
    size: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    storage_key: Mapped[str] = mapped_column(String(300))
    # 空 = 永久
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 临时文件：空间放不下时传的，固定 24 小时后过期，不占空间配额（docs/group-plus.md 第 5 节）
    temporary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 所在的文件夹（group_file_folders.id）；空 = 根目录。不建外键：删文件夹时由服务层把文件移回根目录
    folder_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GroupFileFolder(Base):
    """群文件的文件夹（docs/group-plus.md 5.5）：只有一层。name_key 是名字的 NFKC + casefold，
    (conversation_id, name_key) 唯一，保证同一个群里不区分大小写不重名（两个 worker 并发建同名也只成一个）。"""

    __tablename__ = "group_file_folders"
    __table_args__ = (UniqueConstraint("conversation_id", "name_key", name="uq_group_file_folder_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(40))
    name_key: Mapped[str] = mapped_column(String(160))
    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GroupFileUpload(Base):
    """直传的上传单（docs/group-plus.md 第 5 节）：发了预签名 PUT、还没 complete 的上传。complete 成功后删掉；
    过期的由群文件 worker 连同桶里可能已经传上去的对象一起删。"""

    __tablename__ = "group_file_uploads"
    __table_args__ = (Index("ix_group_file_uploads_expires", "expires_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer)
    uploader_id: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(300))
    name: Mapped[str] = mapped_column(String(160))
    size: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    # 请求时带的 permanent（只对群管有效）；temporary 是开单时定的，complete 时按当时的剩余空间再定一遍
    permanent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    temporary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 开单时指定的文件夹；complete 时文件夹没了 / 变成临时文件 / 档位不够了就放根目录
    folder_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GroupTodo(MessageSnapshotMixin, Base):
    """群待办：每个群最多一条（conversation_id 唯一）。"""

    __tablename__ = "group_todos"
    __table_args__ = (UniqueConstraint("conversation_id", name="uq_group_todo_conversation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    set_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    set_by_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    set_by_avatar: Mapped[str | None] = mapped_column(String(500), nullable=True)
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


# ---------------------------------------------------------------- 建表后的补充（SQLite / PostgreSQL 各一套）


@event.listens_for(Base.metadata, "after_create")
def _after_create(target, connection, **_kw) -> None:  # noqa: ANN001
    """create_all 之后：把精华表升级成快照版、建聊天记录搜索索引和触发器。幂等。

    放在 metadata 的 after_create 里：init_db 和只跑 create_all 的测试都会走到。
    """
    from app.services.schema_extras import install_schema_extras

    install_schema_extras(connection)
