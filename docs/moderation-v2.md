# 审核 / 处罚 / 举报 / 风纪委员 接口契约

客户端与后台对接用。**第 1 节已实现**；第 2、3 节是定稿前的草案，实现后会更新本文件并标注。

所有路径前缀 `/api/v1`。时间一律 ISO 8601（UTC）。

## 1. 统一处罚（已实现）

### 1.1 规则

规则不要写死在客户端，从 `GET /moderation/rules`（无需登录）读取：

```jsonc
{
  "point_window_days": 90,
  "categories": [{ "key": "harassment", "label": "辱骂骚扰", "level": 2 }, ...],
  "levels":  [{ "level": 1, "points": 1, "penalty": "notice" },
              { "level": 2, "points": 3, "penalty": "warning" },
              { "level": 3, "points": 6, "penalty": "mute:24" },
              { "level": 4, "points": 12, "penalty": "ban:168" }],
  "ladder":  [{ "threshold": 6,  "penalty": "mute:24" },
              { "threshold": 9,  "penalty": "mute:72" },
              { "threshold": 12, "penalty": "ban:168" },
              { "threshold": 18, "penalty": "ban:720" },
              { "threshold": 24, "penalty": "ban:permanent" }]
}
```

处置编码：`none` / `notice` / `warning` / `mute:<小时>` / `ban:<小时>` / `ban:permanent`。

- 记分在 90 天滚动窗口内累计，过期自动不计。
- 累计分数**跨过**门槛时才触发阶梯处置；没跨过新门槛就只是该等级的基础处置。
- 机器和风纪委员给出的封禁最长 7 天；阶梯算出更重时先封 7 天，`under_review=true`，等管理员确认。
- 同一条内容只罚一次。
- 机器「拿不准」（review）的内容不处理、不记分，只进人工队列。

### 1.2 我的处罚状态

`GET /moderation/me` → `ModerationStanding`

```jsonc
{
  "points": 9,
  "point_window_days": 90,
  "muted_until": "2026-10-14T03:00:00+00:00",   // 未禁言为 null
  "mute_reason": "辱骂骚扰（L2，90 天内累计 9 分）：……",
  "next_threshold": 12,          // 已到顶为 null
  "next_penalty": "ban:168",
  "ban": null,                   // 被封时 { reason, banned_until, permanent }
  "violations": [ViolationRead]  // 最近 50 条，新的在前
}
```

`ViolationRead`：

| 字段 | 说明 |
| --- | --- |
| `id` | |
| `category` / `category_label` | 类别 key 与中文名 |
| `level` | 0–4；0 表示只处理内容不记分 |
| `points` / `points_total` | 本次记分 / 记完后的 90 天累计 |
| `source` | `auto` / `report` / `jury` / `admin` |
| `target_type` / `target_id` | `message` / `post` / `post_comment` / `post_topic` / `teamup_profile` / `teamup_comment` / `profile_cover`，可为 null |
| `conversation_id` | 仅消息有 |
| `summary` | 违规内容摘要（≤120 字），图片为 `[图片]` |
| `status` | `active` / `revoked` |
| `penalty_kind` | `none` / `notice` / `warning` / `mute` / `ban` |
| `penalty_ends_at` / `penalty_permanent` | 禁言 / 封禁到期时间；永久时前者为 null、后者为 true |
| `under_review` | 管理员还没确认最终处置 |
| `appeal_status` | `none` / `pending` / `accepted` / `rejected` |
| `can_appeal` | 能否申诉（生效中且没申诉过） |
| `created_at` / `expires_at` | 认定时间 / 记分过期时间 |

### 1.3 申诉

`POST /moderation/violations/{id}/appeal`，body `{ "text": "5–2000 字" }` → `ViolationRead`

- 每条处罚只能申诉一次，重复提交返回 409。
- 申诉通过后该记录 `status=revoked`，记分清零，由它触发的禁言 / 封禁同步解除。

### 1.4 被封禁时怎么申诉

被封的账号拿不到正常令牌。用密码登录（`POST /auth/login`、`POST /auth/cookie-login`）失败时，403 的 `detail` 里多一个 `appeal_token`：

```jsonc
{ "detail": { "code": "account_suspended", "message": "账号已被封禁", "reason": "...",
              "banned_until": "...", "permanent": false, "appeal_token": "<jwt>" } }
```

`appeal_token` 有效 30 分钟，**只能**用于 `GET /moderation/me` 和 `POST /moderation/violations/{id}/appeal`（照常放在 `Authorization: Bearer`）。其它接口一律 401。会话恢复（`GET /auth/session`）和 IP 登录的 403 不带这个字段。

### 1.5 禁言

禁言 = 能登录能看，不能发。到期自动恢复，无需任何操作。

- 状态来源：`GET /moderation/me` 的 `muted_until`，以及下面的实时事件。
- 被禁言时这些接口返回 **403**：发消息、转发、发动态、评论动态、保存组队名片、组队留言。

