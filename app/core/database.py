from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite")


def is_postgres_url(url: str) -> bool:
    return url.startswith("postgresql")


def engine_options(url: str) -> dict:
    """按数据库类型给 create_async_engine 的参数。

    SQLite：连接池放宽到每个 worker 10 + 20 —— WAL 下读不互斥，多几条连接能让读请求不必排在慢写后面。
    PostgreSQL：每个 worker ``DATABASE_POOL_SIZE`` + ``DATABASE_MAX_OVERFLOW`` 条（默认 10 + 10，两个 worker 共 40 条，
    低于 PG 默认的 max_connections=100）；``pool_pre_ping`` 让数据库重启 / 空闲断开后的第一条请求不报错。
    超时（idle_in_transaction_session_timeout / statement_timeout / lock_timeout）设在数据库角色上，见 docs/postgres-cutover.md。
    """
    if is_sqlite_url(url):
        if ":memory:" in url:
            return {"connect_args": {"check_same_thread": False}}
        return {"connect_args": {"check_same_thread": False}, "pool_size": 10, "max_overflow": 20}
    if is_postgres_url(url):
        return {
            "pool_size": settings.database_pool_size,
            "max_overflow": settings.database_max_overflow,
            "pool_timeout": 30,
            "pool_pre_ping": True,
            "pool_recycle": 1800,
            "connect_args": {"server_settings": {"application_name": "kukechat-api"}},
        }
    return {}


engine = create_async_engine(settings.database_url, echo=False, future=True, **engine_options(settings.database_url))
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

from app.core import db_watch  # noqa: E402

db_watch.install(engine.sync_engine)


def _is_sqlite_dbapi(dbapi_connection) -> bool:  # noqa: ANN001
    return "sqlite" in type(dbapi_connection).__module__.lower()


