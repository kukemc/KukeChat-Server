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
- **申诉预审**（后台用，客户端无变化）：机器给文字的处罚（`source` 为 `auto_rule` / `auto_ai`、内容是消息）被申诉时，后台任务
  结合上下文（前 5 条、后 3 条，同 1.10 的匿名规则）问一次二审，违规概率写在 `AdminViolationRead.appeal_judge_p`；
  低于 `MODERATION_APPEAL_LOW_P`（默认 0.3）的 `appeal_possible_error=true`，后台显示「二审：违规概率 0.08，可能误判」。
  `GET /admin/violations?appeal_status=pending` 按概率从低到高排（没有预审结果的排最后）。**只排先后，不会自动通过或驳回**，
  也不影响资深复审。二审不可用、后台开关 `machine_judge` 关着时没有这个概率，申诉照常。预审出结果时后台通道发
  `admin.violation.changed`（`action=appeal_judged`）。

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

后台读规则用 `GET /admin/moderation/rules`（与 `/moderation/rules` 内容相同；后者在客户端证明头校验之后，后台调不通）。

`penalty` 结构：`{ kind, hours?, permanent? }`。`AdminViolationRead` 在 `ViolationRead` 基础上多出：`user_id`、`user`、`raw_source`、`confidence`、`reason`、`moderation_record_id`、`report_id`、`case_id`、`actor`、`review_status`、`recommended_penalty`、`reviewed_at`、`appeal_text`、`appealed_at`、`appeal_note`、`appeal_reviewed_at`、`appeal_judge_p`、`appeal_possible_error`（申诉预审，见 1.3）、`revoked_at`、`revoke_reason`。

`GET /admin/stats` 新增 `appeals_pending`、`penalty_reviews_pending`。`POST /admin/reports/{id}/review` 新增可选的 `violation_category`、`violation_level`：给了就按阶梯处罚；同时带 `disable_user` 时以管理员指定的封禁时长为准。原有的手动封禁 / 解封接口不变，但现在都会落到台账里。

后台通道 `/admin/ws` 新增事件 `admin.violation.changed`：`{ violation_id, user_id, action }`。

**防小号 / 设备封禁**：账号之外还能封设备令牌、机器码、浏览器指纹和 IP，后台能看一个账号的关联账号、一次「封禁此人」；
注册时按设备数注册上限。账号被封时要不要自动连带它的设备是一个设置（默认关）。契约见 [device-bans.md](device-bans.md)。

### 1.9 人工复核机器审核记录（后台）

机器「拿不准」（`decision=review`）的内容不再自动处理，需要管理员给最终结论；对机器已经处罚的记录也可以复核纠错。

- `GET /admin/moderation-records` 新增查询参数 `human=pending|done`；每条记录新增 `human_verdict`（`violation` / `clean` / null）、`human_category`、`human_level`、`human_by_id`、`human_at`，以及 `teamup_profile_id`、`teamup_comment_id`、`profile_user_id`。
- `POST /admin/moderation-records/{id}/verdict`，body `{ verdict: "violation" | "clean", category?, level? }` → 更新后的记录。每条记录只能复核一次（重复 409）。
  - `violation`：`category` 必填，`level` 缺省取类别默认值。内容会被撤回 / 删除，并按阶梯记违规；机器已经罚过同一内容时不重复罚，等级更高则改判。
  - `clean`：撤销机器因这条记录给出的处罚（记分、禁言、封禁一并回滚）。**已经撤回 / 删除的内容不会自动恢复。**

复核结论同时是评测审核模型用的标注数据，所以即使不需要处罚，也请把误判的记录判成 `clean`。

### 1.10 二审（机器复判，已实现）

一审是审核服务的规则 + 分类器，每天约 1.7 万条，97% 直接放行。被它标出来的文本（`block` / `review` / 出错，约 2–3%）
再交给审核机上的判定小模型（StartLux-Decision 4B，看上下文）问一次「是否违规」，拿到违规概率 `p` 再决定怎么办。
审核机只有 CPU，一次 4–6 秒，所以只有这一小部分会送过去，一次一个。

