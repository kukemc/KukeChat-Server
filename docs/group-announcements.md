# 群公告（v2）

路径前缀 `/api/v1/conversations/{conversation_id}`。带 `code` 的错误，`detail` 是 `{ code, message }`，`message` 是可直接展示的中文。
只对群聊有效（私聊返回 400）；不是群成员返回 404。

权限：群主 / 管理员发布、修改、删除、看确认统计；普通成员只能看和确认。

## 1. 公告对象 `GroupAnnouncementRead`

```jsonc
{
  "id": 7,
  "conversation_id": 3,
  "author_id": 12,
  "author": { /* UserPublic */ },       // 发布人已注销时为 null
  "author_display_name": "群主小明",     // 发布人在这个群的群昵称，没有就是昵称 / 用户名
  "content": "正文，1–600 字，纯文本，保留换行",
  "images": [                            // 最多 9 张，按顺序
    { "url": "https://…/messages/ab12.jpg", "thumbnail_url": "https://…/messages/thumbs/ab12.jpg" }
  ],
  "pinned": true,
  "pinned_at": "2026-10-18T08:00:00Z",   // 没置顶为 null
  "popup": true,                         // 使用弹窗展示
  "require_confirm": true,               // 需群成员确认收到
  "send_to_new_members": false,          // 发送给新成员（全群最多一条为 true）
  "guide_nickname": false,               // 引导群成员修改群昵称
  "created_at": "…",
  "updated_at": "…",
  "edited_at": null,                     // 正文 / 图片 / 选项被修改过的时间；没改过为 null
  "confirmed_count": 5,                  // 确认收到的现任成员人数（require_confirm 为 false 时也照算，一般是 0）
  "member_count": 40,                    // 现任成员人数
  "my_seen": true,                       // 我看过（弹窗关掉 / 打开过详情）
  "my_confirmed": false                  // 我确认收到过
}
```

列表顺序：置顶的在前（按 `pinned_at` 从新到旧），其余按发布时间从新到旧。置顶可以有多条。

## 2. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/announcements` | 公告列表，`GroupAnnouncementRead[]` |
| GET | `/announcements/popups` | 我在这个群里该弹出的公告（见第 4 节），`GroupAnnouncementRead[]`，按发布时间从旧到新 |
| GET | `/announcements/{id}` | 详情。已删除返回 404 `announcement_not_found` |
| POST | `/announcements` | 发布，返回 201 + `GroupAnnouncementRead`，同时在群里发一张公告卡片 |
| PATCH | `/announcements/{id}` | 修改，返回 `GroupAnnouncementRead`，同时再发一张「公告已更新」卡片 |
| DELETE | `/announcements/{id}` | 删除，返回 `{ "detail": "Announcement deleted" }`；旧卡片变成「该公告已删除」 |
| POST | `/announcements/{id}/seen` | 标记我看过，返回 `{ "announcement_id", "my_seen": true, "my_confirmed" }` |
| POST | `/announcements/{id}/confirm` | 确认收到，返回 `GroupAnnouncementRead`。公告没开 `require_confirm` 时 409 `confirm_not_required` |
| GET | `/announcements/{id}/confirmations?status=confirmed\|unconfirmed&limit=50&offset=0` | 确认统计（仅管理员），见第 3 节 |

### 2.1 发布 / 修改的请求体

```jsonc
{
  "content": "正文",              // 去掉首尾空白后 1–600 字
  "images": ["https://…/messages/ab12.jpg"],   // 已上传图片的 url（POST /uploads/message-image 或 /uploads/announcement-image），最多 9 张
  "pinned": false,
  "popup": false,
  "require_confirm": false,
  "send_to_new_members": false,
  "guide_nickname": false
}
```

- POST：除 `content` 外都可以省略，省略按 false / []。
- PATCH：**每个字段都可以省略，省略表示不改**（老客户端只传 `content` + `pinned`，不会把图片和选项冲掉）。
- `send_to_new_members: true` 会把这个群其它公告的这一项自动关掉。
- 置顶：从未置顶改成置顶时记 `pinned_at = 现在`；已经置顶的再传 true 不刷新时间。
- 正文和图片发布前同步过内容审核（审核服务不可用时放行）：正文违规 422 `announcement_rejected`，图片违规 422 `announcement_image_rejected`。
- 图片不是本站上传的图：422 `invalid_announcement_image`；超过 9 张：422 `too_many_images`。