```jsonc
{ "detail": { "code": "account_muted", "message": "你已被禁言至 10月14日 11:00，暂时不能发布内容",
              "reason": "辱骂骚扰（L2，90 天内累计 9 分）", "muted_until": "2026-10-14T03:00:00+00:00" } }
```

输入框建议：`muted_until` 在未来时禁用并显示「禁言中，10月14日 11:00 解除」，到点本地解锁即可，不必再请求。

### 1.6 实时事件（用户通道 `/ws`）

| 事件 | data | 时机 |
| --- | --- | --- |
| `moderation.penalty` | `ViolationRead` | 被记了一次违规（`penalty_kind` 不为 `none`） |
| `moderation.violation.updated` | `{ action, violation: ViolationRead \| null, standing }` | `action` ∈ `revoked` / `reviewed` / `appeal_accepted` / `appeal_rejected` / `unmuted`。`standing` 与 `/moderation/me` 同结构但不含 `ban`、`violations` |
| `account.suspended` | 现有结构，新增 `violation_id` | 被封禁，随后连接断开 |

收到 `moderation.penalty` 且 `penalty_kind=mute` 时用 `penalty_ends_at` 更新本地禁言状态；收到 `moderation.violation.updated` 时用 `standing.muted_until` 覆盖。

### 1.7 通知中心

`GET /notifications` 的统一列表里已经带处罚通知，旧客户端无需改动就能显示：

```jsonc
{ "id": "violation:12", "category": "system", "type": "moderation_penalty",
  "title": "你的消息因「辱骂骚扰」被处理",
  "body": "违规等级 L2，记 3 分，90 天内累计 9 分。禁言 3 天。如有异议可以申诉。",
  "action": "none", "payload": { "violation": ViolationRead } }
```

新客户端可以识别 `type=moderation_penalty`，点开跳到处罚详情 / 申诉页。

### 1.8 后台接口（`/admin`）

| 接口 | 说明 |
| --- | --- |
| `GET /admin/violations?user_id&status&appeal_status&review_status&source&limit&offset` | 台账分页，返回 `{ items: AdminViolationRead[], total }` |
| `POST /admin/violations` | 手动记违规：`{ user_id, category, level?, penalty?, reason?, summary? }`；不给 `penalty` 时走阶梯 |
| `POST /admin/violations/{id}/revoke` | `{ reason }`，撤销并回滚由它触发的禁言 / 封禁 |
| `POST /admin/violations/{id}/review` | 复核被上限压住的处置：`{ action: "confirm" \| "adjust", penalty?, note? }` |
| `POST /admin/violations/{id}/appeal` | 处理申诉：`{ accept, note? }` |
| `POST /admin/users/{id}/unmute` | 直接解除禁言（记分保留） |

`penalty` 结构：`{ kind, hours?, permanent? }`。`AdminViolationRead` 在 `ViolationRead` 基础上多出：`user_id`、`user`、`raw_source`、`confidence`、`reason`、`moderation_record_id`、`report_id`、`case_id`、`actor`、`review_status`、`recommended_penalty`、`reviewed_at`、`appeal_text`、`appealed_at`、`appeal_note`、`appeal_reviewed_at`、`revoked_at`、`revoke_reason`。

`GET /admin/stats` 新增 `appeals_pending`、`penalty_reviews_pending`。`POST /admin/reports/{id}/review` 新增可选的 `violation_category`、`violation_level`：给了就按阶梯处罚；同时带 `disable_user` 时以管理员指定的封禁时长为准。原有的手动封禁 / 解封接口不变，但现在都会落到台账里。

后台通道 `/admin/ws` 新增事件 `admin.violation.changed`：`{ violation_id, user_id, action }`。

## 2. 举报案件（草案，未实现）

- 同一目标的举报合并为一个案件；同一人对同一目标只能举报一次（已举报返回 409，并带上已有案件的状态）。
- `POST /reports` 入参收紧：`reason` 改为固定枚举；选「其他」时 `details` 必填（≥10 字）；举报用户（`target_type=user`）必须带 `evidence_message_ids`（1–10 条）。
- 举报人能看到自己举报的处理结果，并收到通知。
- 举报信誉决定每日举报额度；恶意举报会扣信誉。

## 3. 风纪委员（草案，未实现）

- 资格：注册满 30 天、已绑定 CCW、90 天无违规、有基本活跃度；申请后管理员审批，任期 30 天可续。
- 只评审**被举报的群聊消息**和**公开内容**（动态、评论、组队、资料）。私聊举报只给管理员。
- 案件随机分配，隐去举报人与被举报人身份；回避本人、好友、自己所在的群。
- 投票选项：不违规 / L1 / L2 / L3 / 看不懂。3 票一致或 5 票中 4 票一致即生效，否则升级给管理员。
- 风纪委员裁决最高到封 7 天。
