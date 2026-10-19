# 我的电脑 / 我的手机（自己设备之间的私密会话）

像 QQ 的「我的电脑」：每个人有且只有一个**只有自己**的会话，用来在自己的几台设备之间发消息、传文件。
它是**同一个会话**，只是各端显示的名字不同：桌面端 / 网页显示「我的手机」，手机上显示「我的电脑」（以后平板显示「我的 Pad」）。

所有路径前缀 `/api/v1`，都要求登录（和别的接口一样带 `Authorization` 和客户端证明头）。错误的 `detail` 大多是 `{ code, message }`，`message` 是可以直接展示的中文；少数沿用老接口的错误是字符串（见第 4、5.6 节）。时间一律 ISO 8601（UTC）。

本文件是 PC（React / Tauri）和安卓（Kotlin / Compose）客户端开发的依据。实现后改契约会同步改这里。

## 1. 会话

### 1.1 `GET /conversations/self`

返回我的「我的设备」会话，没有就建（幂等：怎么调、几台设备同时调，都只有一个）。返回普通的 `ConversationRead`（和 `GET /conversations` 列表里的元素同一个结构），其中：

```jsonc
{ "id": 1234,
  "type": "self",                 // 新的会话类型；老的只有 direct / group
  "title": null,
  "display_title": "我的设备",     // 服务端给的中性名字。客户端不要直接显示它，按 1.3 自己起名
  "avatar_url": null,
  "direct_user": null,            // 没有「对方」
  "member_count": 1,              // 只有自己
  "my_role": "member",
  "join_mode": "invite_only",
  "unread_count": 0,              // 恒为 0，见 3.3
  "last_message": { /* MessageRead，和别的会话一样 */ },
  "my_pinned": false, "my_do_not_disturb": false, "my_message_setting": "notify",
  "is_friend": false, "is_temporary": false, "plus_tier": "free",
  // 其余字段和私聊一样取默认值
  "updated_at": "2026-10-20T08:00:00Z" }
```

### 1.2 会话列表

- `GET /conversations` 的结果里**一定有**这个会话（`type = "self"`，每人恰好一个）：第一次列的时候服务端顺手建好。所以客户端一般不用专门调 `GET /conversations/self`，从列表里按 `type == "self"` 找就行；进会话页之前没拿到列表、或者列表是本地缓存的老数据时，再调一次 `GET /conversations/self`。
- 排序和别的会话一样（置顶在前，然后按 `updated_at` 新的在前），服务端不额外置顶。客户端可以自己决定放哪（比如固定放在列表顶部、或者放进「联系人」页的入口），这里不约束。
- 置顶、免打扰、清空聊天记录（`PATCH /conversations/{id}/members/me/settings`、`POST /conversations/{id}/clear`）照常能用。
- 会话 id 每个人不同，不要写死；同一个人在所有设备上是同一个 id。

### 1.3 客户端怎么起名、怎么画头像

| 当前客户端 | 列表 / 标题栏显示 | 头像 |
| --- | --- | --- |
| 桌面端（`X-Kuke-Platform: desktop`）、网页（`web`） | 我的手机 | 手机图标（本地画，不用 `avatar_url`） |
| 安卓（`android`）、iOS（`ios`） | 我的电脑 | 电脑图标 |
| （以后）平板 | 我的 Pad | 平板图标 |

不显示「对方在线状态」「对方资料」「好友火花」这类私聊才有的东西；会话设置页只留置顶、免打扰、查找聊天记录、清空聊天记录、临时文件列表（5.2 的 `GET .../transfers`）。

## 2. 哪台设备发的：`metadata.device`

「我的设备」会话里的**每一条**消息（文字、图片、语音、表情、合并转发、临时文件）都带 `metadata.device`：

| 值 | 含义 | 设备类 |
| --- | --- | --- |
| `desktop` | 桌面端（Tauri） | 电脑 |
| `web` | 网页 | 电脑 |
| `android` | 安卓 | 手机 |
| `ios` | iOS | 手机 |

- 由服务端按**发送请求**的 `X-Kuke-Platform` 头写入（就是各端每个请求本来就带的那个，见 `device-bans.md`）。没带这个头时按 `X-Kuke-Client` 里有没有 `android` 猜，再猜不出算 `web`；不在上表里的值也算 `web`。客户端在 `metadata` 里自己带的 `device` 会被覆盖，不用带。
- 临时文件消息的 `device` 是**开单**（`POST .../transfers`）那个请求的设备。
- 只有「我的设备」会话里的消息有这个字段；别的会话里的消息没有。