**只对纯文本消息**（`type=text`，群聊和私聊都算）生效。图片照旧只看一审：同一条消息里有图片没被放行时整条不送二审。
动态、评论、话题、组队的文字暂不走二审（它们发出前要等审核，二审会把发布再拖几秒到几十秒）。

| 一审 | 二审 `p` | 结果（`judge_outcome`） |
| --- | --- | --- |
| block | ≥ 0.9 | `confirmed`：照旧撤回 + 按阶梯处罚 |
| block | < 0.9 | `downgraded`：**不处罚**，按「拿不准」处理（默认也不撤回；`CONTENT_MODERATION_TEXT_REVIEW_RECALL=true` 时和拿不准一样先撤回）—— 进机器案件（`machine_review` 开着时），排在机器案件最前面，不占每日名额 |
| review / 出错 | < 0.1 | `cleared`：放行，不建案件；`CONTENT_MODERATION_TEXT_REVIEW_RECALL=true` 时也不撤回 |
| review / 出错 | ≥ 0.1 | `flagged`：和原来的「拿不准」一样进机器案件，按 `p` 排先后（一审出错的原来不进，现在也进） |
| 任意 | 二审不可用 | `unavailable`：完全照一审结论办，和没有二审时一样 |

- **时机**：消息照常立刻送达，审核本来就在后台任务里跑。一审 `block` 的消息**等二审出结果再处理**（最多约 45 秒），这段时间里
  消息是可见的 —— 用户投诉的是误罚，宁可晚一点撤回。二审不可用时立刻按原来的规矩撤回 + 处罚。
- **二审不可用**：二审没开、后台开关关着、排队满了、排队加调用超过 `MODERATION_JUDGE_TIMEOUT_SECONDS`、审核服务报错或连不上，
  都算不可用，照一审结论办。连不上 / 审核机关了二审时 60 秒内不再去问。等二审期间进程退出（发版重启）留下的 `pending` 记录，
  由审核重试循环（每 `CONTENT_MODERATION_RETRY_INTERVAL_SECONDS`）按一审结论补做，不会丢。
- **排队**：每个后端进程一个有界优先队列（`MODERATION_JUDGE_QUEUE_SIZE`，默认 16）、同一时刻最多一个请求在路上；
  审核机上还有一道全局并发 1。先做一审 `block` 的，再做一审拿不准（含出错）的，然后是举报和申诉预审，最后是空闲复查（1.11）和题库体检
  （jury-v2.md 2.1.1）。同一档里一审风险分 `risk_score` 高的先做（没有按 0）。
- **上下文**：只给送二审的消息查一次同会话里之前的 `MODERATION_JUDGE_CONTEXT_MESSAGES`（默认 5）条（举报的消息另带后 3 条），
  撤回的和系统消息不带，正文截断。发言人匿名成「被举报人」（待审消息的作者）和 成员A / 成员B …，`@昵称` 换成 `@某人`，
  不带用户名、昵称、ID。上下文只发给我们自己的审核机做自动判断（每条消息本来就会单独送审），不落库，管理员看不到私聊上下文。
- **举报**：新举报的消息在机器预审时同样问二审（复用这条消息发送时的二审结果，没有才问）。一审 `block` 也要二审 ≥ 0.9 才当场
  处罚结案，否则按「拿不准」分到委员 / 管理员。并进已有案件、而案件还没有二审概率的，后台补问一次。`p` 写在案件的 `judge_p`
  上，排队优先级乘以 `0.5 + p`（只调先后）。**二审永远不会驳回举报。**