@event.listens_for(engine.sync_engine, "connect")
def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    """SQLite 的默认设置扛不住多进程聊天服务，每条连接都得调一遍（不是 SQLite 的连接直接跳过）。

    默认的 rollback journal（``journal_mode=delete``）每次提交都要对整库加排他
    锁、建日志文件、fsync 两次再删掉，读写还互相阻塞 —— 多 worker 下发消息会
    肉眼可见地卡。WAL 让读不挡写、写不挡读，提交也只是往 -wal 追加。

    ``synchronous=NORMAL`` 在 WAL 下依旧不会损坏数据库，最坏情况是断电时丢掉
    最后几个事务；换来的是省掉每次提交的那次 fsync。

    ``journal_mode`` 是写在库文件头里的，设一次就一直生效，之后每次连接执行它
    只是一次廉价的读取；``synchronous`` 和 ``busy_timeout`` 是连接级的，必须
    每条连接都设。
    """
    if not _is_sqlite_dbapi(dbapi_connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # 抢锁失败时最多等 10 秒再报 database is locked，别让偶发争用直接 500
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA temp_store=MEMORY")
    finally:
        cursor.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def has_uncommitted_write(db: AsyncSession) -> bool:
    """会话里有没有还没提交的写：内存里待 flush 的改动，或者已经 flush / 直接执行过、但还没提交的写语句。

    后一种光看 ``db.new / dirty / deleted`` 看不出来（flush 之后它们就清空了），要问数据库：

    - SQLite：sqlite3 只在 INSERT / UPDATE / DELETE 之前才隐式 BEGIN，所以连接上 ``in_transaction`` 为真就说明这条
      连接已经拿了（或正在抢）SQLite 的写锁。
    - PostgreSQL：驱动在第一条语句（读也算）之前就 BEGIN，看驱动看不出来；改问事务有没有分到事务号 ——
      只有写过（或 SELECT ... FOR UPDATE 锁过行）的事务才会分到。

    看不出来的驱动按「有」算，宁可不提交。
    """
    if db.new or db.dirty or db.deleted:
        return True
    if not db.in_transaction():
        return False
    connection = await db.connection()
    if connection.dialect.name == "postgresql":
        return bool(await connection.scalar(text("SELECT pg_current_xact_id_if_assigned() IS NOT NULL")))
    raw = await connection.get_raw_connection()
    flag = getattr(raw.driver_connection, "in_transaction", None)
    return True if flag is None else bool(flag)


async def release_connection(db: AsyncSession) -> bool:
    """去等网络 / 慢 IO（CCW、审核服务、对象存储、图片处理、sleep）之前，把这个会话借着的数据库连接还回池子。

    会话第一次查询就借走一条连接，一直占到提交 / 回滚 / 关闭。带着它去等几秒到几十秒的外部服务，并发一上来就会
    把连接池占满（线上已经出过几次）；要是这时还有已经 flush 的写，就更糟 —— SQLite 的写锁一直拿着，全站写操作
    排队直到 ``database is locked``。

    只在没有未提交的写时提交：没有写的提交只是结束读事务，不会把调用方的半成品写进去；会话用的是
    ``expire_on_commit=False``，对象不过期，之后接着用。有未提交的写时不提交（那是调用方的顺序问题，该把网络调用
    挪到写之前），只打一条日志，返回 False。
    """
    if not db.in_transaction() and not (db.new or db.dirty or db.deleted):
        return True
    if await has_uncommitted_write(db):
        logger.warning("release_connection: session has uncommitted writes, keeping the connection", stack_info=True)
        return False
    await db.commit()
    return True


# ------------------------------------------------------------------ 建表与增量迁移
#
# 全新的库由 create_all 直接按模型建好；下面这些只对「表比模型老」的库生效（线上 SQLite 是一路加列过来的），
# 全部可以重复执行。PostgreSQL 和 SQLite 走同一份清单：列定义按 SQLite 的写法记，PG 由 _column_ddl 翻译。
#
# 以后加列：在 _ADDED_COLUMNS 末尾追加一行；不能为空的列必须带 DEFAULT（老行要有值）。
# 加索引：在 _ADDED_INDEXES 末尾追加，一律写 IF NOT EXISTS。

_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("users", "registration_ip", "registration_ip VARCHAR(45)"),
    ("users", "ip_login_expires_at", "ip_login_expires_at DATETIME"),
    ("users", "platform_role", "platform_role VARCHAR(5) NOT NULL DEFAULT 'USER'"),
    ("users", "status", "status VARCHAR(8) NOT NULL DEFAULT 'ACTIVE'"),
    ("users", "banned_until", "banned_until DATETIME"),
    ("users", "banned_at", "banned_at DATETIME"),
    ("users", "ban_reason", "ban_reason TEXT"),
    ("users", "muted_until", "muted_until DATETIME"),
    ("users", "mute_reason", "mute_reason TEXT"),
    ("users", "share_typing", "share_typing BOOLEAN NOT NULL DEFAULT 1"),
    ("users", "poke_action", "poke_action VARCHAR(4) NOT NULL DEFAULT '戳'"),
    ("users", "poke_suffix", "poke_suffix VARCHAR(32) NOT NULL DEFAULT ''"),
    ("users", "last_login_ip", "last_login_ip VARCHAR(45)"),
    ("users", "last_login_at", "last_login_at DATETIME"),
    ("users", "last_seen_ip", "last_seen_ip VARCHAR(45)"),
    ("users", "last_seen_at", "last_seen_at DATETIME"),
    ("users", "presence_status", "presence_status VARCHAR(8) NOT NULL DEFAULT 'online'"),
    ("users", "presence_text", "presence_text VARCHAR(40)"),
    ("users", "presence_updated_at", "presence_updated_at DATETIME"),
    ("users", "profile_title", "profile_title VARCHAR(80)"),
    ("users", "profile_tagline", "profile_tagline VARCHAR(120)"),
    ("users", "profile_status", "profile_status VARCHAR(80)"),
    ("users", "profile_location", "profile_location VARCHAR(80)"),
    ("users", "profile_interests", "profile_interests VARCHAR(240)"),
    ("users", "profile_layout", "profile_layout VARCHAR(20)"),
    ("users", "profile_card_style", "profile_card_style VARCHAR(20)"),
    ("users", "profile_accent_color", "profile_accent_color VARCHAR(20)"),
    ("users", "profile_cover_url", "profile_cover_url VARCHAR(500)"),
    ("users", "profile_cover_pending_url", "profile_cover_pending_url VARCHAR(500)"),
    ("users", "profile_cover_moderation_status", "profile_cover_moderation_status VARCHAR(20) NOT NULL DEFAULT 'approved'"),
    ("users", "ccw_student_oid", "ccw_student_oid VARCHAR(24)"),
    ("users", "ccw_name", "ccw_name VARCHAR(120)"),
    ("users", "ccw_avatar_url", "ccw_avatar_url VARCHAR(500)"),
    ("users", "ccw_bio", "ccw_bio TEXT"),
    ("users", "ccw_homepage_cover_url", "ccw_homepage_cover_url VARCHAR(500)"),
    ("users", "ccw_following_count", "ccw_following_count INTEGER"),
    ("users", "ccw_follower_count", "ccw_follower_count INTEGER"),
    ("users", "ccw_like_count", "ccw_like_count INTEGER"),
    ("users", "ccw_favorite_count", "ccw_favorite_count INTEGER"),
    ("users", "ccw_comment_count", "ccw_comment_count INTEGER"),
    ("users", "ccw_creation_count", "ccw_creation_count INTEGER"),
    ("users", "ccw_view_count", "ccw_view_count INTEGER"),
    ("users", "ccw_synced_at", "ccw_synced_at DATETIME"),
    ("users", "content_moderation_whitelisted", "content_moderation_whitelisted BOOLEAN NOT NULL DEFAULT 0"),
    ("users", "is_bot", "is_bot BOOLEAN NOT NULL DEFAULT 0"),
    ("users", "bot_owner_id", "bot_owner_id INTEGER"),
    ("conversations", "announcement", "announcement TEXT"),
    ("conversations", "description", "description VARCHAR(500)"),
    ("conversations", "category", "category VARCHAR(40)"),
    ("conversations", "all_muted", "all_muted BOOLEAN NOT NULL DEFAULT 0"),
    ("conversations", "slow_mode_seconds", "slow_mode_seconds INTEGER NOT NULL DEFAULT 0"),
    ("conversations", "message_rate_limit_per_minute", "message_rate_limit_per_minute INTEGER NOT NULL DEFAULT 0"),
    ("conversations", "join_mode", "join_mode VARCHAR(20) NOT NULL DEFAULT 'approval'"),
    ("conversations", "auto_approve", "auto_approve BOOLEAN NOT NULL DEFAULT 0"),
    ("conversations", "join_question", "join_question VARCHAR(200)"),
    ("conversations", "tasks_enabled", "tasks_enabled BOOLEAN NOT NULL DEFAULT 0"),
    ("red_packets", "voice_text", "voice_text VARCHAR(60)"),
    ("conversations", "typing_indicator", "typing_indicator VARCHAR(8) NOT NULL DEFAULT 'auto'"),
    ("favorite_stickers", "position", "position INTEGER"),
    ("conversations", "level_titles_enabled", "level_titles_enabled BOOLEAN NOT NULL DEFAULT 0"),
    ("conversations", "level_titles", "level_titles TEXT"),
    ("conversations", "task_creation_permission", "task_creation_permission VARCHAR(20) NOT NULL DEFAULT 'members'"),
    ("conversations", "is_temporary", "is_temporary BOOLEAN NOT NULL DEFAULT 0"),
    ("conversations", "temporary_initiator_id", "temporary_initiator_id INTEGER"),
    ("conversations", "temporary_target_id", "temporary_target_id INTEGER"),
    ("conversation_members", "nickname", "nickname VARCHAR(120)"),
    ("conversation_members", "remark", "remark VARCHAR(500)"),
    ("conversation_members", "muted", "muted BOOLEAN NOT NULL DEFAULT 0"),
    ("conversation_members", "muted_until", "muted_until DATETIME"),
    ("conversation_members", "pinned", "pinned BOOLEAN NOT NULL DEFAULT 0"),
    ("conversation_members", "do_not_disturb", "do_not_disturb BOOLEAN NOT NULL DEFAULT 0"),
    ("conversation_members", "message_setting", "message_setting VARCHAR(20) NOT NULL DEFAULT 'notify'"),
    ("conversation_members", "cleared_before_message_id", "cleared_before_message_id INTEGER"),
    ("conversation_members", "title", "title VARCHAR(40)"),
    ("conversation_members", "level", "level INTEGER NOT NULL DEFAULT 1"),
    ("conversation_members", "level_exp", "level_exp INTEGER NOT NULL DEFAULT 0"),
    ("conversation_members", "activity_score", "activity_score INTEGER NOT NULL DEFAULT 0"),
    ("conversation_members", "total_checkins", "total_checkins INTEGER NOT NULL DEFAULT 0"),
    ("conversation_members", "current_checkin_streak", "current_checkin_streak INTEGER NOT NULL DEFAULT 0"),
    ("conversation_members", "best_checkin_streak", "best_checkin_streak INTEGER NOT NULL DEFAULT 0"),
    ("conversation_members", "last_checkin_date", "last_checkin_date DATE"),
    ("conversation_members", "last_active_at", "last_active_at DATETIME"),
    ("messages", "recalled_at", "recalled_at DATETIME"),
    ("messages", "recalled_by_id", "recalled_by_id INTEGER"),
    ("messages", "metadata", "metadata TEXT"),
    ("user_reports", "conversation_id", "conversation_id INTEGER"),
    ("user_reports", "message_id", "message_id INTEGER"),
    ("user_reports", "reported_user_id", "reported_user_id INTEGER"),
    ("user_reports", "attachments", "attachments TEXT"),
    ("user_reports", "target_snapshot", "target_snapshot TEXT"),
    ("user_reports", "evidence_hash", "evidence_hash VARCHAR(64)"),
    ("user_reports", "case_id", "case_id INTEGER"),
    ("content_moderation_records", "human_verdict", "human_verdict VARCHAR(12)"),
    ("content_moderation_records", "human_category", "human_category VARCHAR(40)"),
    ("content_moderation_records", "human_level", "human_level INTEGER"),
    ("content_moderation_records", "human_by_id", "human_by_id INTEGER"),
    ("content_moderation_records", "human_at", "human_at DATETIME"),
    ("user_reports", "malicious", "malicious BOOLEAN NOT NULL DEFAULT 0"),
    ("password_reset_requests", "requested_password", "requested_password VARCHAR(128) NOT NULL DEFAULT ''"),
    ("posts", "moderation_status", "moderation_status VARCHAR(20) NOT NULL DEFAULT 'approved'"),
    ("posts", "repost_of_id", "repost_of_id INTEGER"),
    ("posts", "pinned_at", "pinned_at DATETIME"),
    ("posts", "ccw_creations", "ccw_creations TEXT"),
    ("conversations", "game_mode_enabled", "game_mode_enabled BOOLEAN NOT NULL DEFAULT 0"),
    ("conversations", "game_creation_oid", "game_creation_oid VARCHAR(64)"),
    ("post_topics", "moderation_status", "moderation_status VARCHAR(20) NOT NULL DEFAULT 'approved'"),
    ("post_topics", "created_by_id", "created_by_id INTEGER"),
    ("post_comments", "moderation_status", "moderation_status VARCHAR(20) NOT NULL DEFAULT 'approved'"),
    ("bot_reviews", "moderation_status", "moderation_status VARCHAR(20) NOT NULL DEFAULT 'approved'"),
    ("bot_reviews", "deleted_at", "deleted_at DATETIME"),
    ("bot_review_replies", "moderation_status", "moderation_status VARCHAR(20) NOT NULL DEFAULT 'approved'"),
    ("bot_review_replies", "deleted_at", "deleted_at DATETIME"),
    ("jury_members", "tier", "tier VARCHAR(12) NOT NULL DEFAULT 'trainee'"),
    ("jury_votes", "alias", "alias VARCHAR(12)"),
    ("jury_votes", "is_quality_check", "is_quality_check BOOLEAN NOT NULL DEFAULT 0"),
    ("report_cases", "queued_at", "queued_at DATETIME"),
    ("report_cases", "jury_outcome", "jury_outcome VARCHAR(16)"),
    ("report_cases", "escalated_at", "escalated_at DATETIME"),
    ("report_cases", "jury_block_reason", "jury_block_reason VARCHAR(20)"),
    ("report_cases", "publishable", "publishable BOOLEAN NOT NULL DEFAULT 0"),
    ("report_cases", "content_publishable", "content_publishable BOOLEAN NOT NULL DEFAULT 0"),
    ("report_cases", "overturned_at", "overturned_at DATETIME"),
    ("report_cases", "overturned_by_id", "overturned_by_id INTEGER"),
    ("report_cases", "overturn_note", "overturn_note TEXT"),
    ("users", "jury_badge", "jury_badge VARCHAR(12)"),
    ("report_cases", "golden_verified", "golden_verified BOOLEAN NOT NULL DEFAULT 0"),
    ("report_cases", "golden_source", "golden_source VARCHAR(12)"),
    ("report_cases", "golden_explanation", "golden_explanation TEXT"),
    ("report_cases", "golden_outcome", "golden_outcome VARCHAR(12)"),
    ("report_cases", "golden_level", "golden_level INTEGER"),
    ("report_cases", "golden_category", "golden_category VARCHAR(40)"),
    ("jury_members", "points", "points INTEGER NOT NULL DEFAULT 0"),
    ("jury_members", "badge_visible", "badge_visible BOOLEAN NOT NULL DEFAULT 0"),
    ("jury_members", "serving_since", "serving_since DATETIME"),
    ("jury_members", "streak_days", "streak_days INTEGER NOT NULL DEFAULT 0"),
    ("jury_members", "streak_last_day", "streak_last_day DATE"),
    ("jury_members", "cooldown_until", "cooldown_until DATETIME"),
    ("jury_members", "votes_since_quality", "votes_since_quality INTEGER NOT NULL DEFAULT 0"),
    ("jury_members", "force_quality", "force_quality BOOLEAN NOT NULL DEFAULT 0"),
    ("jury_votes", "category", "category VARCHAR(40)"),
    ("jury_votes", "recused_at", "recused_at DATETIME"),
    ("conversations", "last_mention_all_message_id", "last_mention_all_message_id INTEGER"),
    ("conversation_members", "last_mention_message_id", "last_mention_message_id INTEGER"),
    ("users", "account_level", "account_level INTEGER NOT NULL DEFAULT 0"),
    ("users", "account_active_tenths", "account_active_tenths INTEGER NOT NULL DEFAULT 0"),
    ("users", "account_streak_days", "account_streak_days INTEGER NOT NULL DEFAULT 0"),
    ("users", "account_streak_day", "account_streak_day DATE"),
    ("friend_bonds", "sunmoon_days", "sunmoon_days INTEGER NOT NULL DEFAULT 0"),
    ("report_cases", "case_origin", "case_origin VARCHAR(12) NOT NULL DEFAULT 'report'"),
    ("report_cases", "snapshot", "snapshot TEXT"),
    ("report_cases", "panel_at", "panel_at DATETIME"),
    ("report_cases", "panel_outcome", "panel_outcome VARCHAR(16)"),
    ("jury_votes", "stage", "stage VARCHAR(8) NOT NULL DEFAULT 'jury'"),
    ("user_violations", "appeal_stage", "appeal_stage VARCHAR(8)"),
    ("user_violations", "appeal_result", "appeal_result VARCHAR(8)"),
    ("user_violations", "appeal_decided_by", "appeal_decided_by VARCHAR(8)"),
    ("jury_votes", "past_recusals", "past_recusals INTEGER NOT NULL DEFAULT 0"),
    ("users", "cosmetics", "cosmetics TEXT"),
    ("users", "profile_like_count", "profile_like_count INTEGER NOT NULL DEFAULT 0"),
    ("users", "charm", "charm INTEGER NOT NULL DEFAULT 0"),
    ("users", "birthday_year", "birthday_year INTEGER"),
    ("users", "birthday_month", "birthday_month INTEGER"),
    ("users", "birthday_day", "birthday_day INTEGER"),
    ("users", "birthday_show_year", "birthday_show_year BOOLEAN NOT NULL DEFAULT 0"),
    ("users", "birthday_set_at", "birthday_set_at DATETIME"),
    ("users", "birthday_changed_at", "birthday_changed_at DATETIME"),
    ("users", "birthday_celebrated_year", "birthday_celebrated_year INTEGER"),
    ("shop_items", "duration_days", "duration_days INTEGER"),
    ("shop_purchases", "expires_at", "expires_at DATETIME"),
    ("shop_purchases", "duration_days", "duration_days INTEGER"),
    ("shop_purchases", "superseded_at", "superseded_at DATETIME"),
    ("shop_purchases", "from_user_id", "from_user_id INTEGER"),
    ("shop_purchases", "gift_record_id", "gift_record_id INTEGER"),
    ("shop_purchases", "expiring_notified_at", "expiring_notified_at DATETIME"),
    ("shop_purchases", "expired_notified_at", "expired_notified_at DATETIME"),
    ("conversations", "plus_tier", "plus_tier VARCHAR(12) NOT NULL DEFAULT 'free'"),
    ("conversations", "plus_expires_at", "plus_expires_at DATETIME"),
    ("conversations", "plus_files_tier", "plus_files_tier VARCHAR(12)"),
    ("conversations", "plus_files_until", "plus_files_until DATETIME"),
    ("conversations", "file_default_ttl_hours", "file_default_ttl_hours INTEGER NOT NULL DEFAULT 24"),
    ("conversations", "member_limit_override", "member_limit_override INTEGER"),
    ("group_files", "temporary", "temporary BOOLEAN NOT NULL DEFAULT 0"),
    ("group_files", "folder_id", "folder_id INTEGER"),
    ("group_file_uploads", "folder_id", "folder_id INTEGER"),
    ("friend_requests", "ignored_at", "ignored_at DATETIME"),
    ("users", "notifications_seen_at", "notifications_seen_at DATETIME"),
    ("red_packets", "chain_text", "chain_text VARCHAR(60)"),
    ("red_packets", "target_user_id", "target_user_id INTEGER"),
    ("group_emojis", "sort_order", "sort_order INTEGER"),
    ("group_join_requests", "answer", "answer VARCHAR(500)"),
    ("content_moderation_records", "post_id", "post_id INTEGER"),
    ("content_moderation_records", "post_topic_id", "post_topic_id INTEGER"),
    ("content_moderation_records", "post_comment_id", "post_comment_id INTEGER"),
    ("content_moderation_records", "profile_user_id", "profile_user_id INTEGER"),
    ("content_moderation_records", "teamup_profile_id", "teamup_profile_id INTEGER"),
    ("content_moderation_records", "teamup_comment_id", "teamup_comment_id INTEGER"),
    ("teamup_profiles", "background_url", "background_url VARCHAR(500)"),
    # 群公告 v2（docs/group-announcements.md）
    ("group_announcements", "images_json", "images_json TEXT"),
    ("group_announcements", "pinned_at", "pinned_at DATETIME"),
    ("group_announcements", "popup", "popup BOOLEAN NOT NULL DEFAULT 0"),
    ("group_announcements", "popup_since", "popup_since DATETIME"),
    ("group_announcements", "require_confirm", "require_confirm BOOLEAN NOT NULL DEFAULT 0"),
    ("group_announcements", "send_to_new_members", "send_to_new_members BOOLEAN NOT NULL DEFAULT 0"),
    ("group_announcements", "new_members_since", "new_members_since DATETIME"),
    ("group_announcements", "guide_nickname", "guide_nickname BOOLEAN NOT NULL DEFAULT 0"),
    ("group_announcements", "edited_at", "edited_at DATETIME"),
    # 二审（docs/moderation-v2.md 1.10）
    ("content_moderation_records", "judge_p", "judge_p FLOAT"),
    ("content_moderation_records", "judge_ms", "judge_ms INTEGER"),
    ("content_moderation_records", "judge_outcome", "judge_outcome VARCHAR(12)"),
    ("report_cases", "judge_p", "judge_p FLOAT"),
)

