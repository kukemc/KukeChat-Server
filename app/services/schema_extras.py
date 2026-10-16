"""SQLite 上 ORM 表达不了的那部分结构（docs/group-plus.md 第 8、10 节）：

1. 精华表升级成「永久快照」版：老库里的 featured_messages 带外键（消息被修剪就级联删掉精华），这里重建并回填快照。
2. 聊天记录搜索索引：message_search_index（普通表，一条可搜消息一行）+ message_search_fts（FTS5 外部内容表，trigram 分词）。
   用触发器随 messages 的新增 / 修改 / 撤回 / 删除（含修剪、级联）同步；首次建表时回填。
3. 撤回时清掉精华 / 群待办的快照内容（触发器，所有撤回路径都覆盖）。

全部幂等：每次 create_all 之后都会跑一遍（models/group_plus.py 的 after_create），已经有的不动。
FTS5 / trigram 不可用时只建普通索引表，搜索退回 LIKE（services/message_search.py 自己判断）。
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

# 进索引的消息类型（messages.type 存的是枚举名）
INDEXED_TYPES = "('TEXT','IMAGE','FORWARD_BUNDLE','POLL')"


def _category_sql(row: str) -> str:
    meta = f"{row}.metadata"
    return f"""CASE
        WHEN {row}.type = 'IMAGE' THEN CASE WHEN json_valid({meta}) AND json_extract({meta}, '$.sticker') = 1 THEN 'sticker' ELSE 'media' END
        WHEN {row}.type = 'FORWARD_BUNDLE' THEN 'bundle'
        WHEN {row}.type = 'POLL' THEN 'poll'
        WHEN json_valid({meta}) AND json_type({meta}, '$.file') = 'object' THEN 'file'
        WHEN {row}.content LIKE '%http://%' OR {row}.content LIKE '%https://%' OR {row}.content LIKE '%www.%' THEN 'link'
        ELSE 'text' END"""


def _text_sql(row: str) -> str:
    meta = f"{row}.metadata"
    return f"""CASE
        WHEN {row}.type = 'IMAGE' THEN ''
        WHEN {row}.type = 'FORWARD_BUNDLE' THEN {row}.content || coalesce(
            (SELECT ' ' || group_concat(CASE WHEN j.type = 'object' THEN json_extract(j.value, '$.content') END, ' ')
               FROM json_each(CASE WHEN json_valid({meta}) THEN {meta} ELSE '{{}}' END, '$.items') j), '')
        ELSE {row}.content END"""


def _index_select(row: str) -> str:
    return f"SELECT {row}.id, {row}.conversation_id, {row}.sender_id, {row}.created_at, {_category_sql(row)}, {_text_sql(row)}"


_COLUMNS = "(message_id, conversation_id, sender_id, created_at, category, text)"


def _exists(connection: Connection, name: str) -> bool:
    return connection.execute(text("SELECT 1 FROM sqlite_master WHERE name = :n"), {"n": name}).first() is not None


def _migrate_featured(connection: Connection) -> None:
    """老库的 featured_messages（message_id 非空、带外键）→ 快照版。已经是新结构就什么都不做。"""
    columns = {row[1] for row in connection.execute(text("PRAGMA table_info(featured_messages)"))}
    if not columns or "snap_type" in columns:
        return
    connection.execute(text("ALTER TABLE featured_messages RENAME TO featured_messages_legacy"))
    # 索引不会跟着表改名，名字还占着：先删掉，新表要建同名的
    for (name,) in connection.execute(
        text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'featured_messages_legacy' AND sql IS NOT NULL")
    ).all():
        connection.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
    from app.models.message import FeaturedMessage

    FeaturedMessage.__table__.create(connection)
    connection.execute(
        text(
            """
            INSERT INTO featured_messages (
                id, message_id, conversation_id, set_by_id, created_at,
                snap_type, snap_content, snap_metadata, snap_sender_id, snap_sender_username, snap_sender_name,
                snap_sender_avatar, snap_sender_title, snap_created_at, snap_recalled_at, snapshot_missing
            )
            SELECT f.id, f.message_id, f.conversation_id, f.set_by_id, f.created_at,
                   COALESCE(m.type, 'TEXT'),
                   CASE WHEN m.recalled_at IS NULL THEN COALESCE(m.content, '') ELSE '' END,
                   CASE WHEN m.recalled_at IS NULL THEN m.metadata ELSE NULL END,
                   m.sender_id, u.username,
                   COALESCE(NULLIF(cm.nickname, ''), NULLIF(u.nickname, ''), u.username),
                   u.avatar_url, cm.title, m.created_at, m.recalled_at,
                   CASE WHEN m.id IS NULL THEN 1 ELSE 0 END
            FROM featured_messages_legacy f
            LEFT JOIN messages m ON m.id = f.message_id
            LEFT JOIN users u ON u.id = m.sender_id
            LEFT JOIN conversation_members cm ON cm.conversation_id = f.conversation_id AND cm.user_id = m.sender_id
            """
        )
    )
    connection.execute(text("DROP TABLE featured_messages_legacy"))


def _install_search_index(connection: Connection) -> None:
    index_is_new = not _exists(connection, "message_search_index")
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS message_search_index (
                message_id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                category VARCHAR(12) NOT NULL,
                text TEXT NOT NULL
            )
            """
        )
    )
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_message_search_conversation ON message_search_index (conversation_id, message_id)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_message_search_sender ON message_search_index (sender_id, message_id)"))
    if index_is_new:
        # 首次：回填现有的、没被撤回的消息。FTS 的触发器还没建，等下面一次性 rebuild
        select = f"{_index_select('m')} FROM messages m WHERE m.type IN {INDEXED_TYPES} AND m.recalled_at IS NULL"
        connection.execute(text(f"INSERT OR IGNORE INTO message_search_index {_COLUMNS} {select}"))

    fts_ready = False
    try:
        fts_is_new = not _exists(connection, "message_search_fts")
        connection.execute(
            text(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS message_search_fts USING fts5(
                    text, content='message_search_index', content_rowid='message_id', tokenize='trigram'
                )
                """
            )
        )
        if fts_is_new:
            connection.execute(text("INSERT INTO message_search_fts(message_search_fts) VALUES ('rebuild')"))
        fts_ready = True
    except Exception as exc:  # FTS5 或 trigram 分词器不可用：退回 LIKE
        logger.warning("FTS5 trigram index unavailable, message search falls back to LIKE: %s", exc)

    for name in ("trg_message_search_fts_ai", "trg_message_search_fts_ad", "trg_message_search_fts_au"):
        connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
    if fts_ready:
        connection.execute(
            text(
                """
                CREATE TRIGGER trg_message_search_fts_ai AFTER INSERT ON message_search_index BEGIN
                    INSERT INTO message_search_fts(rowid, text) VALUES (new.message_id, new.text);
                END
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TRIGGER trg_message_search_fts_ad AFTER DELETE ON message_search_index BEGIN
                    INSERT INTO message_search_fts(message_search_fts, rowid, text) VALUES ('delete', old.message_id, old.text);
                END
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TRIGGER trg_message_search_fts_au AFTER UPDATE ON message_search_index BEGIN
                    INSERT INTO message_search_fts(message_search_fts, rowid, text) VALUES ('delete', old.message_id, old.text);
                    INSERT INTO message_search_fts(rowid, text) VALUES (new.message_id, new.text);
                END
                """
            )
        )

    for name in ("trg_message_search_ai", "trg_message_search_au", "trg_message_search_ad", "trg_message_recall_snapshots"):
        connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
    insert_new = (
        f"INSERT OR REPLACE INTO message_search_index {_COLUMNS} {_index_select('new')} "
        f"WHERE new.type IN {INDEXED_TYPES} AND new.recalled_at IS NULL"
    )
    connection.execute(text(f"CREATE TRIGGER trg_message_search_ai AFTER INSERT ON messages BEGIN {insert_new}; END"))
    connection.execute(
        text(
            "CREATE TRIGGER trg_message_search_au AFTER UPDATE OF type, content, metadata, recalled_at, created_at, sender_id ON messages BEGIN "
            f"DELETE FROM message_search_index WHERE message_id = old.id; {insert_new}; END"
        )
    )
    connection.execute(
        text("CREATE TRIGGER trg_message_search_ad AFTER DELETE ON messages BEGIN DELETE FROM message_search_index WHERE message_id = old.id; END")
    )
    # 撤回：精华 / 群待办的快照内容清掉，只留撤回时间（所有撤回路径：用户、群管、机器审核、案件结案、后台）
    connection.execute(
        text(
            """
            CREATE TRIGGER trg_message_recall_snapshots AFTER UPDATE OF recalled_at ON messages
            WHEN NEW.recalled_at IS NOT NULL AND OLD.recalled_at IS NULL
            BEGIN
                UPDATE featured_messages SET snap_recalled_at = NEW.recalled_at, snap_content = '', snap_metadata = NULL
                    WHERE message_id = NEW.id;
                UPDATE group_todos SET snap_recalled_at = NEW.recalled_at, snap_content = '', snap_metadata = NULL
                    WHERE message_id = NEW.id;
            END
            """
        )
    )


def install_sqlite_extras(connection: Connection) -> None:
    # 有的测试只建一部分表：缺了触发器要引用的表就什么都不做（完整的库一定都在）
    if not all(_exists(connection, name) for name in ("messages", "featured_messages", "group_todos")):
        return
    _migrate_featured(connection)
    _install_search_index(connection)