- **开关**：部署配置 `MODERATION_JUDGE_ENABLED`（默认 true，且只在 `CONTENT_MODERATION_ENABLED` 时生效）；后台「风纪 › 设置」
  的 `machine_judge`（默认开，`PUT /admin/jury/settings` 可改，几秒内对所有进程生效）。两个都开才生效。
  阈值 `MODERATION_JUDGE_BLOCK_THRESHOLD=0.9`、`MODERATION_JUDGE_ALLOW_THRESHOLD=0.1` 来自 88 件人工核实案件的离线评测：
  ≥ 0.9 没有误罚，< 0.1 漏掉 3.6% 的违规。

接口变化：

- `ContentModerationRecordRead`（`GET /admin/moderation-records`）新增 `judge_p`（违规概率）、`judge_ms`（审核服务报的耗时，含它那边的排队）、
  `judge_outcome`（`pending` / `confirmed` / `downgraded` / `cleared` / `flagged` / `scored` / `unavailable` / `prefiltered`；`scored` 是举报预审里只打分的，
  `prefiltered` 是预筛拦下、没问二审的，见 1.12）。
  没送二审的三个都是 null。记录的 `decision` 始终是一审原样。`judge_outcome` 还有一种 `idle_scan`：一审放行、被空闲复查送过二审的（1.11）。
- `ContentModerationRecordRead` 新增 `risk_score`：一审给文本的风险分（0–10，越大越可疑）。审核服务 `/moderate/text` 的结果、`/moderate` 的
  文本结果顶层带 `risk_score`；老版本审核服务没有这个字段、不是 0–10 的数、图片记录都是 null。所有文本记录（消息、动态、评论、话题、组队）都存。
- `CaseRead` 新增 `judge_p`。不给委员看（`JuryCaseView` 里没有），免得影响他们的判断。
- 审核服务新增 `POST /judge/text`（和 `/moderate*` 同一个 `X-Moderation-Token`）：body `{ text, context: [str], after?: [str], mode?: "report" | "patrol" }`，
  返回 `{ p_violation, latency_ms, model, mode }`。`mode` 默认 `report`（举报审核的问法，作者叫「被举报人」；实时二审、举报、申诉都用它，
  上面的阈值是按它评的）；`patrol` 是没人举报的日常巡查（空闲复查用，1.11）：作者叫「发言人」，提示词里写明正常分享链接、@别人、简短提问不算违规。
  老版本审核服务不认 `mode`，照 `report` 问；不可用时 503，`detail.code` ∈ `judge_disabled` / `judge_busy` / `judge_timeout` / `judge_unavailable` / `judge_bad_response`。

### 1.11 空闲复查（已实现）

二审一小时能做 600–1400 次，实时的活每小时只有二十来次。空闲的时候，后端把最近一审**放行**过、但看起来可疑的群聊文字再交给二审看一遍，
概率够高的建成没有举报人的机器案件交给委员（docs/jury-v2.md 3.3）。**从不处罚、从不撤回。**

- **挑哪些消息**：最近 `MODERATION_IDLE_SCAN_LOOKBACK_HOURS`（默认 6）小时、发出满 3 分钟（下文出来了）的**群聊纯文本**消息，一审 `allow`、
  没被处理过、没送过二审、没撤回、不在任何案件里，且满足下面任意一条：
  - 作者注册不满 7 天；
  - 作者和被封的设备、或最近 180 天被封且还封着的账号有强 / 中关联（同一设备令牌、机器码、浏览器指纹；只靠 IP 的不算，见 device-bans.md）；
  - 作者近 30 天有处罚（没撤销的违规记录）；
  - 作者近 7 天被举报过（不论结果）；
  - 消息所在的群近 7 天被举报 ≥ 3 次；
  - 一审风险分 `risk_score` ≥ `MODERATION_IDLE_SCAN_MIN_RISK`（默认 3），不看作者。
  候选先按一审风险分从高到低、再按作者身上的风险信号多少排，每轮最多 `MODERATION_IDLE_SCAN_BATCH`（默认 50）条。查询只走带索引的列
  （`content_moderation_records.created_at`、新加的部分索引 `(created_at, risk_score) WHERE risk_score IS NOT NULL`、
  `user_violations.created_at`、`user_reports.created_at`、`users.banned_at`、设备表的 `(kind, value_hash)`），风险名单每 10 分钟算一次。
