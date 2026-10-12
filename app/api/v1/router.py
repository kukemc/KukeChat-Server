from fastapi import APIRouter

from app.api.v1 import admin, announcements, chat_extras, auth, bot_api, bots, ccw, conversations, friends, game, home, invites, jury, moderation, notifications, posts, reports, stickers, task, teamup, uploads, users


api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(home.router, prefix="/home", tags=["home"])
api_router.include_router(friends.router, prefix="/friends", tags=["friends"])
api_router.include_router(conversations.router, prefix="/conversations", tags=["conversations"])
api_router.include_router(chat_extras.router, prefix="/conversations", tags=["conversations"])
api_router.include_router(bots.router, prefix="/bots", tags=["bots"])
api_router.include_router(bot_api.router, prefix="/bot-api", tags=["bot-api"])
api_router.include_router(invites.router, prefix="/invites", tags=["invites"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
api_router.include_router(stickers.router, prefix="/stickers", tags=["stickers"])
api_router.include_router(posts.router, prefix="/posts", tags=["posts"])
api_router.include_router(uploads.router, prefix="/uploads", tags=["uploads"])
api_router.include_router(reports.router, prefix="/reports", tags=["reports"])
api_router.include_router(moderation.router, prefix="/moderation", tags=["moderation"])
api_router.include_router(jury.router, prefix="/jury", tags=["jury"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
# 风纪三期里资深委员的接口（资深合议 /jury/panel/...、申诉复审 /jury/appeals/...），和 jury.router 挂在同一个前缀下
from app.api.v1 import jury_senior  # noqa: E402

api_router.include_router(jury_senior.router, prefix="/jury", tags=["jury"])
api_router.include_router(ccw.router, prefix="/ccw", tags=["ccw"])
api_router.include_router(teamup.router, prefix="/teamup", tags=["teamup"])
api_router.include_router(task.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(announcements.router, prefix="/announcements", tags=["announcements"])
api_router.include_router(game.router, prefix="/game", tags=["game-mode"])

# 风纪 v2 三期：判例和议事厅（docs/jury-v2.md 3.4、3.5、3.8）。和 jury / admin 用同样的前缀再挂一次
from app.api.v1 import admin_jury_hall, jury_hall  # noqa: E402

api_router.include_router(jury_hall.router, prefix="/jury", tags=["jury"])
api_router.include_router(admin_jury_hall.router, prefix="/admin", tags=["admin"])