**左右怎么放**：消息的 `sender_id` 永远是自己，不能再按「是不是我发的」决定左右。改成按设备类：

- `metadata.device` 的设备类和当前客户端的设备类**相同** → 右边（「这类设备发的」）。
- 不同 → 左边（「另一类设备发的」），左边的头像用对方设备类的图标（电脑 / 手机），不用自己的头像，名字显示「我的电脑」/「我的手机」。
- 没有 `metadata.device` 的（理论上不会有；比如以后加了别的写入路径）→ 当成右边。

当前客户端的设备类：桌面端 / 网页 = 电脑，安卓 / iOS = 手机（和自己请求头里的 `X-Kuke-Platform` 对应）。

例子：在手机上发的消息，手机上显示在右边，电脑上显示在左边、头像是手机图标、名字「我的手机」。

## 3. 消息

### 3.1 能用的

和私聊完全一样，用原来的接口，不需要改：

- 发消息 `POST /conversations/{id}/messages`：文字、图片（`type: "image"`）、语音（`type: "voice"`）、表情（图片 + `metadata.sticker: true`）、引用回复（`metadata.reply_to`，和别的会话同一个形状）、作品卡片 / 链接预览。`client_nonce` 乐观发送照常（`chat-features.md` 第 11 节）。
- 历史 `GET /conversations/{id}/messages`、上下文 `.../messages/{mid}/context`、会话内搜索 `.../messages/search`、全局搜索 `GET /search/messages`（结果里会话摘要的 `display_title` 是「我的设备」，客户端按 1.3 换名字）。
- 撤回（自己的消息随时能撤，和私聊里撤自己的消息规则一样）、删除（仅自己可见）、表情回应、已读、语音转文字。
- 收藏（`.../bookmark`）：普通消息可以收藏；**临时文件消息不行**（5.5）。
- 合并转发 `POST /conversations/{target}/forward-bundle`：
  - **转进**「我的设备」（从别的会话把消息转给自己）：可以，这是主要用法之一。生成的那条合并转发消息照样带 `metadata.device`。
  - 从「我的设备」**转出**去：普通消息可以，来源标题（`metadata.source_title`）是「我的设备」；选中的消息里有临时文件 → 整个请求 422 `transfer_not_forwardable`。

### 3.2 实时

和别的会话一样推 `message.created` / `message.recalled` / `message.reaction.updated` / `conversation.updated` / `conversation.read`。会话成员只有自己，所以这些事件推给**自己所有在线的连接**，**包括发出这条消息的那台设备自己**：

- 发送端：POST 的返回和自己收到的 `message.created` 都会到，按 `client_nonce` 找到本地那条替换，另一份按 `id` 去重（和群聊里自己发的消息一样，客户端已经这么做了）。
- 另一台设备：收到 `message.created` 就插进会话，`metadata.device` 决定左右。
- 「正在输入」（`conversation.typing`）不会推（只推给会话里的其他人，这里没有其他人）。

### 3.3 未读和通知

- 未读、已读是按**人**记的（不是按设备），而所有消息都是自己发的，所以 `unread_count` 恒为 0、`mention_pending` 恒为 `null`。
- 客户端如果想在另一台设备上提示「有新消息」：自己在本地记——收到 `message.created` 且 `conversation.type == "self"` 且 `metadata.device` 的设备类和自己不同，就在本地给这个会话加一个未读角标 / 弹通知；打开会话后清掉。服务端不提供跨设备的未读数。
- 一般会话的通知逻辑是「不是我发的才通知」，对这个会话要改成上面这条规则，否则另一台设备发来的东西永远不提示。

## 4. 不支持的功能

「我的设备」里只有自己，下面这些一律不支持。统一错误：**400**，`detail = { "code": "self_chat_unsupported", "message": "「我的设备」里不能……" }`（`message` 可以直接展示）。客户端应该直接把入口藏掉，不要等报错。

