from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_async_engine(settings.database_url, echo=False, future=True, connect_args=connect_args)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    """SQLite 的默认设置扛不住多进程聊天服务，每条连接都得调一遍。

    默认的 rollback journal（``journal_mode=delete``）每次提交都要对整库加排他
    锁、建日志文件、fsync 两次再删掉，读写还互相阻塞 —— 多 worker 下发消息会
    肉眼可见地卡。WAL 让读不挡写、写不挡读，提交也只是往 -wal 追加。

    ``synchronous=NORMAL`` 在 WAL 下依旧不会损坏数据库，最坏情况是断电时丢掉
    最后几个事务；换来的是省掉每次提交的那次 fsync。

    ``journal_mode`` 是写在库文件头里的，设一次就一直生效，之后每次连接执行它
    只是一次廉价的读取；``synchronous`` 和 ``busy_timeout`` 是连接级的，必须
    每条连接都设。
    """
    if not settings.database_url.startswith("sqlite"):
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


async def init_db() -> None:
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        # 线上是两个 worker 一起启动：create_all 是「先看有没有、没有再建」，两边可能都看到新表（或它的索引）不存在、都去建，
        # 后到的那个会撞上 already exists。那就再来一遍 —— 这时别人已经建好的会被跳过，只补还缺的。
        for attempt in range(5):
            try:
                await conn.run_sync(Base.metadata.create_all)
                break
            except Exception as exc:
                if "already exists" not in str(exc).lower() or attempt == 4:
                    raise
        if conn.dialect.name == "sqlite":
            columns = await conn.execute(text("PRAGMA table_info(users)"))
            user_columns = {row[1]: row for row in columns}
            if user_columns.get("email") is not None and user_columns["email"][3]:
                indexes = await conn.execute(text("PRAGMA index_list(users)"))
                user_indexes = [row[1] for row in indexes if not row[1].startswith("sqlite_autoindex")]
                await conn.execute(text("PRAGMA foreign_keys=OFF"))
                await conn.execute(text("PRAGMA legacy_alter_table=ON"))
                await conn.execute(text("ALTER TABLE users RENAME TO users_old"))
                for index_name in user_indexes:
                    safe_index_name = index_name.replace('"', '""')
                    await conn.execute(text(f'DROP INDEX IF EXISTS "{safe_index_name}"'))
                await conn.run_sync(Base.metadata.tables["users"].create)
                await conn.execute(
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
                await conn.execute(text("DROP TABLE users_old"))
                await conn.execute(text("PRAGMA legacy_alter_table=OFF"))
                await conn.execute(text("PRAGMA foreign_keys=ON"))

            tables = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            table_names = {row[0] for row in tables}
            if "user_reports" not in table_names:
                await conn.run_sync(Base.metadata.tables["user_reports"].create)
                table_names.add("user_reports")
            for table_name in ("posts", "post_likes", "post_comments", "post_comment_likes", "post_topics", "post_topic_links", "post_mentions", "post_notifications"):
                if table_name not in table_names and table_name in Base.metadata.tables:
                    await conn.run_sync(Base.metadata.tables[table_name].create)
                    table_names.add(table_name)

            for table_name, column_name, column_sql in (
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
                # 风纪 v2 二期
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
            ):
                if table_name not in table_names:
                    continue
                columns = await conn.execute(text(f"PRAGMA table_info({table_name})"))
                existing_columns = {row[1] for row in columns}
                if column_name not in existing_columns:
                    await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))

            # 会话列表上的「有人@我 / @全体成员」（docs/chat-features.md 第 7 节）：只是两个可空的整数列，不用回填、不用索引。
            # 线上是两个 worker 一起启动，可能都看到列不存在、都去 ALTER：后到的那个会撞上 duplicate column name，
            # 这里把它当成「别人已经加好了」。稳妥起见上线前仍然先单进程跑一遍迁移。
            for table_name, column_name, column_sql in (
                ("conversations", "last_mention_all_message_id", "last_mention_all_message_id INTEGER"),
                ("conversation_members", "last_mention_message_id", "last_mention_message_id INTEGER"),
            ):
                if table_name not in table_names:
                    continue
                columns = await conn.execute(text(f"PRAGMA table_info({table_name})"))
                if column_name not in {row[1] for row in columns}:
                    try:
                        await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))
                    except Exception as exc:
                        if "duplicate column name" not in str(exc).lower():
                            raise

            # 账号等级（docs/account-level.md）。新表 account_level_days / account_level_notifications 由上面的 create_all 建；
            # 老用户的折算不在这里做，见 services/account_levels.py 的 backfill_legacy（后台循环启动时跑一次）。
            for table_name, column_name, column_sql in (
                ("users", "account_level", "account_level INTEGER NOT NULL DEFAULT 0"),
                ("users", "account_active_tenths", "account_active_tenths INTEGER NOT NULL DEFAULT 0"),
                ("users", "account_streak_days", "account_streak_days INTEGER NOT NULL DEFAULT 0"),
                ("users", "account_streak_day", "account_streak_day DATE"),
            ):
                if table_name not in table_names:
                    continue
                columns = await conn.execute(text(f"PRAGMA table_info({table_name})"))
                if column_name not in {row[1] for row in columns}:
                    await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))

            # 好友亲密度（docs/intimacy.md）。friend_bonds 是新表；这一列是表建好之后才加的
            for table_name, column_name, column_sql in (("friend_bonds", "sunmoon_days", "sunmoon_days INTEGER NOT NULL DEFAULT 0"),):
                if table_name not in table_names:
                    continue
                columns = await conn.execute(text(f"PRAGMA table_info({table_name})"))
                if column_name not in {row[1] for row in columns}:
                    await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))

            # 风纪二期的存量回填，可重复执行：上线前就在题库里的题算 legacy（未核对）；
            # 在任委员的「本次任职开始时间」用审批时间补上。
            if "report_cases" in table_names:
                await conn.execute(text("UPDATE report_cases SET golden_source = 'legacy' WHERE is_golden = 1 AND golden_source IS NULL"))
            if "jury_members" in table_names:
                await conn.execute(
                    text("UPDATE jury_members SET serving_since = COALESCE(reviewed_at, applied_at) WHERE serving_since IS NULL AND status = 'active'")
                )

            # 风纪三期（资深合议、申诉复审、机器案源、复议）。新表 jury_reviews / jury_review_votes 由上面的 create_all 建。
            for table_name, column_name, column_sql in (
                ("report_cases", "case_origin", "case_origin VARCHAR(12) NOT NULL DEFAULT 'report'"),
                ("report_cases", "snapshot", "snapshot TEXT"),
                ("report_cases", "panel_at", "panel_at DATETIME"),
                ("report_cases", "panel_outcome", "panel_outcome VARCHAR(16)"),
                ("jury_votes", "stage", "stage VARCHAR(8) NOT NULL DEFAULT 'jury'"),
                ("user_violations", "appeal_stage", "appeal_stage VARCHAR(8)"),
                ("user_violations", "appeal_result", "appeal_result VARCHAR(8)"),
                ("user_violations", "appeal_decided_by", "appeal_decided_by VARCHAR(8)"),
                # 回避过的质控题再出给同一个人时复用原来那一行，之前回避了几次记在这里
                ("jury_votes", "past_recusals", "past_recusals INTEGER NOT NULL DEFAULT 0"),
            ):
                if table_name not in table_names:
                    continue
                columns = await conn.execute(text(f"PRAGMA table_info({table_name})"))
                if column_name not in {row[1] for row in columns}:
                    await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))
            # 存量申诉的回填，可重复执行：上线前的申诉都是管理员处理的。
            if "user_violations" in table_names:
                await conn.execute(text("UPDATE user_violations SET appeal_stage = 'admin' WHERE appeal_status = 'pending' AND appeal_stage IS NULL"))
                await conn.execute(
                    text(
                        "UPDATE user_violations SET appeal_decided_by = 'admin', "
                        "appeal_result = CASE WHEN appeal_status = 'accepted' THEN 'revoked' ELSE 'upheld' END "
                        "WHERE appeal_status IN ('accepted', 'rejected') AND appeal_decided_by IS NULL"
                    )
                )

            await conn.execute(text("UPDATE users SET presence_status = lower(presence_status) WHERE presence_status IN ('ONLINE', 'AWAY', 'BUSY', 'DND', 'CREATING', 'GAMING', 'CUSTOM')"))
            await conn.execute(text("UPDATE posts SET visibility = lower(visibility) WHERE visibility IN ('PUBLIC', 'FRIENDS', 'PRIVATE')"))
            await conn.execute(text("UPDATE posts SET moderation_status = lower(moderation_status) WHERE moderation_status IN ('PENDING', 'APPROVED', 'REJECTED')"))
            await conn.execute(text("UPDATE post_topics SET moderation_status = lower(moderation_status) WHERE moderation_status IN ('PENDING', 'APPROVED', 'REJECTED')"))
            await conn.execute(text("UPDATE post_comments SET moderation_status = lower(moderation_status) WHERE moderation_status IN ('PENDING', 'APPROVED', 'REJECTED')"))
            await conn.execute(text("UPDATE bot_reviews SET moderation_status = lower(moderation_status) WHERE moderation_status IN ('PENDING', 'APPROVED', 'REJECTED')"))
            await conn.execute(text("UPDATE bot_review_replies SET moderation_status = lower(moderation_status) WHERE moderation_status IN ('PENDING', 'APPROVED', 'REJECTED')"))

            if "group_join_requests" not in table_names:
                await conn.run_sync(Base.metadata.tables["group_join_requests"].create)
                table_names.add("group_join_requests")
            columns = await conn.execute(text("PRAGMA table_info(group_join_requests)"))
            existing_columns = {row[1] for row in columns}
            if "answer" not in existing_columns:
                await conn.execute(text("ALTER TABLE group_join_requests ADD COLUMN answer VARCHAR(500)"))
            if "admin_audit_logs" not in table_names:
                await conn.run_sync(Base.metadata.tables["admin_audit_logs"].create)
            if "temporary_conversation_blocks" not in table_names:
                await conn.run_sync(Base.metadata.tables["temporary_conversation_blocks"].create)
            if "password_reset_requests" not in table_names:
                await conn.run_sync(Base.metadata.tables["password_reset_requests"].create)
            if "invite_links" not in table_names:
                await conn.run_sync(Base.metadata.tables["invite_links"].create)
            if "auth_sessions" not in table_names:
                await conn.run_sync(Base.metadata.tables["auth_sessions"].create)
            if "ccw_binding_challenges" not in table_names:
                await conn.run_sync(Base.metadata.tables["ccw_binding_challenges"].create)
                table_names.add("ccw_binding_challenges")
            if "group_checkins" not in table_names:
                await conn.run_sync(Base.metadata.tables["group_checkins"].create)
            if "group_member_daily_activity" not in table_names:
                await conn.run_sync(Base.metadata.tables["group_member_daily_activity"].create)
            if "group_member_exp_logs" not in table_names:
                await conn.run_sync(Base.metadata.tables["group_member_exp_logs"].create)
            if "sticker_assets" not in table_names:
                await conn.run_sync(Base.metadata.tables["sticker_assets"].create)
            if "favorite_stickers" not in table_names:
                await conn.run_sync(Base.metadata.tables["favorite_stickers"].create)
            if "system_settings" not in table_names:
                await conn.run_sync(Base.metadata.tables["system_settings"].create)
            if "announcements" not in table_names:
                await conn.run_sync(Base.metadata.tables["announcements"].create)
                table_names.add("announcements")
            if "content_moderation_records" not in table_names:
                await conn.run_sync(Base.metadata.tables["content_moderation_records"].create)
            for table_name in ("teamup_events", "teamup_profiles", "teamup_comments", "teamup_comment_likes"):
                if table_name not in table_names and table_name in Base.metadata.tables:
                    await conn.run_sync(Base.metadata.tables[table_name].create)
                    table_names.add(table_name)
            for table_name in ("task_lists", "tasks", "task_assignees", "task_watchers", "task_activities", "task_comments", "task_groups", "task_group_items"):
                if table_name not in table_names and table_name in Base.metadata.tables:
                    await conn.run_sync(Base.metadata.tables[table_name].create)
                    table_names.add(table_name)
            if "bots" not in table_names:
                await conn.run_sync(Base.metadata.tables["bots"].create)
                table_names.add("bots")
            if "bot_installations" not in table_names:
                await conn.run_sync(Base.metadata.tables["bot_installations"].create)
                table_names.add("bot_installations")
            if "bot_reviews" not in table_names:
                await conn.run_sync(Base.metadata.tables["bot_reviews"].create)
                table_names.add("bot_reviews")
            if "bot_review_replies" not in table_names:
                await conn.run_sync(Base.metadata.tables["bot_review_replies"].create)
                table_names.add("bot_review_replies")
            if "bot_review_likes" not in table_names:
                await conn.run_sync(Base.metadata.tables["bot_review_likes"].create)
                table_names.add("bot_review_likes")
            if "post_comment_likes" not in table_names:
                await conn.run_sync(Base.metadata.tables["post_comment_likes"].create)
            for table_name in ("post_topics", "post_topic_links", "post_mentions", "post_notifications"):
                if table_name not in table_names and table_name in Base.metadata.tables:
                    await conn.run_sync(Base.metadata.tables[table_name].create)
                    table_names.add(table_name)
            moderation_columns = await conn.execute(text("PRAGMA table_info(content_moderation_records)"))
            existing_moderation_columns = {row[1] for row in moderation_columns}
            if "post_id" not in existing_moderation_columns:
                await conn.execute(text("ALTER TABLE content_moderation_records ADD COLUMN post_id INTEGER"))
            if "post_topic_id" not in existing_moderation_columns:
                await conn.execute(text("ALTER TABLE content_moderation_records ADD COLUMN post_topic_id INTEGER"))
            if "post_comment_id" not in existing_moderation_columns:
                await conn.execute(text("ALTER TABLE content_moderation_records ADD COLUMN post_comment_id INTEGER"))
            if "profile_user_id" not in existing_moderation_columns:
                await conn.execute(text("ALTER TABLE content_moderation_records ADD COLUMN profile_user_id INTEGER"))
            if "teamup_profile_id" not in existing_moderation_columns:
                await conn.execute(text("ALTER TABLE content_moderation_records ADD COLUMN teamup_profile_id INTEGER"))
            if "teamup_comment_id" not in existing_moderation_columns:
                await conn.execute(text("ALTER TABLE content_moderation_records ADD COLUMN teamup_comment_id INTEGER"))

            if "teamup_profiles" in table_names:
                teamup_profile_columns = await conn.execute(text("PRAGMA table_info(teamup_profiles)"))
                existing_teamup_profile_columns = {row[1] for row in teamup_profile_columns}
                if "background_url" not in existing_teamup_profile_columns:
                    await conn.execute(text("ALTER TABLE teamup_profiles ADD COLUMN background_url VARCHAR(500)"))

            indexes = await conn.execute(text("PRAGMA index_list(users)"))
            user_indexes = {row[1] for row in indexes}
            if "ix_users_registration_ip" not in user_indexes:
                await conn.execute(text("CREATE UNIQUE INDEX ix_users_registration_ip ON users (registration_ip)"))
            if "ix_users_ccw_student_oid" not in user_indexes:
                await conn.execute(text("CREATE UNIQUE INDEX ix_users_ccw_student_oid ON users (ccw_student_oid)"))

            for table_name, index_name, index_sql in (
                (
                    "user_reports",
                    "ix_user_reports_case_id",
                    "CREATE INDEX ix_user_reports_case_id ON user_reports (case_id)",
                ),
                (
                    "messages",
                    "ix_messages_conversation_id_id",
                    "CREATE INDEX ix_messages_conversation_id_id ON messages (conversation_id, id)",
                ),
                (
                    "messages",
                    "ix_messages_sender_created_at",
                    "CREATE INDEX ix_messages_sender_created_at ON messages (sender_id, created_at)",
                ),
                (
                    "conversation_members",
                    "ix_conversation_members_last_read_message_id",
                    "CREATE INDEX ix_conversation_members_last_read_message_id ON conversation_members (last_read_message_id)",
                ),
                (
                    "conversation_members",
                    "ix_conversation_members_cleared_before_message_id",
                    "CREATE INDEX ix_conversation_members_cleared_before_message_id ON conversation_members (cleared_before_message_id)",
                ),
                (
                    "conversation_members",
                    "ix_conversation_members_user_pinned",
                    "CREATE INDEX ix_conversation_members_user_pinned ON conversation_members (user_id, pinned)",
                ),
                (
                    "conversation_members",
                    "ix_conversation_members_conversation_level_exp",
                    "CREATE INDEX ix_conversation_members_conversation_level_exp ON conversation_members (conversation_id, level_exp)",
                ),
                (
                    "conversation_members",
                    "ix_conversation_members_conversation_activity_score",
                    "CREATE INDEX ix_conversation_members_conversation_activity_score ON conversation_members (conversation_id, activity_score)",
                ),
                (
                    "group_checkins",
                    "ix_group_checkins_conversation_date",
                    "CREATE INDEX ix_group_checkins_conversation_date ON group_checkins (conversation_id, checkin_date)",
                ),
                (
                    "group_member_daily_activity",
                    "ix_group_member_daily_activity_conversation_date",
                    "CREATE INDEX ix_group_member_daily_activity_conversation_date ON group_member_daily_activity (conversation_id, activity_date)",
                ),
                (
                    "group_member_exp_logs",
                    "ix_group_member_exp_logs_conversation_user_created",
                    "CREATE INDEX ix_group_member_exp_logs_conversation_user_created ON group_member_exp_logs (conversation_id, user_id, created_at)",
                ),
                (
                    "message_visibilities",
                    "ix_message_visibilities_user_message",
                    "CREATE INDEX ix_message_visibilities_user_message ON message_visibilities (user_id, message_id)",
                ),
                (
                    "message_bookmarks",
                    "ix_message_bookmarks_user_created",
                    "CREATE INDEX ix_message_bookmarks_user_created ON message_bookmarks (user_id, created_at)",
                ),
                (
                    "message_reactions",
                    "ix_message_reactions_message_emoji",
                    "CREATE INDEX ix_message_reactions_message_emoji ON message_reactions (message_id, emoji)",
                ),
                (
                    "posts",
                    "ix_posts_author_created",
                    "CREATE INDEX ix_posts_author_created ON posts (author_id, created_at)",
                ),
                (
                    "posts",
                    "ix_posts_visibility_created",
                    "CREATE INDEX ix_posts_visibility_created ON posts (visibility, created_at)",
                ),
                (
                    "posts",
                    "ix_posts_repost_of_id",
                    "CREATE INDEX ix_posts_repost_of_id ON posts (repost_of_id)",
                ),
                (
                    "post_likes",
                    "ix_post_likes_post_created",
                    "CREATE INDEX ix_post_likes_post_created ON post_likes (post_id, created_at)",
                ),
                (
                    "post_comments",
                    "ix_post_comments_post_created",
                    "CREATE INDEX ix_post_comments_post_created ON post_comments (post_id, created_at)",
                ),
                (
                    "post_comment_likes",
                    "ix_post_comment_likes_comment_created",
                    "CREATE INDEX ix_post_comment_likes_comment_created ON post_comment_likes (comment_id, created_at)",
                ),
                (
                    "posts",
                    "ix_posts_author_pinned_created",
                    "CREATE INDEX ix_posts_author_pinned_created ON posts (author_id, pinned_at, created_at)",
                ),
                (
                    "post_notifications",
                    "ix_post_notifications_recipient_read_created",
                    "CREATE INDEX ix_post_notifications_recipient_read_created ON post_notifications (recipient_id, read_at, created_at)",
                ),
                (
                    "post_topic_links",
                    "ix_post_topic_links_topic_post",
                    "CREATE INDEX ix_post_topic_links_topic_post ON post_topic_links (topic_id, post_id)",
                ),
                (
                    "bot_reviews",
                    "ix_bot_reviews_bot_updated",
                    "CREATE INDEX ix_bot_reviews_bot_updated ON bot_reviews (bot_id, updated_at)",
                ),
                (
                    "bot_review_replies",
                    "ix_bot_review_replies_review_created",
                    "CREATE INDEX ix_bot_review_replies_review_created ON bot_review_replies (review_id, created_at)",
                ),
                (
                    "bot_review_likes",
                    "ix_bot_review_likes_review_created",
                    "CREATE INDEX ix_bot_review_likes_review_created ON bot_review_likes (review_id, created_at)",
                ),
                (
                    "bot_review_likes",
                    "ix_bot_review_likes_reply_created",
                    "CREATE INDEX ix_bot_review_likes_reply_created ON bot_review_likes (reply_id, created_at)",
                ),
                (
                    "post_mentions",
                    "ix_post_mentions_mentioned_created",
                    "CREATE INDEX ix_post_mentions_mentioned_created ON post_mentions (mentioned_user_id, created_at)",
                ),
                (
                    "ccw_binding_challenges",
                    "ix_ccw_binding_challenges_user_status",
                    "CREATE INDEX ix_ccw_binding_challenges_user_status ON ccw_binding_challenges (user_id, status)",
                ),
                (
                    "teamup_profiles",
                    "ix_teamup_profiles_event_status",
                    "CREATE INDEX ix_teamup_profiles_event_status ON teamup_profiles (event_id, status, moderation_status)",
                ),
                (
                    "teamup_profiles",
                    "ix_teamup_profiles_pinned_created",
                    "CREATE INDEX ix_teamup_profiles_pinned_created ON teamup_profiles (pinned_at, created_at)",
                ),
                (
                    "teamup_comments",
                    "ix_teamup_comments_profile_created",
                    "CREATE INDEX ix_teamup_comments_profile_created ON teamup_comments (profile_id, created_at)",
                ),
                (
                    "teamup_comment_likes",
                    "ix_teamup_comment_likes_comment_created",
                    "CREATE INDEX ix_teamup_comment_likes_comment_created ON teamup_comment_likes (comment_id, created_at)",
                ),
                (
                    "tasks",
                    "ix_tasks_conversation_status",
                    "CREATE INDEX ix_tasks_conversation_status ON tasks (conversation_id, status)",
                ),
                (
                    "tasks",
                    "ix_tasks_due_at",
                    "CREATE INDEX ix_tasks_due_at ON tasks (due_at)",
                ),
                (
                    "tasks",
                    "ix_tasks_parent_id",
                    "CREATE INDEX ix_tasks_parent_id ON tasks (parent_id)",
                ),
                (
                    "task_assignees",
                    "ix_task_assignees_user",
                    "CREATE INDEX ix_task_assignees_user ON task_assignees (user_id)",
                ),
                (
                    "task_watchers",
                    "ix_task_watchers_user",
                    "CREATE INDEX ix_task_watchers_user ON task_watchers (user_id)",
                ),
                (
                    "task_activities",
                    "ix_task_activities_task_created",
                    "CREATE INDEX ix_task_activities_task_created ON task_activities (task_id, created_at)",
                ),
                (
                    "task_comments",
                    "ix_task_comments_task_created",
                    "CREATE INDEX ix_task_comments_task_created ON task_comments (task_id, created_at)",
                ),
                (
                    "task_groups",
                    "ix_task_groups_owner",
                    "CREATE INDEX ix_task_groups_owner ON task_groups (owner_id)",
                ),
                (
                    "task_group_items",
                    "ix_task_group_items_owner",
                    "CREATE INDEX ix_task_group_items_owner ON task_group_items (owner_id)",
                ),
            ):
                if table_name not in table_names:
                    continue
                table_indexes = await conn.execute(text(f"PRAGMA index_list({table_name})"))
                table_index_names = {row[1] for row in table_indexes}
                if index_name not in table_index_names:
                    await conn.execute(text(index_sql))

            mention_indexes = await conn.execute(text("PRAGMA index_list(mention_all_usages)"))
            mention_index_names = {row[1] for row in mention_indexes}
            if "uq_mention_all_usage_user_date" not in mention_index_names:
                await conn.execute(
                    text(
                        "CREATE UNIQUE INDEX uq_mention_all_usage_user_date "
                        "ON mention_all_usages (user_id, usage_date)"
                    )
                )

            featured_indexes = await conn.execute(text("PRAGMA index_list(featured_messages)"))
            featured_index_names = {row[1] for row in featured_indexes}
            featured_columns = {row[1] for row in await conn.execute(text("PRAGMA table_info(featured_messages)"))}
            # 快照版的精华表（message_id 可空、没有外键）自带唯一约束，不需要下面这个旧库的补丁
            if "uq_featured_message" not in featured_index_names and "snap_type" not in featured_columns:
                duplicate_featured = await conn.execute(
                    text(
                        """
                        SELECT message_id
                        FROM featured_messages
                        GROUP BY message_id
                        HAVING COUNT(*) > 1
                        """
                    )
                )
                for (message_id,) in duplicate_featured.all():
                    await conn.execute(
                        text(
                            """
                            DELETE FROM featured_messages
                            WHERE message_id = :message_id
                              AND id NOT IN (
                                SELECT id
                                FROM featured_messages
                                WHERE message_id = :message_id
                                ORDER BY created_at DESC, id DESC
                                LIMIT 1
                              )
                            """
                        ),
                        {"message_id": message_id},
                    )
                await conn.execute(text("CREATE UNIQUE INDEX uq_featured_message ON featured_messages (message_id)"))

            # 商店（docs/shop.md）。新表 shop_items / shop_purchases / shop_accounts 由上面的 create_all 建，这里只补已有表的列。
            for table_name, column_name, column_sql in (("users", "cosmetics", "cosmetics TEXT"),):
                if table_name not in table_names:
                    continue
                columns = await conn.execute(text(f"PRAGMA table_info({table_name})"))
                if column_name not in {row[1] for row in columns}:
                    await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))

            # 资料点赞（docs/chat-features.md 第 6 节）。新表 profile_likes 由上面的 create_all 建，这里只补 users 上的累计列。
            for table_name, column_name, column_sql in (
                ("users", "profile_like_count", "profile_like_count INTEGER NOT NULL DEFAULT 0"),
            ):
                if table_name not in table_names:
                    continue
                columns = await conn.execute(text(f"PRAGMA table_info({table_name})"))
                if column_name not in {row[1] for row in columns}:
                    await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))

            # 限时商品、礼物、生日（docs/shop.md 2.5、docs/gifts.md、docs/chat-features.md 第 8 节）。
            # 新表 gifts / gift_records 由上面的 create_all 建，这里补已有表的列和索引。都是可空列或带默认值的列，不用回填。
            # 线上是两个 worker 一起启动，可能都看到列 / 索引不存在、都去加：后到的那个撞上 duplicate column name / already exists，
            # 当成「别人已经加好了」。稳妥起见上线前仍然先单进程跑一遍迁移。
            table_names = {row[0] for row in await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
            for table_name, column_name, column_sql in (
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
            ):
                if table_name not in table_names:
                    continue
                columns = await conn.execute(text(f"PRAGMA table_info({table_name})"))
                if column_name not in {row[1] for row in columns}:
                    try:
                        await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))
                    except Exception as exc:
                        if "duplicate column name" not in str(exc).lower():
                            raise
            # 索引。shop_purchases 的唯一索引换了条件（续期之后被接替的行不再算「当前拥有」）：先建新的再删旧的，
            # 中间任何时刻都至少有一个唯一索引管着，不会出现「一件商品两行当前记录」。
            for table_name, index_sql in (
                ("users", "CREATE INDEX IF NOT EXISTS ix_users_charm ON users (charm)"),
                ("shop_purchases", "CREATE INDEX IF NOT EXISTS ix_shop_purchases_expires ON shop_purchases (expires_at)"),
                (
                    "shop_purchases",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_shop_purchases_live ON shop_purchases (user_id, item_id) "
                    "WHERE revoked_at IS NULL AND superseded_at IS NULL",
                ),
                ("shop_purchases", "DROP INDEX IF EXISTS uq_shop_purchases_owner"),
            ):
                if table_name not in table_names:
                    continue
                try:
                    await conn.execute(text(index_sql))
                except Exception as exc:
                    if "already exists" not in str(exc).lower() and "no such index" not in str(exc).lower():
                        raise

            # 群升级、群文件、群待办（docs/group-plus.md）。新表 group_plus_records / group_files / group_todos 由上面的 create_all 建；
            # 精华表重建成快照版、聊天记录搜索索引和触发器由 create_all 的 after_create（services/schema_extras.py）负责。
            # 这里补 conversations / group_files 上的列：都是可空列或带默认值的列，不用回填。
            # 稳妥起见上线前先单进程启动一次（搜索索引的首次回填可能要几秒到几十秒）。
            table_names = {row[0] for row in await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
            for table_name, column_name, column_sql in (
                ("conversations", "plus_tier", "plus_tier VARCHAR(12) NOT NULL DEFAULT 'free'"),
                ("conversations", "plus_expires_at", "plus_expires_at DATETIME"),
                ("conversations", "plus_files_tier", "plus_files_tier VARCHAR(12)"),
                ("conversations", "plus_files_until", "plus_files_until DATETIME"),
                ("conversations", "file_default_ttl_hours", "file_default_ttl_hours INTEGER NOT NULL DEFAULT 24"),
                # 临时文件（空间放不下时传的 24 小时文件，不占配额）；老文件都是空间文件
                ("group_files", "temporary", "temporary BOOLEAN NOT NULL DEFAULT 0"),
            ):
                if table_name not in table_names:
                    continue
                columns = await conn.execute(text(f"PRAGMA table_info({table_name})"))
                if column_name not in {row[1] for row in columns}:
                    try:
                        await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))
                    except Exception as exc:
                        if "duplicate column name" not in str(exc).lower():
                            raise
            for index_sql in (
                "CREATE INDEX IF NOT EXISTS ix_conversations_plus_expires ON conversations (plus_expires_at)",
            ):
                await conn.execute(text(index_sql))

            # 好友申请「忽略」、通知只提醒一次（docs/chat-features.md 第 10 节）。两个可空列，不用回填：
            # 老申请都算没忽略；notifications_seen_at 为空就是「全部算新的」，用户第一次打开通知中心后就有值了。
            # 两个 worker 一起启动时后到的会撞上 duplicate column name，当成别人已经加好了
            for table_name, column_name, column_sql in (
                ("friend_requests", "ignored_at", "ignored_at DATETIME"),
                ("users", "notifications_seen_at", "notifications_seen_at DATETIME"),
            ):
                if table_name not in table_names:
                    continue
                columns = await conn.execute(text(f"PRAGMA table_info({table_name})"))
                if column_name not in {row[1] for row in columns}:
                    try:
                        await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))
                    except Exception as exc:
                        if "duplicate column name" not in str(exc).lower():
                            raise
