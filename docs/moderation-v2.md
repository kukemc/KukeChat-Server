# 审核 / 处罚 / 举报 / 风纪委员 接口契约

客户端与后台对接用。三节均已在后端实现。

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

## 2. 举报案件（已实现）

同一个目标的所有举报并成一个**案件**，只审一次，结论回写给案件里的每条举报。

### 2.1 提交举报

表单的可选项从 `GET /reports/options`（无需登录）读取：

```jsonc
{ "reasons": [{ "reason": "辱骂骚扰", "details_required": false }, ...,
              { "reason": "其他", "details_required": true }],
  "details_min_length": 10, "max_evidence_messages": 10, "max_attachments": 6 }
```

`POST /reports`，body 在原有字段上新增 `evidence_message_ids`：

| 字段 | 说明 |
| --- | --- |
| `target_type` | `message` / `post` / `user` / `conversation` |
| `target_id` | |
| `reason` | 必须是 options 里的理由之一 |
| `details` | 选「其他」时必填，≥10 字 |
| `attachments` | 截图 URL，最多 6 张 |
| `conversation_id` | 举报用户时可选：说明是在哪个会话里 |
| `evidence_message_ids` | 举报用户时选中的证据消息，最多 10 条；必须是被举报人在 `conversation_id` 里发的、举报人自己看得到的消息 |

校验失败的返回（`detail` 是对象，`message` 可直接展示）：

| HTTP | `detail.code` | 含义 |
| --- | --- | --- |
| 422 | `invalid_reason` | 理由不在可选列表里（`detail.reasons` 给出列表） |
| 422 | `details_required` | 选了「其他」但说明不足 10 字 |
| 422 | `evidence_required` | 举报用户 / 群聊时既没选证据消息、也没截图、也没写说明 |
| 409 | `already_reported` | 自己已经举报过这个目标（`detail.case_id`） |
| 409 | `already_handled` | 这条消息已经被处理（撤回）了 |
| 400 | — | 举报自己 / 自己的消息 / 自己的动态 |
| 429 | `report_quota_exceeded` | 今天的举报次数用完 |
| 429 | `report_restricted` | 近期多次无效 / 恶意举报，暂时不能举报 |

成功返回 `ReportRead`，新增三个字段：`case_id`、`result_title`、`result_text`（未结案时后两个为 null）。

### 2.2 额度与结果

- `GET /reports/quota` → `{ score, daily_quota, used_today, remaining_today, upheld, dismissed, malicious, pending, window_days }`。额度按近 90 天举报的准确率分档：20 / 10 / 3 / 0 次每天，新用户 10 次。
- `GET /reports/me`：每条举报带 `status`（`pending` / `reviewed` 成立 / `rejected` 不成立）和 `result_title` / `result_text`。结果文案不透露对被举报人的具体处罚。
- 实时事件 `report.resolved`：`{ report_id, case_id, status, title, body }`。
- 通知中心同样会出现 `type="report_result"`、`category="system"` 的条目，旧客户端可直接显示。

### 2.3 案件怎么流转

1. 新案件先过机器预审（仅消息）：明确违规的当场撤回、处罚、结案。
2. 其余的：**群聊消息**和**公开动态**进风纪委员队列；私聊、举报用户、举报群聊只进管理员队列。
3. 风纪委员 72 小时没有结论、或投票没有共识的，转给管理员。

### 2.4 后台接口

| 接口 | 说明 |
| --- | --- |
| `GET /admin/cases?status=open&queue=&q=&limit&offset` | `status` ∈ `open` / `resolved` / `dismissed`；`queue` ∈ `triage` / `jury` / `admin`。返回 `{ items: CaseRead[], total, open_counts: { triage, jury, admin } }`，未结案件按优先级排序 |
| `GET /admin/cases/{id}` | `CaseDetail` = `CaseRead` + `snapshot`（证据快照）+ `reports`（案件内全部举报）+ `votes`（风纪委员已投的票）。读取会写审计日志 |
| `POST /admin/cases/{id}/resolve` | `{ outcome: "violation" \| "dismiss", category?, level?, delete_content?, penalty?, note?, malicious_reporter_ids? }` |
| `POST /admin/cases/{id}/queue` | `{ queue: "jury" \| "admin" }`，手动改派；涉及私聊的案件不能派给风纪委员 |

`CaseRead` 字段：`id`、`target_type`、`target_id`、`conversation_id`、`message_id`、`reported_user_id`、`reported_user`、`status`、`queue`、`jury_eligible`、`report_count`、`primary_reason`、`priority`、`summary`、`machine_decision`（`allow` / `review` / `block`）、`machine_category`、`machine_level`、`machine_confidence`、`verdict_source`（`auto` / `jury` / `admin`）、`verdict_category`、`verdict_category_label`、`verdict_level`、`verdict_note`、`violation_id`、`first_report_id`、`created_at`、`closed_at`。

- `outcome=violation`：`category` 缺省按举报理由映射，`level` 缺省取类别默认等级，`penalty` 缺省走阶梯；`delete_content` 默认 true（撤回消息 / 删除动态）。
- `outcome=dismiss`：可用 `malicious_reporter_ids` 标出恶意举报人，会降低他们的举报额度。
- 原有的 `POST /admin/reports/{id}/review` 仍可用：处理其中一条举报会让整个案件结案，同案的其它举报同步得到结论。