_ADDED_INDEXES: tuple[tuple[str, str], ...] = (
    ("users", "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_registration_ip ON users (registration_ip)"),
    ("users", "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_ccw_student_oid ON users (ccw_student_oid)"),
    ("user_reports", "CREATE INDEX IF NOT EXISTS ix_user_reports_case_id ON user_reports (case_id)"),
    ("messages", "CREATE INDEX IF NOT EXISTS ix_messages_conversation_id_id ON messages (conversation_id, id)"),
    ("messages", "CREATE INDEX IF NOT EXISTS ix_messages_sender_created_at ON messages (sender_id, created_at)"),
    ("conversation_members", "CREATE INDEX IF NOT EXISTS ix_conversation_members_last_read_message_id ON conversation_members (last_read_message_id)"),
    ("conversation_members", "CREATE INDEX IF NOT EXISTS ix_conversation_members_cleared_before_message_id ON conversation_members (cleared_before_message_id)"),
    ("conversation_members", "CREATE INDEX IF NOT EXISTS ix_conversation_members_user_pinned ON conversation_members (user_id, pinned)"),
    ("conversation_members", "CREATE INDEX IF NOT EXISTS ix_conversation_members_conversation_level_exp ON conversation_members (conversation_id, level_exp)"),
    ("conversation_members", "CREATE INDEX IF NOT EXISTS ix_conversation_members_conversation_activity_score ON conversation_members (conversation_id, activity_score)"),
    ("group_checkins", "CREATE INDEX IF NOT EXISTS ix_group_checkins_conversation_date ON group_checkins (conversation_id, checkin_date)"),
    ("group_member_daily_activity", "CREATE INDEX IF NOT EXISTS ix_group_member_daily_activity_conversation_date ON group_member_daily_activity (conversation_id, activity_date)"),
    ("group_member_exp_logs", "CREATE INDEX IF NOT EXISTS ix_group_member_exp_logs_conversation_user_created ON group_member_exp_logs (conversation_id, user_id, created_at)"),
    ("message_visibilities", "CREATE INDEX IF NOT EXISTS ix_message_visibilities_user_message ON message_visibilities (user_id, message_id)"),
    ("message_bookmarks", "CREATE INDEX IF NOT EXISTS ix_message_bookmarks_user_created ON message_bookmarks (user_id, created_at)"),
    ("message_reactions", "CREATE INDEX IF NOT EXISTS ix_message_reactions_message_emoji ON message_reactions (message_id, emoji)"),
    ("posts", "CREATE INDEX IF NOT EXISTS ix_posts_author_created ON posts (author_id, created_at)"),
    ("posts", "CREATE INDEX IF NOT EXISTS ix_posts_visibility_created ON posts (visibility, created_at)"),
    ("posts", "CREATE INDEX IF NOT EXISTS ix_posts_repost_of_id ON posts (repost_of_id)"),
    ("post_likes", "CREATE INDEX IF NOT EXISTS ix_post_likes_post_created ON post_likes (post_id, created_at)"),
    ("post_comments", "CREATE INDEX IF NOT EXISTS ix_post_comments_post_created ON post_comments (post_id, created_at)"),
    ("post_comment_likes", "CREATE INDEX IF NOT EXISTS ix_post_comment_likes_comment_created ON post_comment_likes (comment_id, created_at)"),
    ("posts", "CREATE INDEX IF NOT EXISTS ix_posts_author_pinned_created ON posts (author_id, pinned_at, created_at)"),
    ("post_notifications", "CREATE INDEX IF NOT EXISTS ix_post_notifications_recipient_read_created ON post_notifications (recipient_id, read_at, created_at)"),
    ("post_topic_links", "CREATE INDEX IF NOT EXISTS ix_post_topic_links_topic_post ON post_topic_links (topic_id, post_id)"),
    ("bot_reviews", "CREATE INDEX IF NOT EXISTS ix_bot_reviews_bot_updated ON bot_reviews (bot_id, updated_at)"),
    ("bot_review_replies", "CREATE INDEX IF NOT EXISTS ix_bot_review_replies_review_created ON bot_review_replies (review_id, created_at)"),
    ("bot_review_likes", "CREATE INDEX IF NOT EXISTS ix_bot_review_likes_review_created ON bot_review_likes (review_id, created_at)"),
    ("bot_review_likes", "CREATE INDEX IF NOT EXISTS ix_bot_review_likes_reply_created ON bot_review_likes (reply_id, created_at)"),
    ("post_mentions", "CREATE INDEX IF NOT EXISTS ix_post_mentions_mentioned_created ON post_mentions (mentioned_user_id, created_at)"),
    ("ccw_binding_challenges", "CREATE INDEX IF NOT EXISTS ix_ccw_binding_challenges_user_status ON ccw_binding_challenges (user_id, status)"),
    ("teamup_profiles", "CREATE INDEX IF NOT EXISTS ix_teamup_profiles_event_status ON teamup_profiles (event_id, status, moderation_status)"),
    ("teamup_profiles", "CREATE INDEX IF NOT EXISTS ix_teamup_profiles_pinned_created ON teamup_profiles (pinned_at, created_at)"),
    ("teamup_comments", "CREATE INDEX IF NOT EXISTS ix_teamup_comments_profile_created ON teamup_comments (profile_id, created_at)"),
    ("teamup_comment_likes", "CREATE INDEX IF NOT EXISTS ix_teamup_comment_likes_comment_created ON teamup_comment_likes (comment_id, created_at)"),
    ("tasks", "CREATE INDEX IF NOT EXISTS ix_tasks_conversation_status ON tasks (conversation_id, status)"),
    ("tasks", "CREATE INDEX IF NOT EXISTS ix_tasks_due_at ON tasks (due_at)"),
    ("tasks", "CREATE INDEX IF NOT EXISTS ix_tasks_parent_id ON tasks (parent_id)"),
    ("task_assignees", "CREATE INDEX IF NOT EXISTS ix_task_assignees_user ON task_assignees (user_id)"),
    ("task_watchers", "CREATE INDEX IF NOT EXISTS ix_task_watchers_user ON task_watchers (user_id)"),
    ("task_activities", "CREATE INDEX IF NOT EXISTS ix_task_activities_task_created ON task_activities (task_id, created_at)"),
    ("task_comments", "CREATE INDEX IF NOT EXISTS ix_task_comments_task_created ON task_comments (task_id, created_at)"),
    ("task_groups", "CREATE INDEX IF NOT EXISTS ix_task_groups_owner ON task_groups (owner_id)"),
    ("task_group_items", "CREATE INDEX IF NOT EXISTS ix_task_group_items_owner ON task_group_items (owner_id)"),
    ("mention_all_usages", "CREATE UNIQUE INDEX IF NOT EXISTS uq_mention_all_usage_user_date ON mention_all_usages (user_id, usage_date)"),
    # 限时商品、礼物（docs/shop.md 2.5、docs/gifts.md）。shop_purchases 的唯一索引换了条件（续期之后被接替的行不再算
    # 「当前拥有」）：先建新的再删旧的，中间任何时刻都至少有一个唯一索引管着，不会出现「一件商品两行当前记录」。
    ("users", "CREATE INDEX IF NOT EXISTS ix_users_charm ON users (charm)"),
    ("shop_purchases", "CREATE INDEX IF NOT EXISTS ix_shop_purchases_expires ON shop_purchases (expires_at)"),
    (
        "shop_purchases",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_shop_purchases_live ON shop_purchases (user_id, item_id) WHERE revoked_at IS NULL AND superseded_at IS NULL",
    ),
    ("shop_purchases", "DROP INDEX IF EXISTS uq_shop_purchases_owner"),
    # 群升级、群文件（docs/group-plus.md）
    ("conversations", "CREATE INDEX IF NOT EXISTS ix_conversations_plus_expires ON conversations (plus_expires_at)"),
    ("group_files", "CREATE INDEX IF NOT EXISTS ix_group_files_folder ON group_files (conversation_id, folder_id)"),
    # 二审等到一半进程退出留下的 pending：重试循环按这个找（部分索引，平时几乎是空的）
    (
        "content_moderation_records",
        "CREATE INDEX IF NOT EXISTS ix_moderation_records_judge_pending ON content_moderation_records (created_at) WHERE judge_outcome = 'pending'",
    ),
)