| 功能 | 接口 | 说明 |
| --- | --- | --- |
| @ 某人 / @全体成员 | 发消息时 `metadata.mentions` 非空或 `mention_all: true` | 输入框里不要弹 @ 选人 |
| 投票 | `POST /conversations/{id}/polls` | |
| 红包 | `POST /red-packets` | |
| 礼物 | `POST /gifts`（带这个会话的 `conversation_id`） | |
| 精华 | `POST .../messages/{mid}/feature` | |
| 举报 | `POST /reports`（`target_type` 是 `message` 且消息在这个会话里，或者 `conversation` 且就是这个会话） | 消息菜单里不要有「举报」 |
| 加人 / 进群 / 申请进群 / 群设置 / 群公告 / 群资料 / 改角色 / 禁言 / 踢人 / 解散 / 机器人 | 各自的群接口 | |
| 退出会话 | `DELETE /conversations/{id}/members/me` | 「我的设备」不能退出、不能删除，只能清空聊天记录 |

另外（不是 `self_chat_unsupported`，沿用原来的错误）：

- 群文件（`/conversations/{id}/files...`）：400 `not_group`。要传文件用第 5 节的临时文件。
- 群待办、群签到、群排行、群升级：和私聊一样，返回原来的「只有群才有」的错误。
- 戳一戳：只能戳别人，这里没有别人（400 `Cannot poke yourself`）。
- 任务：这个会话没开任务功能（403）。
- **别人**访问我的「我的设备」会话的任何接口：和访问不在里面的私聊一样，404。

机器审核：「我的设备」里的消息**不送**内容审核服务，不落审核记录，也不会进二审、空闲复查、机器案件、风纪委员队列（只有自己看得到）。后台也看不到它的内容（后台本来就不能翻私聊；这里连举报入口都没有）。自己给自己发的消息也**不算**账号等级的活跃度。

## 5. 临时文件

在「我的设备」里可以发**任意类型**的文件（安装包、压缩包、视频……），但它们是**临时**的：

- **24 小时**后过期（从 complete 那一刻算），过期后不能下载（410），服务端把存储里的文件删掉。
- 不能收藏、不能转发（5.5）。
- 单个文件上限 `max_file_bytes`（服务器配置 `SELF_TRANSFER_MAX_BYTES`，默认 1 GB）；每人同时存着的（还没过期的，加上开了单还没传完的）文件总量上限 `quota_bytes`（`SELF_TRANSFER_QUOTA_BYTES`，默认 5 GB）。两个值都从 `GET .../transfers` 读，不要写死。
- 只有自己能下载。

### 5.1 流程

```
1. POST /conversations/{cid}/transfers            开单：{ name, size, content_type? } → { transfer_id, transfer, upload }
2. PUT  upload.url                                 把文件本身作为请求体传上去（带 upload.headers）
3. POST /conversations/{cid}/transfers/{tid}/complete   → { transfer, message }，会话里多一条文件消息（message.created）
4. 任意一台设备：GET .../transfers/{tid}/link（直连）或 .../download（经后端）下载
```

第 2 步有两种，看 `upload.direct`：

- `direct = true`（线上：服务器配了对象存储私有桶）：`upload.url` 是对象存储的**预签名 PUT 地址**（15 分钟有效）。
  - 请求体就是文件字节，**原样带上 `upload.headers`**（目前是 `{ "Content-Type": "application/octet-stream" }`），不能多也不能改。
  - **不要**带 `Authorization`、客户端证明头、cookie。签名里固定了 `Content-Length = size`。
  - 成功一般返回 200（对象存储的响应，空 body）。
- `direct = false`（本地开发 / 没配私有桶）：`upload.url` 是后端路径 `/api/v1/conversations/{cid}/transfers/{tid}/content`（相对路径，拼上 API 的 origin），方法 PUT。
  - 和别的 API 请求一样带 `Authorization` 和客户端证明头（`X-Kuke-Client` / `X-Kuke-Timestamp` / `X-Kuke-Nonce` / `X-Kuke-Proof`，证明里的 path 就是 `/conversations/{cid}/transfers/{tid}/content`，方法 `PUT`），再加 `upload.headers`。
  - 请求体是文件字节（不是 multipart）。返回 200 `{ "transfer_id": 7, "uploaded_bytes": 1048576 }`。
  - 可以重传（覆盖上一次的），complete 之前随时可以再 PUT。

开单之后 **1 小时**内要 complete（`transfer.upload_expires_at`），过了上传单作废（404 `upload_not_found`，传上去的东西也会被删），只能重新开单。直传的 PUT 地址 15 分钟就过期，传得慢超过 15 分钟的话重新开单。

