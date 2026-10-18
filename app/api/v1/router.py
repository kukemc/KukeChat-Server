from fastapi import APIRouter

from app.api.v1 import admin, announcements, chat_extras, auth, bot_api, bots, ccw, conversations, friends, game, home, invites, jury, link_preview, moderation, notifications, posts, reports, stickers, task, teamup, uploads, users


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
api_router.include_router(link_preview.router, prefix="/link-preview", tags=["link-preview"])
api_router.include_router(teamup.router, prefix="/teamup", tags=["teamup"])
api_router.include_router(task.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(announcements.router, prefix="/announcements", tags=["announcements"])
api_router.include_router(game.router, prefix="/game", tags=["game-mode"])

# 风纪 v2 三期：判例和议事厅（docs/jury-v2.md 3.4、3.5、3.8）。和 jury / admin 用同样的前缀再挂一次
from app.api.v1 import admin_jury_hall, jury_hall  # noqa: E402

api_router.include_router(jury_hall.router, prefix="/jury", tags=["jury"])
api_router.include_router(admin_jury_hall.router, prefix="/admin", tags=["admin"])

# 商店（docs/shop.md）：用户侧 /shop/...，后台 /admin/shop/...
from app.api.v1 import admin_shop, shop  # noqa: E402

api_router.include_router(shop.router, prefix="/shop", tags=["shop"])
api_router.include_router(admin_shop.router, prefix="/admin", tags=["admin"])

# 红包（docs/red-packets.md）：用户侧 /red-packets/...，后台 /admin/red-packets/...
from app.api.v1 import admin_red_packets, red_packets  # noqa: E402

api_router.include_router(red_packets.router, prefix="/red-packets", tags=["red-packets"])
api_router.include_router(admin_red_packets.router, prefix="/admin", tags=["admin"])

# 礼物（docs/gifts.md）：用户侧 /gifts/...、/users/{id}/gifts...，后台 /admin/gifts/...。生日（docs/chat-features.md 第 8 节）：/users/me/birthday
from app.api.v1 import admin_gifts, birthday, gifts  # noqa: E402

api_router.include_router(gifts.router, prefix="/gifts", tags=["gifts"])
api_router.include_router(gifts.users_router, prefix="/users", tags=["gifts"])
api_router.include_router(admin_gifts.router, prefix="/admin", tags=["admin"])
api_router.include_router(birthday.router, prefix="/users", tags=["users"])

# 账号等级（docs/account-level.md）。/users/{user_id}/level 那一条用单独的 router 再挂到 /users 下
from app.api.v1 import admin_levels, levels  # noqa: E402

api_router.include_router(levels.router, prefix="/levels", tags=["levels"])
api_router.include_router(levels.users_router, prefix="/users", tags=["levels"])
api_router.include_router(admin_levels.router, prefix="/admin", tags=["admin"])

# 好友亲密度与好友成就（docs/intimacy.md）：用户侧 /intimacy/...，后台 /admin/intimacy/...
from app.api.v1 import admin_intimacy, intimacy  # noqa: E402

api_router.include_router(intimacy.router, prefix="/intimacy", tags=["intimacy"])
api_router.include_router(admin_intimacy.router, prefix="/admin", tags=["admin"])

# 群升级、群文件、群待办、聊天记录搜索（docs/group-plus.md）。conversation_router 和 conversations 用同一个前缀
from app.api.v1 import admin_group_plus, group_plus  # noqa: E402

api_router.include_router(group_plus.plans_router, prefix="/group-plus", tags=["group-plus"])
api_router.include_router(group_plus.conversation_router, prefix="/conversations", tags=["group-plus"])
api_router.include_router(group_plus.search_router, prefix="/search", tags=["search"])
api_router.include_router(admin_group_plus.router, prefix="/admin", tags=["admin"])

# 发版服务（docs/releases.md）：公开的 /releases/latest、/releases，后台 /admin/releases/...
from app.api.v1 import releases  # noqa: E402

api_router.include_router(releases.public_router, prefix="/releases", tags=["releases"])
api_router.include_router(releases.admin_router, prefix="/admin", tags=["admin"])

# 群公告 v2（docs/group-announcements.md）：/conversations/{id}/announcements/...
from app.api.v1 import group_announcements  # noqa: E402

api_router.include_router(group_announcements.router, prefix="/conversations", tags=["conversations"])