# 存量数据的回填，可重复执行（WHERE 条件挡住已经回填过的行）。布尔值一律走绑定参数，SQLite 的 1 / PG 的 true 由驱动处理。
_BACKFILLS: tuple[tuple[str, str, dict], ...] = (
    # 风纪二期：上线前就在题库里的题算 legacy（未核对）；在任委员的「本次任职开始时间」用审批时间补上
    ("report_cases", "UPDATE report_cases SET golden_source = 'legacy' WHERE is_golden = :yes AND golden_source IS NULL", {"yes": True}),
    (
        "jury_members",
        "UPDATE jury_members SET serving_since = COALESCE(reviewed_at, applied_at) WHERE serving_since IS NULL AND status = 'active'",
        {},
    ),
    # 风纪三期：上线前的申诉都是管理员处理的
    ("user_violations", "UPDATE user_violations SET appeal_stage = 'admin' WHERE appeal_status = 'pending' AND appeal_stage IS NULL", {}),
    (
        "user_violations",
        "UPDATE user_violations SET appeal_decided_by = 'admin', "
        "appeal_result = CASE WHEN appeal_status = 'accepted' THEN 'revoked' ELSE 'upheld' END "
        "WHERE appeal_status IN ('accepted', 'rejected') AND appeal_decided_by IS NULL",
        {},
    ),
    # 早期按枚举名（大写）存过的几列改成按值（小写）
    (
        "users",
        "UPDATE users SET presence_status = lower(presence_status) WHERE presence_status IN ('ONLINE', 'AWAY', 'BUSY', 'DND', 'CREATING', 'GAMING', 'CUSTOM')",
        {},
    ),
    ("posts", "UPDATE posts SET visibility = lower(visibility) WHERE visibility IN ('PUBLIC', 'FRIENDS', 'PRIVATE')", {}),
    ("posts", "UPDATE posts SET moderation_status = lower(moderation_status) WHERE moderation_status IN ('PENDING', 'APPROVED', 'REJECTED')", {}),
    ("post_topics", "UPDATE post_topics SET moderation_status = lower(moderation_status) WHERE moderation_status IN ('PENDING', 'APPROVED', 'REJECTED')", {}),
    ("post_comments", "UPDATE post_comments SET moderation_status = lower(moderation_status) WHERE moderation_status IN ('PENDING', 'APPROVED', 'REJECTED')", {}),
    ("bot_reviews", "UPDATE bot_reviews SET moderation_status = lower(moderation_status) WHERE moderation_status IN ('PENDING', 'APPROVED', 'REJECTED')", {}),
    (
        "bot_review_replies",
        "UPDATE bot_review_replies SET moderation_status = lower(moderation_status) WHERE moderation_status IN ('PENDING', 'APPROVED', 'REJECTED')",
        {},
    ),
    # 群公告 v2：以前就置顶的那条补上置顶时间（列表按置顶时间排）
    ("group_announcements", "UPDATE group_announcements SET pinned_at = updated_at WHERE pinned = :yes AND pinned_at IS NULL", {"yes": True}),
)