- **私聊永远不送**：私聊不是案源，复查连二审都不问。
- **预筛 + 去重**：选出来的候选先过一遍 1.12 的预筛，太短的、只有平台链接的、同一个人 24 小时内说过的同一段话都不问，记录标 `prefiltered`。
- **怎么问**：和举报一样带前 5 条、后 3 条匿名上下文，但用 `mode=patrol` 的巡查问法（作者匿名成「发言人」，不是「被举报人」），放进二审队列的最低一档；只在本进程队列里没有实时的活、库里也没有别的进程
  正在等二审的记录（`judge_outcome=pending`）时才放，一次一条。限速每小时最多 `MODERATION_IDLE_SCAN_HOURLY_CAP`（默认 600）次
  （两次之间至少 6 秒）。二审排不上 / 超时 / 不可用时整体暂停 5 分钟。
- **不重复**：问之前把审核记录的 `judge_outcome` 从空改成 `idle_scan`（认领），概率写回 `judge_p`；没问成的退回空值下次再来。
  之后有人举报这条消息时直接复用这个概率。
- **只有一个进程在扫**：几个 worker 抢 `system_settings` 里的一行租约（`moderation.idle_scan.lease`，3 分钟没续就过期），每问一条续一次。
- **建案**：概率 ≥ `MODERATION_IDLE_SCAN_THRESHOLD`（默认 0.9；2026-10-19 前是 0.6，见 1.12）且后台开关 `machine_review` 开着时，建一个机器案件：`case_origin=machine`、
  `machine_source=idle_scan`、`machine_decision=allow`、理由「机器筛查 · 空闲复查」，`machine_note` 记着为什么选中它（例如
  「空闲复查：新账号、近 7 天被举报；二审 0.83」，只给后台看）。和其他机器案件一起按概率排、全部排在真实举报后面，只用每日名额里留给复查的那一份。
- **开关**：部署配置 `MODERATION_IDLE_SCAN_ENABLED`（默认 true）；后台「风纪 › 设置」的 `machine_idle_scan`（默认开）。另外 `machine_judge`、
  `machine_review` 也都要开着才跑（`machine_review` 关着时建不了案件，问了也白问）。
- **负载估算**：一天约 1.7 万条消息，6 小时窗口里被选中的通常几十到几百条；即使每小时都打满 600 次，审核机也还有一半空闲，
  实时的活最多多等一条（约 5 秒）。
- **误建的案件**：后台可以作废机器案件（单件或按 `machine_source=idle_scan` 一次全部），见 2.4。

### 1.12 预筛与去重（已实现）

2026-10-19 空闲复查上线当天建出一批误报案件：普通的 CCW 作品链接（`https://ccw.site/detail/…?invite=…`、
`https://www.ccw.site/gandi/project/…?inviteToken=…`，p 0.71–0.80）、「@某人？」（0.71），同一个人反复发的同一条链接还建了好几件。
原因是按「举报审核」的口吻问一条没人举报的消息，模型会先入为主往违规上靠。改成三道防线：巡查问法（1.10 的 `mode=patrol`）、
阈值 0.6 → 0.9、以及下面的预筛 —— 不值得问的根本不送。

预筛只看文字本身（`moderation_judge.prefilter_reason`），不查库：

| 规则 | 判定 | 空闲复查 | 实时二审（一审 `review` / 出错） |
| --- | --- | --- | --- |
| 太短 | 去掉 `@昵称`（消息 metadata 里点名的那几个）、空白、标点、表情之后不超过 `MODERATION_PREFILTER_SHORT_CHARS`（默认 3）个字 | 不问 | 只有「一个字都不剩」才不问 —— 一审标出来的「cnm」三个字不能因为短放过 |
| 平台链接 | 除了网址什么都没有，而且每个网址都在 `MODERATION_TRUSTED_LINK_DOMAINS`（默认 `ccw.site,kuke.ink`，含子域名）上 | 不问 | 不问 |

