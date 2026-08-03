from __future__ import annotations

import unittest
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models.conversation import Conversation, ConversationMember
from app.models.enums import ConversationType, MemberRole, PostVisibility
from app.models.friend import Friendship
from app.models.message import Message
from app.models.post import Post, PostLike
from app.models.user import User
from app.services.home import get_home


class HomeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_home_returns_real_stats_public_groups_and_creators(self) -> None:
        async with self.sessions() as db:
            viewer = User(username="viewer", hashed_password="x")
            friend = User(username="friend", hashed_password="x")
            creator = User(username="creator", hashed_password="x", ccw_student_oid="creator-oid", ccw_follower_count=3000)
            bot = User(username="bot", hashed_password="x", is_bot=True)
            db.add_all([viewer, friend, creator, bot])
            await db.flush()
            public_group = Conversation(type=ConversationType.GROUP, title="Public", join_mode="open", updated_at=datetime.now(UTC))
            private_group = Conversation(type=ConversationType.GROUP, title="Private", join_mode="invite_only")
            db.add_all([public_group, private_group])
            await db.flush()
            db.add_all([
                ConversationMember(conversation_id=public_group.id, user_id=friend.id, role=MemberRole.OWNER),
                Message(conversation_id=public_group.id, sender_id=friend.id, content="hello"),
                Friendship(user_id=viewer.id, friend_id=friend.id),
                Friendship(user_id=friend.id, friend_id=viewer.id),
            ])
            post = Post(author_id=friend.id, content="public", visibility=PostVisibility.PUBLIC, moderation_status="approved")
            db.add(post)
            await db.flush()
            db.add(PostLike(post_id=post.id, user_id=viewer.id))
            await db.commit()

            first = await get_home(db, viewer.id, seed=42)
            second = await get_home(db, viewer.id, seed=42)

            self.assertEqual(first.stats.registered_users, 3)
            self.assertEqual(first.stats.messages, 1)
            self.assertEqual(first.stats.messages_last_7_days, 1)
            self.assertEqual(first.stats.groups, 2)
            self.assertEqual(first.stats.public_groups, 1)
            self.assertEqual(first.stats.public_posts, 1)
            self.assertEqual(first.stats.friendships, 1)
            self.assertEqual(first.stats.post_likes, 1)
            self.assertEqual([group.id for group in first.groups], [public_group.id])
            self.assertGreater(first.groups[0].heat_score, 0)
            self.assertEqual([item.id for item in first.creators], [creator.id])
            self.assertEqual([group.id for group in first.groups], [group.id for group in second.groups])


if __name__ == "__main__":
    unittest.main()
