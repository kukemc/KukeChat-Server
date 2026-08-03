from __future__ import annotations

import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.enums import UserStatus
from app.models.user import User
from app.services.users import list_ccw_creators


class CcwCreatorDirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_directory_filters_orders_and_paginates_creators(self) -> None:
        async with self.sessions() as db:
            db.add_all([
                User(username="top", hashed_password="x", ccw_student_oid="top-oid", ccw_follower_count=5600),
                User(username="qualified", hashed_password="x", ccw_student_oid="qualified-oid", ccw_follower_count=1001),
                User(username="threshold", hashed_password="x", ccw_student_oid="threshold-oid", ccw_follower_count=1000),
                User(username="small", hashed_password="x", ccw_student_oid="small-oid", ccw_follower_count=999),
                User(username="unbound", hashed_password="x", ccw_follower_count=9000),
                User(username="bot", hashed_password="x", ccw_student_oid="bot-oid", ccw_follower_count=8000, is_bot=True),
                User(username="disabled", hashed_password="x", ccw_student_oid="disabled-oid", ccw_follower_count=7000, status=UserStatus.DISABLED),
            ])
            await db.commit()

            first_page, total = await list_ccw_creators(db, limit=1, offset=0)
            second_page, second_total = await list_ccw_creators(db, limit=1, offset=1)

            self.assertEqual(total, 2)
            self.assertEqual(second_total, 2)
            self.assertEqual([user.username for user in first_page], ["top"])
            self.assertEqual([user.username for user in second_page], ["qualified"])


if __name__ == "__main__":
    unittest.main()
