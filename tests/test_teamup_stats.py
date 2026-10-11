from __future__ import annotations

import unittest
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.teamup import TeamupComment, TeamupEvent, TeamupProfile
from app.models.user import User
from app.services import teamup as teamup_service

STATS_TABLES = [Base.metadata.tables[name] for name in ("users", "teamup_events", "teamup_profiles", "teamup_comments")]


class TeamupStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=STATS_TABLES))

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_overall_stats_do_not_depend_on_an_active_event(self) -> None:
        async with self.sessions() as db:
            users = [User(username=f"u{index}", hashed_password="x") for index in range(6)]
            old_event = TeamupEvent(title="上一季", is_active=False)
            db.add_all([*users, old_event])
            await db.flush()

            def profile(user: User, **overrides) -> TeamupProfile:
                values = {"user_id": user.id, "moderation_status": "approved", "status": "recruiting", **overrides}
                return TeamupProfile(**values)

            profiles = [
                profile(users[0], view_count=10),
                profile(users[1], view_count=5, status="teamed"),
                # 上一季留下来的名片：广场现在不展示，但算进全站合计
                profile(users[2], view_count=100, event_id=old_event.id),
                # 下面三张都不算：待审、已删除、大小写不同的状态值仍算通过
                profile(users[3], view_count=1000, moderation_status="pending"),
                profile(users[4], view_count=1000, deleted_at=datetime.now(UTC)),
                profile(users[5], view_count=1, moderation_status="APPROVED"),
            ]
            db.add_all(profiles)
            await db.flush()
            db.add_all([
                TeamupComment(profile_id=profiles[0].id, author_id=users[1].id, content="好", moderation_status="approved"),
                TeamupComment(profile_id=profiles[2].id, author_id=users[1].id, content="好", moderation_status="approved"),
                TeamupComment(profile_id=profiles[0].id, author_id=users[1].id, content="待审", moderation_status="pending"),
                TeamupComment(profile_id=profiles[0].id, author_id=users[1].id, content="删了", moderation_status="approved", deleted_at=datetime.now(UTC)),
                TeamupComment(profile_id=profiles[3].id, author_id=users[1].id, content="名片没过审", moderation_status="approved"),
            ])
            await db.commit()

            self.assertIsNone(await teamup_service.get_active_event_id(db))
            stats = await teamup_service.overall_stats(db, None)
            self.assertEqual((stats.profile_count, stats.recruiting_count), (3, 2))
            self.assertEqual((stats.total_profile_count, stats.total_recruiting_count), (4, 3))
            self.assertEqual((stats.total_view_count, stats.total_comment_count), (116, 2))
            self.assertEqual(
                (stats.total_profile_count, stats.total_recruiting_count), await teamup_service._all_profile_counts(db)
            )

            in_event = await teamup_service.overall_stats(db, old_event.id)
            self.assertEqual((in_event.profile_count, in_event.recruiting_count, in_event.total_profile_count), (1, 1, 4))


if __name__ == "__main__":
    unittest.main()
