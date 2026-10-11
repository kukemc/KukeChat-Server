from fastapi import APIRouter

from app.api.v1 import admin, announcements, auth, bot_api, bots, ccw, conversations, friends, game, home, invites, jury, moderation, notifications, posts, reports, stickers, task, teamup, uploads, users


api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(home.router, prefix="/home", tags=["home"])
api_router.include_router(friends.router, prefix="/friends", tags=["friends"])
api_router.include_router(conversations.router, prefix="/conversations", tags=["conversations"])
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
api_router.include_router(ccw.router, prefix="/ccw", tags=["ccw"])
api_router.include_router(teamup.router, prefix="/teamup", tags=["teamup"])
api_router.include_router(task.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(announcements.router, prefix="/announcements", tags=["announcements"])
api_router.include_router(game.router, prefix="/game", tags=["game-mode"])