- 空闲复查拦下的记录 `judge_outcome = prefiltered`，下一轮不再选；实时二审拦下的同样标 `prefiltered`，**按放行办**（和 `cleared` 一样：不撤回、不建机器案件）。
- 一审 `block` 的从不预筛：要不要处罚得有二审确认。举报复审也从不预筛（概率只用来排队）。后台开关 `machine_judge` 关着时预筛也不做，完全照一审。
- 「@昵称」只按 metadata 里的点名去掉：「@张三傻逼」里只有「@张三」是点名，「傻逼」留着；没有点名信息的「@某人？」在实时二审里照常问。
- 网址里夹着别的字（「加我领福利 https://ccw.site/…」）、或者有任何一个不在白名单上的网址，都照常问。`https://ccw.site@evil.com/`、
  `https://ccw.site.evil.com/` 不算白名单。

去重（只在空闲复查里）：

- 同一个人 `MODERATION_IDLE_SCAN_DEDUPE_HOURS`（默认 24）小时内已经问过二审（`judge_p` 不为空，实时的、复查的都算）的同一段话，不再问；
  同一批候选里同一个人的同一段话只问排在最前面的那条。「同一段话」= `moderation_judge.dedupe_key` 相同：去掉 `@…`、空白、标点、大小写，
  网址只留域名 + 路径（`?invite=` 之类的参数每次不一样）。
- 建机器案件时（`jury_machine._open_case`，所有来源）：同一个人已经有一件未结的消息案件、内容是同一段话（举报的也算），不再开第二件。

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

1. 新案件先过机器预审（仅消息）：明确违规的（文本消息还要二审确认，见 1.10）当场撤回、处罚、结案。
2. 其余的：**群聊消息**和**公开动态**进风纪委员队列；私聊、举报用户、举报群聊只进管理员队列。
3. 风纪委员 72 小时没有结论、或投票没有共识的，转给管理员。

### 2.4 后台接口

| 接口 | 说明 |
| --- | --- |
| `GET /admin/cases?status=open&queue=&q=&sort=&limit&offset` | `status` ∈ `open` / `resolved` / `dismissed` / `archived`；`queue` ∈ `triage` / `jury` / `panel` / `admin`。返回 `{ items: CaseRead[], total, open_counts: { triage, jury, panel, admin } }`（`open_counts` 只数未结案件，不含已归档）。`sort` 只对未结案件生效：`newest` 创建时间从新到旧（默认）/ `oldest` 从旧到新 / `priority` 按排队优先级；已结、已归档的按结案时间倒序 |
| `GET /admin/cases/{id}` | `CaseDetail` = `CaseRead` + `snapshot`（证据快照）+ `reports`（案件内全部举报）+ `votes`（风纪委员已投的票）。读取会写审计日志 |
| `POST /admin/cases/{id}/resolve` | `{ outcome: "violation" \| "dismiss", category?, level?, delete_content?, penalty?, note?, malicious_reporter_ids? }` |
| `POST /admin/cases/{id}/queue` | `{ queue: "jury" \| "admin" }`，手动改派；涉及私聊的案件不能派给风纪委员 |
| `POST /admin/cases/archive` | 一键归档历史遗留案件，body `{ before: datetime, queue?: "admin" \| "triage" \| "jury" \| "panel", dry_run?: bool }` → `{ count, by_queue: { admin, jury, … }, dry_run }`。见下文 |
| `POST /admin/cases/{id}/void` | 作废一件机器案件（机器误报），body `{ note?: str }` → `CaseDetail`。不是机器案件 422 `not_machine_case`；已结 409 `case_closed`；有人举报并进来了 409 `case_has_reports`。见下文 |
| `POST /admin/cases/void` | 批量作废未结的机器案件，body `{ machine_source?: "idle_scan" \| "review" \| "downgraded", case_ids?: int[]（≤200）, note?, dry_run?: bool }`（`machine_source` 和 `case_ids` 至少给一个，否则 422 `void_scope_required`）→ `{ count, by_queue, skipped, case_ids, dry_run }`。`skipped` 是符合条件但有人举报并进来的（不动） |