进度条：用 HTTP 库的上传进度回调（OkHttp 的 RequestBody / Tauri 的 fetch 进度）。服务端不提供进度查询。

### 5.2 接口

| 接口 | 说明 |
| --- | --- |
| `GET /conversations/{cid}/transfers` | 还能下载的文件（`ready` 且没过期，新的在前，最多 100 个）和额度：`{ items: [SelfTransferRead], max_file_bytes, quota_bytes, used_bytes, free_bytes, ttl_hours: 24, direct }`。`used_bytes` 算的是没过期的文件加上没作废的上传单 |
| `POST /conversations/{cid}/transfers` | 开单。body `{ "name": "安装包.apk", "size": 31457280, "content_type": "application/vnd.android.package-archive" }`（`content_type` 可省略，也可以写成 `mime`；只用来显示图标）。201 → `SelfTransferCreated` |
| `PUT /conversations/{cid}/transfers/{tid}/content` | 只在 `upload.direct = false` 时用，见 5.1 |
| `POST /conversations/{cid}/transfers/{tid}/complete` | body 可省略，或 `{ "client_nonce": "local-123" }`（乐观发送，同发普通消息）。首次 201 → `{ transfer, message }`；已经 complete 过的再调一次 200，原样返回那条消息，不会多发一条（网络断了重试是安全的） |
| `GET /conversations/{cid}/transfers/{tid}` | `SelfTransferRead`，看状态用 |
| `GET /conversations/{cid}/transfers/{tid}/link` | 直连下载：`{ "url": "https://…", "expires_at": "…" }`，5 分钟有效的预签名 GET，响应头被签成 `application/octet-stream` + `Content-Disposition: attachment; filename="…"; filename*=UTF-8''…`。不用带任何鉴权头，可以直接交给浏览器 / 系统下载器。`direct = false` 时 409 `direct_download_unavailable`，改用 `/download` |
| `GET /conversations/{cid}/transfers/{tid}/download` | 带鉴权下载（`Authorization` 头），`attachment`，`application/octet-stream`。直传开着时 302 到预签名地址（HTTP 库跟随跳转时会丢掉 `Authorization`，正常）；否则后端直接发文件 |

`cid` 必须是**我的**「我的设备」会话：不是成员 404；是成员但不是 `type = self` 的会话 400 `not_self_conversation`。`tid` 不存在 / 不是我的 / 不在这个会话里：404 `transfer_not_found`。

```jsonc
// SelfTransferCreated（POST /transfers 的 201）
{ "transfer_id": 7,
  "transfer": { /* SelfTransferRead，state = "pending" */ },
  "upload": { "direct": true,
              "method": "PUT",
              "url": "https://cn-nb1.example.com/kukechat-file/self-transfers/42/9f…?X-Amz-…",
              "headers": { "Content-Type": "application/octet-stream" },
              "expires_at": "2026-10-20T08:15:00Z" } }   // url 的过期时间（direct = false 时是上传单的截止时间）

// SelfTransferRead
{ "id": 7,
  "conversation_id": 1234,
  "name": "安装包.apk",              // 清洗过的文件名：去掉路径、控制字符，最长 120 字（保留扩展名），空的叫 file
  "size": 31457280,
  "content_type": "application/vnd.android.package-archive",   // 上传时声明的；不合法的变成 application/octet-stream
  "device": "android",              // 开单的设备
  "state": "ready",                 // pending 还没 complete / ready 能下载 / expired 过期了或被撤回了
  "expired": false,                 // state == "expired"
  "created_at": "…",
  "upload_expires_at": "…",         // pending 时：在这之前要 complete
  "completed_at": "…",              // pending 时 null
  "expires_at": "2026-10-21T08:00:00Z",   // complete 时间 + 24 小时；pending 时 null
  "message_id": 901,                // 落下的那条文件消息；pending 时 null
  "download_path": "/api/v1/conversations/1234/transfers/7/download" }
```

### 5.3 文件消息

complete 时在会话里落一条**普通文本消息**（`type = "text"`，没有新增消息类型——老客户端会直接显示这行字），走正常的发消息路径：推 `message.created`、更新会话预览、受每分钟 20 条的发消息限频约束。