`GET /admin/stats` 新增 `cases_admin_pending`、`cases_jury_pending`、`jury_applications_pending`。后台通道新增事件 `admin.case.changed`：`{ case_id, queue, status, action }`。

## 3. 风纪委员（已实现）

### 3.1 资格与申请

`GET /jury/me` → `JuryStatus`

```jsonc
{
  "status": "none",            // none / applied / active / expired / rejected / suspended / retired
  "eligible": false,
  "requirements": [
    { "key": "account_age",  "ok": true,  "text": "注册满 30 天",             "detail": "已注册 86 天" },
    { "key": "ccw_bound",    "ok": true,  "text": "已绑定 CCW 账号",          "detail": "已绑定" },
    { "key": "clean_record", "ok": true,  "text": "近 90 天没有违规记录",      "detail": "无违规" },
    { "key": "activity",     "ok": false, "text": "近 30 天发言不少于 30 条", "detail": "已发言 12 条" }
  ],
  "term_ends_at": null,
  "review_note": null,         // 被拒绝 / 被暂停时的说明
  "stats": null,               // 当过委员后：{ votes_total, votes_settled, votes_agreed, agreement_rate, golden_total, golden_correct }
  "daily_limit": 30,
  "voted_today": 0
}
```

`POST /jury/apply`，body `{ "statement"?: "≤500 字" }` → `JuryStatus`

- 不满足条件返回 403 `jury_not_eligible`（`message` 里列出没满足的条件）。
- 首次申请进入 `applied`，等管理员审批；通过后任期 30 天。
- 任期届满（`status=expired`）后再调一次 `/jury/apply` 即可续任，条件仍满足就直接生效，不用再审批。
- 被暂停（`suspended`）的不能申请。

资格变化时用户通道会收到 `jury.status.changed`（data 为空对象），收到后重新拉 `/jury/me`。

### 3.2 评审

`GET /jury/cases/next` → `{ "case": JuryCaseView | null }`

- 一次只发一个案件。没投票前重复调用返回同一个；没有可评审的返回 `case: null`。
- 403 `not_juror`：不是在任委员；403 `juror_restricted`：受处罚期间；429 `jury_daily_limit`：今天已评 30 件。

`JuryCaseView`：

```jsonc
{
  "case_id": 42,
  "kind": "message",                       // message / post
  "reasons": [{ "reason": "辱骂骚扰", "count": 2 }],
  "report_count": 2,
  "messages": [                            // kind=message：被举报消息及其前 5 条、后 3 条
    { "speaker": "成员A",   "is_target": false, "type": "text",  "content": "大家好", "images": [], "created_at": "..." },
    { "speaker": "被举报人", "is_target": true,  "type": "text",  "content": "……",    "images": [], "created_at": "..." }
  ],
  "post": null                             // kind=post：{ content, image_urls, created_at }
}
```

内容已匿名：没有用户名、头像、ID，也没有群名。`type` 为 `image` 时 `content` 是图片地址；`content` 为 null 表示那条上下文消息已被撤回。**客户端不要尝试补全任何身份信息。**

`POST /jury/cases/{case_id}/vote`，body `{ "vote": "none" | "l1" | "l2" | "l3" | "abstain", "note"?: "≤500 字" }` → `{ case_id, settled }`

| vote | 含义 |
| --- | --- |
| `none` | 不违规 |
| `l1` / `l2` / `l3` | 违规，轻微 / 一般 / 严重 |
| `abstain` | 看不懂 / 拿不准，不计入表决 |

- 表决规则：看「违规 / 不违规」是否一致，3 票一致或 5 票里 4 票一致即生效；违规等级取违规票的较低中位数。5 票仍无共识转管理员。
- `settled=true` 表示这一票让案件有了结论。投完直接再取下一个即可。
- 案件里混有已有结论的质控题，外观上无法区分；准确率过低会被自动暂停资格。
- 回避规则由服务端保证：涉案人、举报人、被举报人的好友、案发群的成员都不会被分到这个案件。

`GET /jury/history` → 最近 50 条自己的投票：`{ case_id, kind, vote, voted_at, agreed, outcome }`。`outcome` ∈ `violation` / `clean` / null（未结）；`agreed` 为 null 表示未结或自己弃权。

### 3.3 后台接口

| 接口 | 说明 |
| --- | --- |
| `GET /admin/jury/members?status=&limit&offset` | `status` ∈ `applied` / `active` / `rejected` / `suspended` / `retired`。返回 `{ items: JuryMemberRead[], total }` |
| `POST /admin/jury/members/{user_id}/review` | `{ action: "approve" \| "reject" \| "suspend" \| "retire" \| "reinstate", note? }`。`approve` 也可以直接任命没申请过的用户 |

`JuryMemberRead`：`user_id`、`user`、`status`、`statement`、`applied_at`、`reviewed_at`、`review_note`、`term_ends_at`，以及 `votes_total`、`votes_settled`、`votes_agreed`、`agreement_rate`、`golden_total`、`golden_correct`。

后台通道新增事件 `admin.jury.changed`：`{ user_id }`。