# 两个 worker 同时启动时，PG 上用这个 advisory lock 把建表 / 迁移串起来（任意固定的整数，"kuke"）
INIT_LOCK_KEY = 0x6B756B65


def _column_ddl(dialect_name: str, column_sql: str) -> str:
    """_ADDED_COLUMNS 里按 SQLite 写的列定义 → 当前方言。"""
    if dialect_name != "postgresql":
        return column_sql
    sql = re.sub(r"\bDATETIME\b", "TIMESTAMP WITH TIME ZONE", column_sql)
    sql = re.sub(r"\b(BOOLEAN NOT NULL DEFAULT) 0\b", r"\1 false", sql)
    sql = re.sub(r"\b(BOOLEAN NOT NULL DEFAULT) 1\b", r"\1 true", sql)
    return sql


def _schema_snapshot(sync_conn: Connection) -> dict[str, set[str]]:
    """{表名: {列名}}，用 SQLAlchemy 的 inspect，两种数据库都适用。"""
    columns = inspect(sync_conn).get_multi_columns()
    return {table: {column["name"] for column in cols} for (_schema, table), cols in columns.items()}


def _sqlite_legacy_rebuilds(sync_conn: Connection) -> None:
    """只有很老的 SQLite 库才会走到的重建（SQLite 不能改列约束，只能整表重建）。PG 库都是按当前模型新建的，用不到。"""
    user_columns = {row[1]: row for row in sync_conn.execute(text("PRAGMA table_info(users)"))}
    if user_columns.get("email") is None or not user_columns["email"][3]:
        return
    user_indexes = [row[1] for row in sync_conn.execute(text("PRAGMA index_list(users)")) if not row[1].startswith("sqlite_autoindex")]
    sync_conn.execute(text("PRAGMA foreign_keys=OFF"))
    sync_conn.execute(text("PRAGMA legacy_alter_table=ON"))
    sync_conn.execute(text("ALTER TABLE users RENAME TO users_old"))
    for index_name in user_indexes:
        safe_index_name = index_name.replace('"', '""')
        sync_conn.execute(text(f'DROP INDEX IF EXISTS "{safe_index_name}"'))
    Base.metadata.tables["users"].create(sync_conn)
    sync_conn.execute(
        text(
            """
            INSERT INTO users (
                id, username, email, hashed_password, nickname, avatar_url, bio, created_at, updated_at
            )
            SELECT id, username, email, hashed_password, nickname, avatar_url, bio, created_at, updated_at
            FROM users_old
            """
        )
    )
    sync_conn.execute(text("DROP TABLE users_old"))
    sync_conn.execute(text("PRAGMA legacy_alter_table=OFF"))
    sync_conn.execute(text("PRAGMA foreign_keys=ON"))