```jsonc
{ "type": "text",
  "content": "[文件] 安装包.apk（30.0 MB）",
  "metadata": {
    "device": "android",
    "transfer": { "id": 7,
                  "name": "安装包.apk",
                  "size": 31457280,
                  "content_type": "application/vnd.android.package-archive",
                  "expires_at": "2026-10-21T08:00:00+00:00",
                  "expired": false } } }     // 服务端每次返回消息时现场算的（expires_at <= 现在）
```

- 新客户端看到 `metadata.transfer` 就画文件卡片：文件名、大小、图标（按扩展名 / `content_type`）、「24 小时内有效」/ 剩余时间、下载按钮；用 `transfer.id` 调 5.2 的下载接口。
- **过期**：卡片显示「已过期」、下载按钮置灰。判断用 `expired`，或者客户端自己拿 `expires_at` 和本机时间比（不用等服务端事件——过期时服务端**不推**任何事件）。点下载时拿到 410 `file_expired` 也按已过期处理。
- 和群文件的文件消息（`metadata.file`，见 `group-plus.md` 5.2）是两种东西：键名不同、下载接口不同，不要混用。
- 不能通过普通发消息接口伪造：`POST /conversations/{id}/messages` 的 `metadata` 里带 `transfer` 键，422 `transfer_not_forwardable`。

### 5.4 过期、撤回、清理

- 到了 `expires_at`：下载 410 `file_expired`（`message`: 「文件已过期」），`GET .../transfers` 里不再列出，`SelfTransferRead.state = "expired"`。服务端每 10 分钟删一次存储里过期的对象（不影响上面的判断——按时间判，不看对象删没删）。过期 7 天后连记录也删掉，再查是 404 `transfer_not_found`（客户端按消息里的 `expires_at` 早就知道过期了）。
- **撤回**这条文件消息：文件立刻作废（存储里马上删掉），之后下载 410 `file_expired`。所以「我的设备」里撤回文件消息 = 删除文件。
- 「删除消息（仅自己可见）」「清空聊天记录」只是不显示，文件照样存到 24 小时。
- 会话消息超过 1000 条时老消息会被修剪（和私聊一样），文件照样存到 24 小时，`GET .../transfers` 里还能看到、能下载。

### 5.5 不能收藏、不能转发

临时文件 24 小时后就没了，所以：

- 收藏：`POST .../messages/{mid}/bookmark` → 422 `transfer_not_savable`（「临时文件不能收藏」）。
- 合并转发里有它：422 `transfer_not_forwardable`（「临时文件不能转发」）。
- 逐条转发（客户端把 `content` / `metadata` 原样发到别的会话）：422 `transfer_not_forwardable`。
- 精华：整个会话都不支持（第 4 节）。

客户端在文件消息的长按 / 右键菜单里去掉「收藏」「转发」「多选转发」，多选时不让选文件消息。想把文件留下来就下载到本地。

### 5.6 错误

| HTTP | `detail.code` | 什么时候 |
| --- | --- | --- |
| 404 | —（字符串 `Conversation not found`） | 会话不存在 / 不是我的 |
| 400 | `not_self_conversation` | 在别的会话上调 `/transfers` |
| 404 | `transfer_not_found` | 文件不存在 / 不是我的 / 过期超过 7 天被清掉了 |
| 422 | `empty_file` | 开单：`size <= 0` |
| 413 | `file_too_large` | 开单：超过单个文件上限，带 `max_file_bytes` |
| 413 | `transfer_quota_exceeded` | 开单：加上这个就超过了总量上限，带 `quota_bytes`、`used_bytes`、`size`。等之前的过期或者撤回之前的文件消息再发 |
| 403 | `account_muted` | 被平台禁言了，开单和 complete 都不行（带 `muted_until`）。普通消息同样发不了 |
| 422 | `upload_size_mismatch` | `/content`：传上来的字节数和开单时的 `size` 不一致（不保存，上传单保留，可以重传）；complete：存储里的对象大小不对（对象删掉，上传单保留，重新 PUT 再 complete）。带 `size`、`uploaded_size` |
| 409 | `direct_upload_required` | `direct = true` 时调了 `/content`：直接传到 `upload.url` |
| 409 | `already_completed` | 已经 complete 过的又调 `/content` |
| 404 | `upload_not_found` | 上传单作废了（开单超过 1 小时没 complete），重新开单 |
| 409 | `upload_incomplete` | complete：存储里还没有这个对象（还没传完），传完再 complete；下载：还没 complete |
| 429 | `rate_limited` | complete 撞上了发消息限频（每分钟 20 条）：**上传单和文件都保留**，过一会儿再 complete 就行 |
| 410 | `file_expired` | 下载 / 拿链接：文件已过期（或被撤回了） |
| 409 | `direct_download_unavailable` | `/link`：服务器没开直连，改用 `/download` |
| 422 | `transfer_not_savable` | 收藏临时文件消息 |
| 422 | `transfer_not_forwardable` | 转发临时文件消息 / 发消息时伪造 `metadata.transfer` |
| 400 | `self_chat_unsupported` | 第 4 节那些功能 |

