# 投票、正在输入、群表情

路径前缀 `/api/v1`。带 `code` 的错误，`detail` 是 `{ code, message }`，`message` 是可直接展示的中文。

## 1. 投票

投票是一条 `type = "poll"` 的消息，群聊和私聊都能发。消息正文 `content` 是纯文本摘要（`[投票] 标题` 加编号选项），给不认识这个类型的旧客户端、搜索和通知预览用。

### 1.1 消息里的投票对象

`message.metadata.poll`：

```jsonc
{
  "id": 12,                       // 投票 id，后面的接口都用它
  "title": "周末做什么",
  "options": [
    { "id": 1, "text": "写游戏", "votes": 3 },
    { "id": 2, "text": "画画",   "votes": 1 }
  ],
  "multiple": false,              // 是否多选
  "anonymous": false,             // 是否匿名
  "closes_at": null,              // 截止时间（ISO，UTC）；null 表示手动结束前一直有效
  "closed": false,                // 已手动结束，或已过截止时间
  "closed_at": null,
  "total_voters": 4,              // 参与人数（多选时不等于票数之和）
  "my_option_ids": [1]            // 我投了哪些
}
```

- 消息列表、单条消息、`message.created` 事件里都带这个对象。`message.created` 是发给全会话的同一份数据，其中 `my_option_ids` 恒为空、票数恒为 0（刚创建）。
- 到了 `closes_at` 服务端不会推事件。客户端自己按 `closes_at` 把投票显示为已结束即可；之后任何读取都会返回 `closed: true`。
- 消息被撤回（包括被审核撤回）= 投票作废，之后的投票 / 结束 / 查看都返回 409 `poll_void`。

### 1.2 接口

| 接口 | 说明 |
| --- | --- |
| `POST /conversations/{cid}/polls` | 发起投票，返回 `MessageRead`（和发普通消息一样，其它成员收到 `message.created`） |
| `GET /conversations/{cid}/polls/{poll_id}` | 当前状态 `PollState` |
| `PUT /conversations/{cid}/polls/{poll_id}/vote` | 投票 / 改票 / 撤票，返回 `PollState` |
| `POST /conversations/{cid}/polls/{poll_id}/close` | 提前结束，返回 `PollState` |
| `GET /conversations/{cid}/polls/{poll_id}/options/{option_id}/voters?limit=50&offset=0` | 某个选项的投票人 `{ items: UserPublic[], total }`；`limit` 最大 100 |

发起：

```jsonc
{ "title": "周末做什么", "options": ["写游戏", "画画"], "multiple": false, "anonymous": false, "closes_at": null }
```

- 标题 1–80 字；2–10 个选项，每个 1–40 字，不能重复（首尾空格会被去掉）；`closes_at` 必须在一分钟之后、30 天之内。
- 不能通过普通的发消息接口发 `type = "poll"`（422）。

投票：`{ "option_ids": [1] }`。提交的是**完整的选项集合**，会替换之前的选择；`[]` 表示撤票。截止前可以随便改。

`PollState`：

```jsonc
{ "conversation_id": 5, "message_id": 901, "poll_id": 12,
  "options": [{ "id": 1, "votes": 3 }, { "id": 2, "votes": 1 }],
  "total_voters": 4, "closed": false, "closed_at": null, "my_option_ids": [1] }
```

权限：

- 投票、查看：会话成员。**被禁言的人不能发起投票，但可以投票**（投票不算发布内容）。
- 提前结束：发起人；群聊里群主和管理员也可以。私聊里只有发起人。
- 查看投票人：非匿名投票的会话成员。

错误：

| HTTP | code | 含义 |
| --- | --- | --- |
| 422 | `invalid_poll` | 标题 / 选项 / 截止时间不合规（`message` 说明具体哪条） |
| 422 | `poll_rejected` | 标题或选项未通过文本审核 |
| 403 | `account_muted` | 被禁言，不能发起投票 |
| 422 | `invalid_poll_option` | 选项 id 不存在 |
| 422 | `poll_single_choice` | 单选投票提交了多个选项 |
| 409 | `poll_closed` | 投票已结束 |
| 409 | `poll_void` | 投票消息已被撤回 |
| 403 | `poll_forbidden` | 无权结束这个投票 |
| 403 | `poll_anonymous` | 匿名投票不能查看投票人 |
| 404 | — | 投票不存在，或你不是会话成员 |

### 1.3 实时事件

`poll.updated`，发给会话的在线成员：