def _sqlite_legacy_featured_unique(sync_conn: Connection) -> None:
    """老版精华表（message_id 带外键、没有快照列）补唯一索引。快照版自带唯一约束，不需要。"""
    featured_columns = {row[1] for row in sync_conn.execute(text("PRAGMA table_info(featured_messages)"))}
    featured_index_names = {row[1] for row in sync_conn.execute(text("PRAGMA index_list(featured_messages)"))}
    if not featured_columns or "snap_type" in featured_columns or "uq_featured_message" in featured_index_names:
        return
    duplicates = sync_conn.execute(text("SELECT message_id FROM featured_messages GROUP BY message_id HAVING COUNT(*) > 1")).all()
    for (message_id,) in duplicates:
        sync_conn.execute(
            text(
                """
                DELETE FROM featured_messages
                WHERE message_id = :message_id
                  AND id NOT IN (
                    SELECT id FROM featured_messages WHERE message_id = :message_id ORDER BY created_at DESC, id DESC LIMIT 1
                  )
                """
            ),
            {"message_id": message_id},
        )
    sync_conn.execute(text("CREATE UNIQUE INDEX uq_featured_message ON featured_messages (message_id)"))


async def _add_missing_columns(conn: AsyncConnection) -> None:
    dialect = conn.dialect.name
    snapshot = await conn.run_sync(_schema_snapshot)
    for table_name, column_name, column_sql in _ADDED_COLUMNS:
        existing = snapshot.get(table_name)
        if existing is None or column_name in existing:
            continue
        ddl = _column_ddl(dialect, column_sql)
        if dialect == "postgresql":
            await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {ddl}"))
        else:
            # 两个 worker 一起启动时，后到的那个可能撞上 duplicate column name：当成别人已经加好了
            try:
                await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {ddl}"))
            except Exception as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        existing.add(column_name)

    # 兜底检查：模型里有、库里没有、清单里也没写的列 —— 说明改了模型忘了加迁移，启动时说出来
    for table in Base.metadata.sorted_tables:
        existing = snapshot.get(table.name)
        if existing is None:
            continue
        missing = [column.name for column in table.columns if column.name not in existing]
        if missing:
            logger.error("init_db: table %s is missing columns %s (add them to _ADDED_COLUMNS)", table.name, missing)