## 6. 实时事件一览

| 事件 | 什么时候 | 备注 |
| --- | --- | --- |
| `message.created` | 发了消息 / complete 了临时文件 | 推给自己所有在线连接（包括发送端），`data` 是 `MessageRead`，带 `metadata.device`、可能带 `client_nonce` |
| `message.recalled` | 撤回 | 文件消息撤回后文件作废，不另推事件 |
| `message.reaction.updated` / `conversation.updated` / `conversation.read` | 和私聊一样 | |

没有新增事件。临时文件过期不推事件（客户端按 `expires_at` 自己算）。

## 7. 客户端清单

两端都要做的：

1. 会话列表认 `type == "self"`：按 1.3 显示名字和图标，不显示对方资料 / 在线状态 / 火花。老逻辑里「不是 group 就当私聊、去读 `direct_user`」的地方要先判断 self（`direct_user` 是 null）。
2. 聊天页：左右按第 2 节的设备类规则；左边的消息头像 / 名字用对方设备类。
3. 输入框：没有 @、红包、礼物、投票、戳一戳入口；多一个「文件」入口（任意类型），走第 5 节流程，显示上传进度，complete 前本地先插一条「发送中」的文件卡片（`client_nonce`）。
4. 文件卡片：`metadata.transfer`；下载优先 `/link`（409 再退到 `/download`）；过期置灰；菜单里没有收藏 / 转发 / 举报。
5. 会话设置：只留置顶、免打扰、查找聊天记录、清空聊天记录、「临时文件」（`GET .../transfers`，显示剩余有效期、额度）。
6. 通知 / 本地未读：按 3.3。
7. 错误提示：`detail.message` 直接显示。

## 8. 服务端实现要点（不影响契约）

- **表**：`conversations` 新增列 `self_owner_id`（主人的用户 id，别的会话为空）+ 唯一索引 `uq_conversations_self_owner`，并发建会话时靠它只留一个（`services/self_chat.get_or_create` 撞了唯一约束就回头读）；`self_transfers`（临时文件，pending → ready → expired，不建外键，会话 / 用户删了 worker 照样能删对象）。启动时自动迁移。
- **存储**：对象放 `self-transfers/{用户 id}/{随机名}`。和 `group-files/` 一样是私有前缀：配了 `S3_PRIVATE_BUCKET` 时放私有桶、走预签名直传直下；本地存储时 `/uploads/self-transfers/...` 直链 404。
- **清理**：`services/self_chat.sweep`，每 10 分钟一次：作废的上传单删行删对象、过期的文件标 expired 删对象、过期 7 天的行删掉。可以重复跑，多个 worker 同时跑也安全（改状态的语句带着原状态做条件；删对象幂等）。
- **配置**：`SELF_TRANSFER_MAX_BYTES`（默认 1 GB）、`SELF_TRANSFER_QUOTA_BYTES`（默认 5 GB），见 `.env.example`。
- **审核**：发消息的接口看到 `_self_chat` 标记就不调度审核；`moderation._moderate_message_task` 里也按会话类型再挡一次（举报复核等别的入口也进不来）。空闲复查、风纪、机器案件本来就只看群聊。
- **后台**：`GET /admin/conversations` 默认不列 `type = self`（每人一个，会把列表淹掉）；要看传 `type=self`，只有元数据，没有消息内容。

## 9. 老客户端

- 老客户端会在会话列表里看到一个 `type = "self"`、`display_title = "我的设备"`、没有 `direct_user` 的会话，按私聊画（没有头像、名字「我的设备」），能正常收发文字消息，消息都显示在右边。
- 文件消息在老客户端上是一行字「[文件] xxx（大小）」，点不开。