```jsonc
{ "conversation_id": 5, "message_id": 901, "poll_id": 12,
  "options": [{ "id": 1, "votes": 3 }, { "id": 2, "votes": 1 }],
  "total_voters": 4, "closed": false, "closed_at": null }
```

- 只有汇总，没有 `my_option_ids`（自己的选择以投票接口的返回为准，不要用事件覆盖）。按 `message_id` 修补缓存里的 `metadata.poll`。
- 票数变化是合并推送的：同一个投票一秒内不管来多少票只推一次最新汇总。提前结束会立刻推一次。

## 2. 正在输入

客户端在已有的用户 WebSocket（`/ws`）上发一条 JSON 文本帧：

```json
{ "type": "typing", "conversation_id": 5 }
```

服务端转发给这个会话的其他在线成员，事件 `conversation.typing`：

```jsonc
{ "conversation_id": 5, "user_id": 42, "name": "小明" }   // name：群昵称 > 昵称 > 用户名
```

- 没有「停止输入」事件。客户端输入时每 3 秒左右发一次；收到后显示约 5 秒，没有新的就自己隐藏。
- 服务端兜底限速：同一个人在同一个会话里 2 秒内的重复上报直接丢弃。不落库，不回任何应答；格式不对的帧被忽略。
- **超过 200 人的群不转发**：大群里「谁在输入」没有意义，还会变成对全群的持续广播。私聊和 200 人以内的群正常工作。
- 不会发给把这个会话设为屏蔽（`message_setting = "ignore"`）的成员，也不会发给自己。
- 服务端不知道谁把会话开在前台，所以是发给所有在线成员，客户端只在对应会话可见时展示即可。

隐私开关：`PATCH /users/me` 新增 `share_typing: boolean`（默认 `true`），`GET /auth/me` 等返回的用户对象（`UserRead`）里带同名字段。关掉后别人看不到「我正在输入」；自己仍然能看到别人的。

## 3. 群表情

每个群一套带名字的表情图片。群主和管理员管理，全体成员可用。

| 接口 | 权限 | 说明 |
| --- | --- | --- |
| `GET /conversations/{cid}/emojis` | 群成员 | `GroupEmojiRead[]`，按添加顺序 |
| `POST /conversations/{cid}/emojis` | 群主 / 管理员 | `{ name, image_url }` → `GroupEmojiRead` |
| `PATCH /conversations/{cid}/emojis/{emoji_id}` | 群主 / 管理员 | `{ name }` → `GroupEmojiRead` |
| `DELETE /conversations/{cid}/emojis/{emoji_id}` | 群主 / 管理员 | |

```jsonc
// GroupEmojiRead
{ "id": 3, "conversation_id": 5, "name": "狗头", "url": "https://…/stickers/xxxx.jpg",
  "content_type": "image/jpeg", "created_by_id": 42, "created_at": "…" }
```

上传分两步：先用现有的 `POST /uploads/message-image` 上传图片，再把返回的地址作为 `image_url` 提交。服务端会把图片另存一份（和收藏表情同一套机制），之后与原图无关。

- 名称 1–12 字，同群不重复，空格会被去掉。
- 每个群最多 60 个；单张最终不超过 512KB；png / jpg / webp / gif。**非 GIF 图片会被转成 JPEG**（透明底会变成不透明），GIF 保留动画原样保存 —— 这是上传链路现有的行为。
- 图片会过图片审核，明确违规的拒绝。
- 没有排序接口，顺序就是添加顺序。
- 只做群聊；私聊调用返回 400。

错误：

| HTTP | code | 含义 |
| --- | --- | --- |
| 403 | `emoji_forbidden` | 不是群主 / 管理员 |
| 422 | `invalid_emoji_name` | 名称长度不对 |
| 409 | `emoji_name_taken` | 同群已有同名表情 |
| 409 | `emoji_limit_reached` | 已达 60 个上限 |
| 422 | `invalid_emoji_image` | `image_url` 不是已上传的图片 |
| 413 | `emoji_too_large` | 超过 512KB |
| 422 | `emoji_rejected` | 图片未通过审核 |

实时事件 `conversation.emojis.updated`：`{ "conversation_id": 5 }`，新增 / 改名 / 删除后发给群里的在线成员，收到后重拉列表。

使用：发一条现有的图片消息，`type = "image"`，`content` 填表情的 `url`，`metadata.sticker = true`（和收藏表情完全一样）。`metadata` 里可以再带 `group_emoji_id` 等来源字段，服务端原样保存和下发，不做校验。删除群表情不影响已经发出去的消息。