async def _add_missing_indexes(conn: AsyncConnection) -> None:
    table_names = set((await conn.run_sync(_schema_snapshot)).keys())
    for table_name, index_sql in _ADDED_INDEXES:
        if table_name not in table_names:
            continue
        try:
            await conn.execute(text(index_sql))
        except Exception as exc:
            # SQLite 上两个 worker 一起启动时后到的可能撞上 already exists；PG 由 advisory lock 串行，不会走到这里
            if conn.dialect.name == "postgresql" or "already exists" not in str(exc).lower():
                raise


async def _run_backfills(conn: AsyncConnection) -> None:
    table_names = set((await conn.run_sync(_schema_snapshot)).keys())
    for table_name, sql, params in _BACKFILLS:
        if table_name in table_names:
            await conn.execute(text(sql), params)


async def init_db(target: AsyncEngine | None = None) -> None:
    """建表 + 增量迁移，可重复执行。启动时调用；数据迁移脚本（scripts/migrate_sqlite_to_postgres.py）也用它在目标库上建结构。"""
    import app.models  # noqa: F401

    async with (target or engine).begin() as conn:
        if conn.dialect.name == "postgresql":
            # 迁移可能比角色上的 statement_timeout / lock_timeout 慢（首次建搜索索引、等另一个 worker 的锁），只在这个事务里放开。
            # 两个 worker 同时启动：advisory lock 让后到的那个等前一个提交，之后看到的就是建好的表，不会撞 already exists。
            # PG 的 DDL 是事务性的：中途失败整个回滚，不会留下建了一半的结构。
            await conn.execute(text("SET LOCAL statement_timeout = 0"))
            await conn.execute(text("SET LOCAL lock_timeout = 0"))
            await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": INIT_LOCK_KEY})
            await conn.run_sync(Base.metadata.create_all)
        else:
            # SQLite 没有 advisory lock：create_all 是「先看有没有、没有再建」，两个 worker 可能都看到新表（或它的索引）
            # 不存在、都去建，后到的那个会撞上 already exists。那就再来一遍 —— 这时别人已经建好的会被跳过，只补还缺的。
            for attempt in range(5):
                try:
                    await conn.run_sync(Base.metadata.create_all)
                    break
                except Exception as exc:
                    if "already exists" not in str(exc).lower() or attempt == 4:
                        raise
            await conn.run_sync(_sqlite_legacy_rebuilds)

        await _add_missing_columns(conn)
        await _run_backfills(conn)
        if conn.dialect.name == "sqlite":
            await conn.run_sync(_sqlite_legacy_featured_unique)
        await _add_missing_indexes(conn)