### 2.2 错误码

| 状态 | code | 场景 |
| --- | --- | --- |
| 403 | `announcement_forbidden` | 普通成员发布 / 修改 / 删除 / 看确认统计 |
| 404 | `announcement_not_found` | 公告不存在或已删除 |
| 409 | `confirm_not_required` | 公告不需要确认 |
| 422 | `announcement_rejected` / `announcement_image_rejected` / `invalid_announcement_image` / `too_many_images` | 见上 |

## 3. 确认统计

```jsonc
{
  "announcement_id": 7,
  "status": "unconfirmed",
  "member_count": 40,          // 现任成员人数
  "confirmed_count": 5,
  "unconfirmed_count": 35,
  "items": [
    { "user": { /* UserPublic */ }, "nickname": "群昵称或 null", "role": "member", "confirmed_at": null }
  ],
  "limit": 50,
  "offset": 0,
  "has_more": false
}
```

只统计现任成员。已确认按确认时间从新到旧；未确认按入群时间。

## 4. 弹窗规则（`/announcements/popups`）

一条公告对我「该弹」，满足下面任一条，而且我还没处理过它：

- `popup = true`，而且我在开启弹窗的那一刻之前就已经在群里；
- `send_to_new_members = true`，而且我是在开启这一项之后入群的。

「处理过」：公告要求确认的，以 `/confirm` 为准；不要求确认的，以 `/seen` 为准。

客户端在进入群聊时调一次；弹窗关掉时调 `/seen`，点「确认收到」调 `/confirm`。收到 `group.announcement.created/updated` 事件且 `popup` 为 true、当前正打开这个群时，可以重新拉一次。

## 5. 公告卡片消息

发布、修改时，服务端以操作人的身份在群里发一条 `type = "system"` 的消息。`content` 是给旧客户端、会话列表预览看的纯文本，例如 `小明 发布了群公告：本周六晚上八点…`。

`metadata`：

```jsonc
{
  "system_event": "group.announcement.card",
  "announcement_id": 7,
  "action": "created",            // created 发布 / updated 修改（卡片上标「公告已更新」）
  "summary": "正文前 200 字",       // 客户端自己按行数截断
  "image_url": "https://…",        // 第一张图，没有为 null
  "image_thumbnail_url": "https://…",
  "image_count": 2,
  "author_id": 12,
  "author_name": "小明",
  "require_confirm": true,
  "deleted": true                  // 只有公告被删除后才出现
}
```

- 是系统消息，所以和其它系统消息一样不进合并转发、不计入发言频率。客户端也不要给它提供「转发」入口。
- 点卡片打开公告详情；`deleted` 为 true 时卡片显示「该公告已删除」，不可点。
- 删除公告时服务端会把这条公告的所有卡片标上 `deleted: true`（之后拉历史就是删除态）；已经在内存里的卡片，客户端收到 `group.announcement.deleted` 时自己改。

## 6. WebSocket 事件（发给在线的群成员）

| 事件 | data |
| --- | --- |
| `group.announcement.created` | `GroupAnnouncementRead`（`my_seen` / `my_confirmed` 恒为 false，客户端保留本地值） |
| `group.announcement.updated` | 同上 |
| `group.announcement.deleted` | `{ conversation_id, announcement_id, card_message_ids }`（`card_message_ids` 是被标成已删除的卡片消息 id） |
| `group.announcement.confirmations` | `{ conversation_id, announcement_id, confirmed_count, member_count }`，只发给在线的群主 / 管理员和确认的人 |

卡片消息本身走普通的 `message.created`。

## 7. 存储

- `group_announcements` 新增列：`images_json`、`pinned_at`、`popup`、`popup_since`、`require_confirm`、`send_to_new_members`、`new_members_since`、`guide_nickname`、`edited_at`（`app/core/database.py` 的 `_ADDED_COLUMNS`）。
- 新表 `group_announcement_receipts`：`(announcement_id, user_id)` 唯一，`seen_at`、`confirmed_at`。公告删除时级联删除。
- `conversations.announcement`（旧的单条公告文本）继续跟着最新一条公告走，给旧客户端用。