`CaseRead` 字段：`id`、`target_type`、`target_id`、`conversation_id`、`message_id`、`reported_user_id`、`reported_user`、`status`、`queue`、`jury_eligible`、`report_count`、`primary_reason`、`priority`、`summary`、`machine_decision`（`allow` / `review` / `block`，已算上二审）、`machine_category`、`machine_level`、`machine_confidence`、`judge_p`（二审违规概率，见 1.10）、`verdict_source`（`auto` / `jury` / `panel` / `admin` / `archive` / `void`）、`verdict_category`、`verdict_category_label`、`verdict_level`、`verdict_note`、`violation_id`、`first_report_id`、`created_at`、`closed_at`。

- `outcome=violation`：`category` 缺省按举报理由映射，`level` 缺省取类别默认等级，`penalty` 缺省走阶梯；`delete_content` 默认 true（撤回消息 / 删除动态）。
- `outcome=dismiss`：可用 `malicious_reporter_ids` 标出恶意举报人，会降低他们的举报额度。
- 原有的 `POST /admin/reports/{id}/review` 仍可用：处理其中一条举报会让整个案件结案，同案的其它举报同步得到结论。

**一键归档**（`POST /admin/cases/archive`）：把创建时间早于 `before`、仍未结的案件（可按 `queue` 只归档一个队列）收起来，用来清理没人处理的历史遗留举报。归档**不是结论**：

- 案件：`status = "archived"`、`closed_at = 归档时间`、`verdict_source = "archive"`、`verdict_note = "历史遗留，未经审理直接归档"`。
- 案件里的每条待处理举报：`status = "rejected"`、`action = "archived"`、`review_note` 同上。用户侧 `ReportRead.archived = true`，`result_title` / `result_text` 是「你的举报已归档」，客户端应显示成「已归档」而不是「未成立」。
- 不记违规、不处罚任何人、不进题库、不结算委员的票、不能改判（`overturn` 返回 409 `case_not_closed`），也不给举报人推送 `report.resolved`。
- 举报人信誉（`/reports/quota`）不计 `action = "archived"` 的举报。
- 风纪委员和资深合议只取 `status = "open"` 的案件，归档后不会再分到；委员已投过票的，票不计一致率，「我的评审」里这类案件 `stage = closed`、`outcome = null`、`verdict = null`。
- `dry_run = true` 只返回数量，不写库。真正归档时写审计日志 `case.archive`（`details` 里有 `before`、`queue`、`count`、`by_queue`、`case_ids`），并在后台通道广播 `admin.report.changed`：`{ action: "archived", count }`。

**作废机器案件**（`POST /admin/cases/{id}/void`、`POST /admin/cases/void`）：机器误报（例如 1.12 之前空闲复查建出来的作品链接案件）直接收掉。
和归档同一个口径，**不是结论**：`status = "archived"`、`verdict_source = "void"`、`verdict_note` = 给的 note 或「机器误报，作废」、`closed_at = 作废时间`；
不处罚、不进题库、不能成为判例；从委员队列和资深合议里消失；委员已经投的票不结算（`agreed` 留空：不计一致率、不给结算的风纪值），
「我的评审」里 `outcome = null`。只动 `case_origin = machine` 且 `report_count = 0` 的未结案件 —— 后来有人举报并进来的要按举报正常审。
写审计日志 `case.void`（`details` 里有 `machine_source`、`count`、`by_queue`、`case_ids`、`note`），后台通道广播 `admin.report.changed`：`{ action: "voided", … }`。
作废的案件仍占当天的机器案件名额（防止作废之后马上又建满一批）。

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
