# 风纪系统 v2 接口契约

在 `moderation-v2.md` 第 2、3 节之上的增量。没提到的保持原样；**现有 `/jury/*` 接口的路径和字段只加不改**，已发布的客户端不受影响。

所有路径前缀 `/api/v1`，都要求登录（照常带客户端证明头）。本文档里新增的错误 `detail` 都是 `{ code, message }`，`message` 可以直接展示；但老接口原有的错误仍是字符串，参数校验失败（422）是框架默认的数组，客户端读错误信息时三种都要兜住。

| 状态 | 范围 |
| --- | --- |
| **第 1 节（一期）：已定稿** | 总览、评审台投后视图、案件详情、观点区、我的评审、公示栏、配置下发 |
| **第 2 节（二期）：已定稿** | 题库、入门考试、练习场、新投票选项、风纪值与层级、成就、排行榜、公开展示身份 |
| **第 3 节（三期）：已定稿** | 资深合议、申诉复审、机器拿不准的内容、判例、议事厅、重复举报与复议 |

已定稿的部分如果要改，会先通知客户端。

## 0. 和产品规格不同的地方及理由

依据是 2026-10-12 对线上库的只读统计：风纪系统刚上线，0 个委员、0 张票；近 30 天每天 1.5 个委员可审案件（近 90 天 3.7 个）；同时满足四项申请条件的用户 81 人；累计 445 个可审的消息案件里 400 个出自同一个 3096 人的公开大厅群。

1. **回避规则放宽（大型公开群）。** 81 个合格用户里 76 个在大厅群里，按「案发群成员一律回避」，89% 的案件全站只有 3–5 人能审。改为：群是 `open` 且成员数 ≥ 200 时，不再按群成员回避，只回避当事人、举报人、被举报人的好友、以及案件快照里出现过的发言人；其余的群保持原规则。阈值可配置。
2. **色情低俗 / 涉未成年类整类不进委员队列。** 案件里只要有一条举报的理由是「色情低俗」，或机器把内容判到色情 / 涉未成年类别，就只走管理员。委员池里未成年人占多数。
3. **委员默认看到快照里的全部上下文（前后各 10 条）**，不再只给前 5 后 3。每条消息带 `offset`，界面默认显示近的几条、可展开。`need_context`（二期）是「全部看完仍不够」，不触发扩窗重发——快照之外的消息拿不到（会话只留最近 1000 条），也不该让非群成员的委员去读实时群消息。
4. **质控题投完当场揭晓**，告诉委员这是质控题和标准结论；质控题不开观点区、不进公示。否则观点区的有无本身就会暴露哪些是质控题，而题库很小。
5. **观点区不过机器文本审核。** 讨论违规内容免不了转述，机器会大量误拦。改为：可举报，同一条观点被 2 名委员举报后自动折叠待处理，管理员可删除 / 恢复。
6. **公示比规格多几条限制**：认定不违规的案件只公示结论和票数，不带内容（否则等于把「此人被举报过」连原话挂出来）；人肉隐私类不带内容；图片一律只给数量；定案满 72 小时且没有待处理的申诉才公示；被改判的标「已改判」并撤下内容。群聊消息的案件不带内容（规格原文），另留一个默认关闭的开关 `JURY_PUBLIC_GROUP_CONTENT`。
7. **「定案前只显示已有 N 票」里的 N 不含弃权**，避免从数字变化反推别人投了什么。
8. **72 小时超时改成后台定时执行**，从进入委员队列算起，不被新举报重置（原来只在管理员打开后台案件页时才触发，且每来一条举报就重新计时）。案件记录转出原因和时间。
9. **入门考试默认不是申请的硬条件**（`exam_required` 默认关）。已发布的客户端没有考试入口，硬性要求会让它们的「申请」按钮直接失效。没考试的申请照旧进审批队列，后台标「未考试」；通过考试的在 `auto_appoint` 打开时直接任命。
10. **自动入库的题视为已核对。** 全票一致（有效票 ≥ 3）的案件自动进题库并可用于考试和质控；否则题库只能靠管理员一道道核对。上线前回填的 35 道历史题仍是未核对。
11. **考试题不带案件编号，且不出公示栏里能看到内容的案件**，否则可以去公示栏查答案。
12. **层级只因一致率低而降，不因数量不够而降**，这样管理员直接任命的创始委员不会被立刻打回见习。
13. **进合议要先确认凑得齐人。** 无共识、上下文不足、超时的案件都先尝试资深合议，但只有此刻确实有 3 位可参加的资深委员时才进，否则直接转管理员；合议 48 小时没结果也转管理员。这样超时案件不会因为「本来就没人能审」而白等一轮。
14. **申诉复审多了两个不进的条件**：色情 / 涉未成年类的处罚（复审要看原内容）、以及此刻凑不齐 3 位可参加的资深委员时，仍直接到管理员。
15. **机器来源的案件超时或无共识时不转管理员**，没有资深合议可用就以「未作处理」结案。它们没有举报人在等结果。
16. **机器来源的案件按委员人数限量**（每天 = 在任委员数 × 2，至少 5）。旧管线每天约 170 条「拿不准」，全收远超委员容量。
17. **待审期间折叠不做**，理由见 3.6。
18. **判例由资深委员直接生效**，不设二次确认；管理员可以改、可以撤。资深委员只能把自己投过票的案件标为判例（他们本来也只看得到这些案件）。
19. **委员看到的案件编号是「评审编号」，不是案件的真实编号**（1.0）。否则质控题能从编号大小和公示栏里认出来。

## 1. 一期

### 1.0 两种案件编号

- **评审编号**：委员侧所有接口里的 `case_id`（评审台、投票、案件详情、观点区、我的评审、资深合议、标判例时提交的、以及发给委员的实时事件和风纪值明细里的）。它对每个委员、每个案件各不相同，是不透明的数字，只对本人有效；路径参数 `{case_id}` 传的也是它。拿别人的评审编号或案件的公开编号来请求一律 404。
- **公开编号**：公示栏、判例、后台里的 `case_id`，是案件本身的编号。

两者不能互相代入。`JuryCaseDetail` 和 `/jury/votes` 的每一项带 `public_case_id`：案件已经进公示栏时是它的公开编号，否则为 null（质控题恒为 null）。

为什么要分开：质控题是早就结案的旧案件，如果委员看到的是真实编号，光看数字大小、或者去公示栏查一下，就能认出哪道是质控题并查到答案。

### 1.1 配置：`GET /jury/config`

客户端要展示的规则数字都从这里读，不要写死。可以缓存到下次启动。

```jsonc
{
  "version": 2,
  // 后端已经实现的能力；客户端据此决定显示哪些板块。二期、三期上线后会追加
  "capabilities": ["overview", "case_detail", "opinions", "votes", "public_cases"],
  // 三个会改变「谁能处罚用户」的开关，生产默认全关
  "switches": { "auto_appoint": false, "appeals": false, "machine_review": false },
  "tiers": [
    { "key": "trainee", "label": "见习委员" },
    { "key": "regular", "label": "正式委员" },
    { "key": "senior",  "label": "资深委员" }
  ],
  // 投票选项，按展示顺序。kind ∈ clean / violation / abstain；二期追加 recuse / need_context
  "votes": [
    { "value": "none",    "kind": "clean",     "label": "不违规",       "hint": "没有违反社区规则" },
    { "value": "l1",      "kind": "violation", "label": "违规 · 轻微", "hint": "…", "level": 1 },
    { "value": "l2",      "kind": "violation", "label": "违规 · 一般", "hint": "…", "level": 2 },
    { "value": "l3",      "kind": "violation", "label": "违规 · 严重", "hint": "…", "level": 3 },
    { "value": "abstain", "kind": "abstain",   "label": "拿不准",       "hint": "看不懂或无法判断，不计入表决" }
  ],
  "rules": {
    "seats_per_case": 5,          // 每个案件最多 5 张有效票
    "quick_consensus_votes": 3,   // 有效票全部一致、且不少于 3 张时定案
    "full_consensus_votes": 4,    // 出现分歧后要收满 5 张，其中至少 4 张一致
    "timeout_hours": 72,
    "assignment_ttl_hours": 24,   // 分到手的案件 24 小时内不投会让给别人
    "daily_votes": 30,
    "term_days": 30,
    "context_near": 3,            // 建议默认展示目标消息前后各几条，其余折叠
    "content_retention_days": 30, // 定案后多久内委员还能回看案件内容
    "publish_delay_hours": 72,    // 定案后多久进公示栏
    "min_sample": 5,              // 总览里的比率至少要这么多样本才给
    "max_ban_hours": 168          // 委员和机器给出的封禁最长多久；更重的要管理员确认
  },
  "opinions": {
    "max_length": 500,
    "per_case": 20,               // 每人每个案件最多发多少条（含回复）
    "per_minute": 5,
    "per_day": 100,
    "auto_hide_reports": 2,
    "report_reasons": ["人身攻击", "泄露身份或隐私", "照抄违规原文", "与案件无关", "其他"]
  },
  "eligibility": { "min_account_age_days": 30, "min_recent_messages": 30, "recent_activity_days": 30 }
}
```

**表决规则的准确说法**（界面文案请照这个写，数字从 `rules` 里取）：

- 有效票（不含「拿不准」等不计入表决的票）**全部一致**，且已有 `quick_consensus_votes` 张或更多：定案。
- 一旦出现分歧，就要收满 `seats_per_case` 张有效票：其中至少 `full_consensus_votes` 张一致则定案，否则转出（资深合议或管理员）。

所以「3 比 1」不会定案，要等第 5 张票：变成 4 比 1 定案，变成 3 比 2 转出。看的是「违规 / 不违规」是否一致，等级取违规票的较低中位数；二期起，按现有票会定为严重违规（l3）时至少要 `severe_min_votes` 张有效票。

### 1.2 `GET /jury/me` 新增字段

原有字段不变（其中 `stats`：只要有过成员记录就是对象，包括申请中还没当上委员的人，此时全 0；从没申请过的人为 null）。新增：

| 字段 | 说明 |
| --- | --- |
| `tier` | `trainee` / `regular` / `senior`；不是在任委员时为 null |
| `available_cases` | 现在能分给我的未结案件数（已去掉要回避的和投过的；上限 50）。不是在任委员时为 null。用来做导航角标 |
| `restriction` | 在任委员正在受处罚时为 `{ "reason": "muted" \| "banned", "until": "…" \| null, "message": "受处罚期间不能参与评审" }`，否则为 null。有值时评审、发观点、点赞、议事厅发言都会被服务端拒绝（403 `juror_restricted`），界面应直接显示受限状态；回看自己投过的案件不受影响 |

### 1.3 总览：`GET /jury/overview`

所有登录用户可读。服务端缓存 60 秒（`me` 不缓存）。

```jsonc
{
  "jurors": { "total": 12, "trainee": 8, "regular": 3, "senior": 1 },   // 在任委员
  "backlog": { "jury": 4, "panel": 0 },        // 等委员评审 / 等资深合议（三期前恒为 0）
  "settled": { "d7": 9, "d30": 31, "total": 120 },   // 经过委员评审并已定案的案件数
  "window_days": 30,
  "sample": 31,                // 窗口内离开委员队列（定案或转出）的案件数
  "sample_enough": true,       // false 时下面三个比率都是 null，客户端只显示件数
  "median_hours": 6.4,         // 举报到定案的中位用时；没有样本时为 null
  "consensus_rate": 0.87,      // 委员自己定案的比例 = consensus / sample
  "violation_rate": 0.61,      // 定案里认定违规的比例
  "overturn_rate": 0.03,       // 定案后被改判的比例
  "counts": { "consensus": 27, "escalated": 4, "violation": 19, "clean": 12, "overturned": 1 },
  "me": { "status": "active", "tier": "trainee", "available_cases": 2, "voted_today": 3, "daily_limit": 30 },
  // 近 30 天每天一项，按日期正序（UTC+8 自然日），没有数据的日子也给 0；样本不足时照样给
  "daily": [{ "date": "2026-10-11", "queued": 3, "settled": 2 }],
  "generated_at": "2026-10-12T03:00:00Z"
}
```

- `daily[].queued` 是当天进入委员队列的案件数，`settled` 是当天定案的（口径同 `settled`）。
- 只统计本系统上线之后真正进过委员队列的案件；历史回填和机器自动结案的不算。
- `me.status` 的取值同 `/jury/me`；`tier` / `available_cases` 在不是在任委员时为 null。

### 1.4 评审台

`GET /jury/cases/next` 不变，`JuryCaseView` 新增：

| 字段 | 说明 |
| --- | --- |
| `messages[].offset` | 相对被举报消息的位置：目标是 0，之前为负，之后为正 |
| `messages` 的范围 | 现在是快照里的全部上下文（前后最多各 10 条）。建议默认展示 `abs(offset) <= rules.context_near` 的，其余折叠 |
| `assigned_at` / `expires_at` | 分到手的时间和让出的时间 |

`POST /jury/cases/{case_id}/vote` 请求不变，响应新增 `case`：

```jsonc
{ "case_id": 42, "settled": false, "case": { /* JuryCaseDetail，见 1.5 */ } }
```

`note` 会成为这个委员在该案件下的第一条观点（见 1.6）。质控题的 `note` 只留给管理员看。

### 1.5 案件详情（投后）：`GET /jury/cases/{case_id}`

只有对这个案件**已经投过票**的人能读。评审编号不是我的（包括拿公开编号来请求）返回 404 `case_not_found`；分到手还没投、或者我回避了的返回 403 `not_voted`。被停职 / 卸任的前委员仍可回看自己投过的案件。

```jsonc
{
  // —— 与 JuryCaseView 相同的部分 ——
  "case_id": 42,
  "kind": "message",
  "reasons": [{ "reason": "辱骂骚扰", "count": 2 }],
  "report_count": 2,
  "messages": [ /* … */ ],
  "post": null,
  // 定案满 content_retention_days 后为 false，此时 messages 为空、post 为 null
  "content_available": true,

  // —— 我的票 ——
  "my_vote": {
    "vote": "l2", "note": "连续三条都在骂人", "voted_at": "…",
    "alias": "委员 B",        // 我在这个案件里的代号
    "agreed": null            // 是否与结论一致；未定案或我弃权时为 null
  },

  // —— 进度与结论 ——
  "stage": "jury",            // jury 委员评审中 / panel 资深合议中 / admin 等管理员 / closed 已定案
  "votes_cast": 3,            // 已有几张有效票（不含弃权）
  "tally": null,              // 定案后才给：{ "none": 1, "l1": 0, "l2": 3, "l3": 0, "abstain": 1 }
  "verdict": null,            // 定案后才给，见下
  "is_quality_check": false,
  "opinions_enabled": true,   // 质控题为 false
  "opinion_count": 4,
  "public_case_id": null      // 已进公示栏时是公开编号，见 1.0
}
```

`verdict`（`stage == "closed"` 时）：

```jsonc
{
  "outcome": "violation",     // violation / clean
  "source": "jury",           // jury 委员共识 / panel 资深合议 / admin 管理员 / auto 机器
  "category": "harassment", "category_label": "辱骂骚扰",   // clean 时为 null
  "level": 2,                 // clean 时为 null
  "closed_at": "…",
  "hours": 5.2,               // 举报到定案
  "overturned": false,        // 定案后被改判
  "overturn_note": null,
  "explanation": null         // 题库里这道题的讲解；只有质控题且管理员写了才有（二期起）
}
```

- 定案前 `tally`、`verdict` 都是 null，只有 `votes_cast`。
- **质控题**：`is_quality_check: true`，投完立刻 `stage: "closed"`、`verdict` 是标准结论、`tally` 为 null、`votes_cast` 为 0、`opinions_enabled: false`。客户端应当明确告诉委员「这是一道质控题」并展示标准结论和 `my_vote.agreed`。
- `my_vote.agreed` 在改判后会跟着变。改判后 `verdict.outcome` / `category` / `level` 是新结论，`overturned: true`；`verdict.source` 和 `tally` 保持原审的样子（它们描述的是原来那次表决）。公示栏同理。
- 质控题不分配代号，`my_vote.alias` 为 null。
- 内容保留期从「定案时间」和「我投票的时间」里较晚的那个算起（质控题是早就定案的案件）。

### 1.6 观点区

谁能读写：对该案件投过票的委员（投票之后）；发表、回复、点赞、举报还要求当前是在任委员且没有受处罚。质控题没有观点区。

**列表** `GET /jury/cases/{case_id}/opinions` → `{ "items": Opinion[], "total": 7 }`

一次返回全部（一个案件最多 7 个参与者，量很小），按时间正序；回复挂在所属的那条观点下面，只有一层。`total` 是未删除的观点数（含回复、含被折叠的）。已删除的回复不出现；已删除的根观点只在下面还有回复时留一个占位（`state: "deleted"`、`content: null`，`mine` 等字段照常给）。

```jsonc
{
  "id": 301,
  "parent_id": null,
  "alias": "委员 B",
  "tier": "regular",
  "vote": null,               // 这位委员投的票；定案后才给
  "content": "连续三条都在骂人，不是玩笑",    // 已删除 / 被折叠时为 null
  "state": "visible",         // visible / deleted（本人或管理员删除）/ hidden（多人举报后折叠，待处理）
  "is_note": true,            // 投票时写的备注
  "mine": false,
  "like_count": 2,
  "liked": false,
  "reported": false,          // 我是否举报过
  "created_at": "…",
  "reply_to_alias": null,     // 回复的是哪位（回复别人的回复时用）
  "replies": [ /* 同结构，parent_id 指向这条，replies 恒为空 */ ]
}
```

**发表** `POST /jury/cases/{case_id}/opinions`，body `{ "content": "≤500 字", "parent_id"?: 301 }` → `Opinion`

- `parent_id` 可以是任何一条观点；如果它本身是回复，新回复会挂到同一条根观点下，并带上 `reply_to_alias`。

**删除** `DELETE /jury/opinions/{id}` → `{ "id": 301, "state": "deleted" }`，只能删自己的。

**点赞** `POST /jury/opinions/{id}/like`、`DELETE /jury/opinions/{id}/like` → `{ "id": 301, "like_count": 3, "liked": true }`。重复点赞 / 取消是幂等的；不能赞自己的。

**举报** `POST /jury/opinions/{id}/report`，body `{ "reason": "人身攻击", "details"?: "≤500 字" }` → `{ "id": 301, "state": "visible" | "hidden" }`

错误码：

| HTTP | `detail.code` | 含义 |
| --- | --- | --- |
| 403 | `not_voted` | 这个案件分给我了但还没投，或者我回避了 |
| 404 | `case_not_found` | 评审编号不是我的 |
| 403 | `not_juror` / `juror_restricted` | 不是在任委员 / 受处罚期间（只读不受影响） |
| 403 | `opinions_closed` | 这个案件没有观点区（质控题） |
| 404 | — | 案件或观点不存在，或观点不属于我能看的案件 |
| 403 | `not_opinion_author` | 删除别人的观点 |
| 400 | `cannot_like_own` / `cannot_report_own` | |
| 409 | `already_reported` | |
| 422 | `invalid_opinion` | 内容为空或超长 |
| 422 | `invalid_reason` | 举报理由不在列表里；选「其他」时 `details` 至少 5 个字 |
| 429 | `opinion_rate_limited` | 发得太快 / 今天太多 / 这个案件下已发满 |

### 1.7 我的评审

`GET /jury/votes?limit=20&offset=0&state=&outcome=&agreed=&vote=` → `{ "items": JuryVoteItem[], "total": 133 }`，按投票时间倒序。原来的 `GET /jury/history` 保留不变。

| 参数 | 取值 |
| --- | --- |
| `state` | `open` 未定案 / `closed` 已定案 |
| `outcome` | `violation` / `clean` |
| `agreed` | `true` / `false` |
| `vote` | `none` / `l1` / `l2` / `l3` / `abstain`，可用逗号分隔多个 |
| `limit` | 1–50，默认 20 |

```jsonc
{
  "case_id": 42, "kind": "message", "primary_reason": "辱骂骚扰",
  "vote": "l2", "voted_at": "…", "alias": "委员 B",
  "stage": "closed",
  "outcome": "violation", "verdict_source": "jury", "verdict_level": 2,
  "closed_at": "…", "agreed": true, "overturned": false,
  "is_quality_check": false,
  "opinion_count": 4,
  "public_case_id": null
}
```

`GET /jury/me/stats` → 个人统计：

```jsonc
{
  "votes_total": 133, "votes_settled": 97, "votes_agreed": 81, "agreement_rate": 0.835,
  "golden_total": 20, "golden_correct": 17, "golden_rate": 0.85,
  "abstain_total": 9, "abstain_rate": 0.068,
  "opinions_total": 31, "likes_received": 44,
  // 近 30 天，每天一项，按日期正序；日期是 UTC+8 的自然日
  "trend": [{ "date": "2026-10-11", "votes": 4, "settled": 3, "agreed": 3 }]
}
```

从没当过委员的人调这两个接口：`/jury/votes` 返回空列表，`/jury/me/stats` 返回全 0。

- 三个比率在分母为 0 时给 0，不是 null（`/jury/me.stats.agreement_rate` 仍是 null，没动）；要判断「还没有数据」请看对应的 `*_total` / `votes_settled`。
- `trend` 的 `votes` 含质控题，`settled` / `agreed` 不含（与一致率同口径）。
- 查询参数留空（`state=&agreed=`）等同于不筛选。

### 1.8 公示栏

所有登录用户可读。

`GET /jury/public/cases?limit=20&offset=0&kind=&outcome=&category=` → `{ "items": PublicCase[], "total": 120 }`，按定案时间倒序。`kind` ∈ `message` / `post`；`outcome` ∈ `violation` / `clean`；`category` 是违规类别 key。

```jsonc
{
  "case_id": 42,
  "kind": "post",
  "reasons": [{ "reason": "垃圾广告", "count": 3 }],
  "report_count": 3,
  "outcome": "violation",
  "category": "advertising", "category_label": "垃圾广告",   // clean 时为 null
  "level": 2,                                                 // clean 时为 null
  "source": "jury",            // jury / panel / admin
  "tally": { "none": 0, "l1": 1, "l2": 3, "l3": 0, "abstain": 0 },
  "votes": 4,                  // 有效票数（不含弃权）
  "closed_at": "…",
  "hours": 5.2,
  "overturned": false,
  "has_content": true          // 详情里是否带内容
}
```

`GET /jury/public/cases/{case_id}` → 上面的字段，加：

```jsonc
{
  "content": { "text": "……", "image_count": 2, "created_at": "…" },   // 没有内容时为 null
  // 没有内容的原因：group_message 群聊消息不公示内容 / clean 认定不违规 /
  // sensitive 涉及隐私等类别 / overturned 已改判 / null 有内容
  "content_hidden_reason": null,
  "overturn_note": null
}
```

进公示栏的条件：经过委员评审（至少有一张委员票）并已定案的案件；定案满 `publish_delay_hours`；相关处罚没有待处理的申诉。质控题、机器自动结案的不进。没有公示的案件访问详情返回 404。

`GET /jury/public/cases/{case_id}/opinions` → `{ items: Opinion[], total }`（`case_id` 是公开编号）：只在后台开关 `opinions_open_to_jurors` 打开时可用，供在任委员只读地看已公示案件的观点区。404 `case_not_found` 不在公示栏里；403 `not_juror` 不是在任委员；403 `opinions_closed` 开关没开，或我和这个案件有回避关系。只读：不能在这里发表、点赞、举报。开关默认关，客户端在收到 403 时不显示这块即可。

内容只在同时满足这些条件时给：公开动态、认定违规、类别不是隐私 / 色情类、没有被改判。只给被举报的那一篇的文字，图片只给数量。

### 1.9 实时事件（用户通道 `/ws`）

| 事件 | data | 发给谁 | 用途 |
| --- | --- | --- | --- |
| `jury.queue.changed` | `{}` | 在任委员 | 有案件进入 / 离开委员队列。收到后重拉 `/jury/me` 更新角标。同一进程 30 秒内最多发一次，不是逐案通知 |
| `jury.case.settled` | `{ case_id, outcome }` | 该案件投过票的委员 | 案件定案 / 被改判，详情和历史可刷新 |
| `jury.opinion.created` | `{ case_id, opinion_id, parent_id }` | 该案件投过票的委员（发表者除外） | |
| `jury.opinion.changed` | `{ case_id, opinion_id, state }` | 该案件投过票的委员 | 观点被删除 / 折叠 / 恢复 |
| `jury.status.changed` | `{}` | 本人 | 已有。资格、层级变化 |

发给委员的事件里的 `case_id` 都是**接收者本人的评审编号**（同一个案件，每个委员收到的数字不同），可以直接用来失效本地缓存或跳到「我的评审」。二期的 `jury.points.changed` 同理；它的 `case_id` 在每日首票这类不对应具体案件的加分上为 null。

### 1.10 后台接口（`/admin`）

| 接口 | 说明 |
| --- | --- |
| `GET /admin/jury/settings` | 三个开关的当前值、全部阈值的当前值（只读展示） |
| `PUT /admin/jury/settings` | `{ auto_appoint?, appeals?, machine_review?, public_group_content?, opinions_open_to_jurors?, exam_required? }`（最后一个二期起），写审计日志，几秒内对所有进程生效 |
| `POST /admin/jury/members/{user_id}/review` | 原有。`approve` / `reinstate` 新增可选的 `tier`（创始任命：直接任命为任意层级）；新增 `action: "set_tier"` |
| `GET /admin/jury/opinions?state=&reported=&case_id=&limit&offset` | 观点列表，带真实作者和举报记录 |
| `POST /admin/jury/opinions/{id}/moderate` | `{ action: "delete" \| "restore" \| "dismiss_reports", note? }` |
| `POST /admin/cases/{id}/overturn` | `{ outcome: "violation" \| "dismiss", category?, level?, note }`，改判已结案件：撤销或补记处罚、同步举报结论和委员的一致率、能恢复的内容恢复 |
| `GET /admin/cases` / `GET /admin/cases/{id}` | `CaseRead` 新增 `jury_outcome`（`consensus` / `no_consensus` / `timeout` / `need_context` / `manual`）、`queued_at`、`escalated_at`、`published_at`、`overturned_at`、`overturn_note`、`jury_block_reason`；详情的 `votes[]` 新增 `alias`、`tier`、`category`、`review_id`（委员看到的评审编号），另加 `opinions[]` |

`JuryMemberRead` 新增 `tier`。申诉通过（撤销处罚）时，如果这条处罚来自案件，案件自动按改判处理。

## 2. 二期

二期上线后 `config.capabilities` 追加：`exam`、`practice`、`votes_v2`、`points`、`tiers`、`achievements`、`leaderboard`、`badge`。客户端按它决定显示哪些板块。

### 2.0 体量与阈值的依据

全站每天 5–19 张真实票（1.5–3.7 个案件 × 3–5 票）。如果有 10–15 个活跃委员，一个认真的委员每月大约能有 20–30 张已出结论的票，另有约五分之一的质控题。层级阈值按这个量定：见习到正式 15 张已结票（约一个月），正式到资深 50 张（约三个月）。全部阈值是配置项，`GET /jury/config` 的 `tiers[].requirements` 会下发当前值。

### 2.0.1 `GET /jury/config` 二期新增的部分

```jsonc
{
  // 投违规票时可选的类别（不含色情类）
  "categories": [{ "key": "harassment", "label": "辱骂骚扰", "default_level": 2 }],
  // 事件 kind → 分值，见 2.6
  "points": { "vote_agreed": 10, "vote_disagreed": 3, "quality_correct": 5, "quality_wrong": -5,
              "daily_first": 2, "streak_7": 10, "opinion_liked": 5, "exam_passed": 20, "overturn_adjust": 7 },
  "tiers": [
    { "key": "trainee", "label": "见习委员", "requirements": null, "demote_below": null },
    { "key": "regular", "label": "正式委员",
      // 晋升到这一层的条件；quality_rate 在答过 quality_min 道质控题之后才看
      "requirements": { "votes_settled": 15, "agreement_rate": 0.7, "quality_rate": 0.7, "quality_min": 3, "days_served": 14 },
      // 这一层的降级线：最近 window 张已结票（不足 min_votes 不判断）一致率低于 agreement_rate 时降到 to
      "demote_below": { "agreement_rate": 0.6, "window": 30, "min_votes": 15, "to": "trainee" } },
    { "key": "senior", "label": "资深委员",
      "requirements": { "votes_settled": 50, "agreement_rate": 0.8, "quality_rate": 0.8, "quality_min": 8, "days_served": 60 },
      "demote_below": { "agreement_rate": 0.7, "window": 30, "min_votes": 15, "to": "regular" } }
  ],
  // 系统自动暂停资格的条件
  "suspend": { "min_settled": 20, "agreement_rate": 0.6, "quality_min": 5, "quality_rate": 0.6 },
  "exam": { "total": 10, "pass_score": 8, "time_limit_minutes": 60, "grace_seconds": 90,
            "attempts_max": 3, "window_days": 30, "retry_hours": 24, "valid_days": 90 },
  "practice": { "daily_limit": 20 },
  "recuse": { "daily_limit": 5 },
  "abstain": { "window": 50, "min_votes": 20, "warn_rate": 0.3, "cooldown_rate": 0.5, "cooldown_hours": 24 }
}
```

`rules` 新增 `severe_min_votes`（5，定为严重违规至少要几张有效票）、`need_context_votes`（2）。`opinions` 新增 `like_reward_threshold`（3，一条观点的赞数达到多少给作者加分）。

### 2.1 题库

题库里的一道题 = 一个已经有结论的案件（内容是匿名的案件快照）。来源有两个：

- 管理员在后台标注（可以写说明、改标准答案）。
- 自动入库：委员全票一致通过（有效票 ≥ 3）、定案满 7 天（过了举报人还能申请复议的窗口）、没有被改判、没有待处理申诉的案件。有分歧的（比如 4 比 1）不入库。案件之后被改判的，自动移出题库。

不入库的：涉及私聊的、色情 / 涉未成年 / 隐私类别的、公示栏里会展示内容的（公开动态且认定违规——否则考试可以去公示栏查答案）。

每道题有「已核对」标记：管理员核对过的，或自动入库的（全票一致本身就是核对）。**未核对的题只用于练习，不用于考试、不用于评审中的质控计分。** 系统上线前回填的 35 道历史题默认未核对。

**判一道题要看被举报人在上下文里的全部言行**，不能只看被标出来的那一条：被举报的那句是「笑死我了」，但前面同一个人刚骂过对方，标准答案就是违规；同一句话连发了九遍，就是刷屏。

计分题（已核对）还要满足委员能看全（`verify_block_reason`，后台题目详情里有 `verify_block_reason` / `verify_block_message`，标成已核对时不满足返回 422 `question_not_verifiable`）：

- 标准答案是「不违规」的，被举报人在上下文里不能有看不到的消息（已撤回 / 已删除，委员看到的是空白）或语音 —— 答案可能就在看不到的那几条里；
- 被举报的那条不能是语音（旧版客户端放不了）。

自动入库的题不满足时只进练习。

**质控题出错之后：**

- 管理员改标准答案时，之前在评审中答这道题、按新答案是对的人改判为答对：退回当时实际扣掉的风纪值并补上答对的分（流水 `quality_fixed`「质控题更正」），正确数 +1，发通知说明。按新答案变成答错的人不追溯扣分。
- 计过分的题被改回未核对、或移出题库时，之前答错的一律改判为答对（同上）。
- 改判之后，被系统按准确率自动暂停的委员如果准确率回到暂停线以上，自动恢复资格（审计 `jury.member.auto_reinstate`，通知 `reinstated`）。
- 某道质控题在上次核对之后答错的 ≥ 2 人且占 ≥ 40%，自动改回未核对、移出考试和质控（审计 `jury.question.auto_pulled`），等管理员复核：改答案、移出题库或重新核对。只移出、不自动改判。

### 2.2 资格与申请

`/jury/me` 的 `requirements` 里「近 30 天发言」改为取两个数里较大的那个：消息表里的条数、群活跃日报里的发言数（后者不受「每个会话只留最近 1000 条」影响）。

`/jury/me` 新增：

| 字段 | 说明 |
| --- | --- |
| `exam` | `{ available, passed, passed_at, attempts_used, attempts_max, next_attempt_at, last_score, pass_score, total }`。`available: false` 表示题库里已核对的题不够，暂时不能考 |
| `exam_required` | 申请是否必须先通过考试。默认 false（已发布的旧客户端没有考试入口） |
| `points` | 风纪值；没当过委员为 0 |
| `tier_progress` | 见 2.6；不是在任委员为 null |
| `badge_visible` | 是否公开展示委员身份 |
| `warnings` | `[{ code, message, current, threshold, sample }]`，后三个可能为 null。`code` ∈ `high_abstain`（弃权偏多）/ `demote_risk`（最近的一致率接近或低于降级线）/ `suspend_risk`（一致率或质控正确率接近或低于暂停线）。`current` 是服务端实际用来判断的那个数（比如最近 30 张已结票的一致率），`sample` 是它的样本数。客户端直接展示，不要自己估算 |
| `cooldown_until` | 弃权过多被暂停发案到什么时候；没有则为 null |
| `recuse_left_today` | 今天还能主动回避几次 |
| `daily_resets_at` | 评审上限的下一个名额什么时候释放（最近 24 小时里最早那一票的时间 + 24 小时）；最近 24 小时没投过票时为 null |
| `recuse_resets_at` | 回避上限的下一个名额什么时候释放；最近 24 小时没回避过时为 null |

`POST /jury/apply` 的行为：

| 情况 | 结果 |
| --- | --- |
| 已通过考试，`auto_appoint` 开 | 直接成为见习委员（`status: active`，`tier: trainee`） |
| 已通过考试，`auto_appoint` 关 | `applied`，后台审批队列里显示考试成绩 |
| 没考试，`exam_required` 关 | `applied`（同现状），后台显示「未考试」 |
| 没考试，`exam_required` 开 | 403 `exam_required` |

任期届满时，条件仍满足（没有违规记录、没被暂停）就自动续任，不需要再点申请；不满足时状态变 `expired` 并发通知。晋升、降级、被系统暂停也会发通知（通知中心 `type="jury"`、`category="system"`）。

### 2.3 入门考试

`GET /jury/exam` → 当前状态：

```jsonc
{
  "available": true,
  "eligible": true,              // 四项资格是否都满足；不满足不能考
  "passed": false, "passed_at": null,
  "total": 10, "pass_score": 8, "time_limit_minutes": 60,
  "attempts_used": 1, "attempts_max": 3, "window_days": 30,
  "next_attempt_at": "…",        // 现在就能考时为 null
  "last_result": { "score": 6, "passed": false, "submitted_at": "…" },
  "active": null,                // 进行中的一场：{ attempt_id, expires_at, questions: ExamQuestion[] }
  "server_time": "…"             // 服务器当前时间，用来校正本机倒计时
}
```

`POST /jury/exam/start` → `{ "attempt_id": 7, "expires_at": "…", "questions": ExamQuestion[], "server_time": "…" }`。已有进行中的一场时返回同一场。服务端不保存作答草稿。

`ExamQuestion` 和 `JuryCaseView` 同形（`kind`、`reasons`、`messages`、`post`），但没有 `case_id`，用 `question`（1–10 的序号）标识。

`POST /jury/exam/{attempt_id}/submit`，body `{ "answers": [{ "question": 1, "vote": "l2" }, …] }`（`vote` ∈ `none` / `l1` / `l2` / `l3`）。**可以不答完就交**：没出现在 `answers` 里的题按答错算，`results` 里它的 `your_vote` 为 null。→

```jsonc
{
  "score": 8, "total": 10, "pass_score": 8, "passed": true,
  "results": [
    { "question": 1, "correct": true, "your_vote": "l2",
      "answer": { "outcome": "violation", "level": 2, "category_label": "辱骂骚扰", "explanation": "……" } }
  ],
  "attempts_left": 2, "next_attempt_at": null,
  "status": { /* JuryStatus：通过且 auto_appoint 开时这里已经是 active */ }
}
```

- 只判「违规 / 不违规」对不对；等级不计分，但 `answer` 里给出。
- 没通过：24 小时后可重考，30 天内最多 3 次。
- 交卷在 `expires_at` 之后还有 `grace_seconds`（90 秒）的宽限；再晚返回 409 `exam_closed`，这一场按 0 分、未通过记一次。409 `exam_closed` 的 `detail.reason` 区分两种情况：`submitted` 这一场已经交过卷（比如在另一个设备上）、`expired` 超时作废。服务端不保存作答，超时后没法按已有的作答判分。客户端可以在倒计时结束前自动提交已有的答案。
- 通过后 90 天内有效（期间没被任命的话要重考）。
- 通过后不会自动提交申请，除非 `auto_appoint` 开着（此时直接任命）；否则客户端接着调 `POST /jury/apply`。

错误码：403 `jury_not_eligible`；409 `exam_unavailable`（题库不足）、`exam_passed`（已通过）、`exam_closed`（这一场已交卷或已过期）；429 `exam_cooldown`（`detail.next_attempt_at`）、`exam_attempts_exhausted`；422 `invalid_answers`（题号不存在、重复或 `vote` 不合法）。

### 2.4 练习场

满足四项资格的用户、以及当过委员的人都可以练习。不计入一致率，不给风纪值。

`GET /jury/practice/next` → `{ "question": PracticeQuestion | null, "left_today": 17, "daily_limit": 20, "stats": { "answered": 34, "correct": 29 }, "round": { "total": 42, "done": 30 }, "resets_at": "…" }`

`resets_at` 是练习上限的下一个名额什么时候释放（最近 24 小时没练过时为 null），答题响应里也带。`round.total` 是题库里我能练的题数，`round.done` 是其中我做过的（不重复计）；`done == total` 说明这一轮做完了，之后出的都是重做的题。

`PracticeQuestion` = `JuryCaseView` 的内容字段 + `question_id` + `verified`（是否已核对）+ `answered_before`（我以前做过这道题）。受处罚期间（`restriction` 有值）也可以练习，服务端不拦。没做过的题优先；都做过后按「最久没做」的顺序重出。题库为空时 `question: null`。

`POST /jury/practice/{question_id}/answer`，body `{ "vote": "none" | "l1" | "l2" | "l3" }` →

```jsonc
{
  "correct": true,
  "answer": { "outcome": "violation", "level": 2, "category_label": "辱骂骚扰", "explanation": "……" },
  "left_today": 16,
  "stats": { "answered": 34, "correct": 29 }      // 我的练习累计
}
```

错误码：403 `jury_not_eligible`；404 题目不存在；429 `practice_daily_limit`（`detail.resets_at`）。

### 2.5 投票的变化

`config.votes` 追加两项，`JuryVoteCreate.vote` 相应多两个取值：

| vote | kind | 含义 |
| --- | --- | --- |
| `recuse` | `recuse` | 主动回避：我可能认识当事人或有利害关系。案件改派给别人；不占席位、不计入当日评审数和总票数、不影响一致率。每天最多 5 次，超了返回 429 `recuse_limit` |
| `need_context` | `abstain` | 上下文不足，无法判断。不计入表决；同一案件累计 2 票后转出（三期起转给资深合议，之前转管理员） |

- 回避之后不能看这个案件的详情和观点区（响应里 `case: null`）。
- `/jury/votes`：回避和「上下文不足」的记录都会列出，`vote` 分别是 `recuse` / `need_context`，`vote` 筛选参数也接受这两个值。每项新增 `can_view`：回避的记录为 false（点不进详情），其余为 true。**回避的记录不透露案件的任何进展**：`stage`、`outcome`、`agreed`、`closed_at`、`verdict_*`、`public_case_id` 都是 null，`opinion_count` 为 0，`is_quality_check` 恒为 false；带 `state` / `outcome` / `agreed` 筛选时不返回回避的记录。回避过的质控题过几天（默认 7 天）可能再次出现，评审编号和上次相同，再出之后它就不在回避记录里了。
- 案件详情和公示栏的 `tally` 新增一个键 `need_context`；`recuse` 不出现在 `tally` 里。`votes_cast` 仍然只数有效票。
- 质控题上也可以回避，但下一件仍会补一道质控题。
- 案件详情也带 `suggested_category`，`my_vote` 里带我选的 `category`（没选为 null）。
- **投违规票时可以带 `category`**：`{ "vote": "l2", "category": "harassment", "note"?: … }`。可选的类别在 `config.categories`（`[{ key, label, default_level }]`，不含色情类）；`JuryCaseView.suggested_category` 是按举报理由映射的默认值。定案类别取违规票里的多数，平票时取默认值。
- **严重违规要 5 票**：如果按现有票数会定为 l3，但有效票不足 5 张，案件继续收票，到 5 张再按原规则表决。
- **质控题的位置随机**：不再固定在第 5、10、15… 件；只出已核对的题。质控题在投票之前展示的内容时间会整体平移到近期（它们是早就结案的旧案件，不平移的话看时间就能认出来），各条消息的间隔不变；投完揭晓后显示真实时间。客户端不用做任何事，只是别拿投票前后的时间做比较。
- **弃权过多**：近 50 张票里「拿不准 + 上下文不足」超过 30% 时 `/jury/me.warnings` 给 `high_abstain`；超过 50% 时暂停发案 24 小时，`GET /jury/cases/next` 返回 429 `jury_cooldown`（`detail.until`）。不足 20 张票时不判断。

`POST /jury/cases/{case_id}/vote` 的响应新增 `reward`（见 2.6）。

### 2.6 风纪值、层级

**风纪值**（数值在 `config.points` 下发，下面是默认值）：

| 事件 `kind` | 分值 | 说明 |
| --- | --- | --- |
| `vote_agreed` | +10 | 案件定案，我的票与结论一致 |
| `vote_disagreed` | +3 | 案件定案，我的票与结论不一致 |
| `quality_correct` / `quality_wrong` | +5 / −5 | 质控题，投完当场结算 |
| `daily_first` | +2 | 每天第一张计入表决的票 |
| `streak_7` | +10 | 连续 7 天都有投票（每满 7 天一次） |
| `opinion_liked` | +5 | 一条观点的赞数第一次达到 3 |
| `exam_passed` | +20 | 第一次通过入门考试 |
| `overturn_adjust` | ±7 | 案件被改判后，按新结论补差 |

弃权、上下文不足、回避、练习都不给分。总分不会低于 0。

投票响应里的 `reward` 只含当场结算的部分：

```jsonc
"reward": { "points": 7, "total": 142,
            "items": [{ "kind": "quality_correct", "points": 5, "label": "质控题答对" },
                      { "kind": "daily_first", "points": 2, "label": "今日首票" }] }
```

案件定案时的加分通过实时事件 `jury.points.changed` 通知：`{ delta, total, kind, label, case_id }`。`label` 是可以直接显示的文字，以后新增 kind 时客户端不用跟着改。案件详情的 `my_vote` 新增 `points`（这个案件累计给了我多少分）。

`GET /jury/me/points?limit=20&offset=0` → `{ "total": 142, "items": [{ "kind", "label", "points", "case_id", "created_at" }], "count": 57 }`，明细按时间倒序。

**层级**（`config.tiers[].requirements` 下发；默认值）：

| | 已结票数 | 一致率 | 质控正确率 | 在任天数 |
| --- | --- | --- | --- | --- |
| 见习 → 正式 | ≥ 15 | ≥ 70% | ≥ 70%（答过 ≥ 3 道才看） | ≥ 14 |
| 正式 → 资深 | ≥ 50 | ≥ 80% | ≥ 80%（答过 ≥ 8 道才看） | ≥ 60 |

- 每次有票出结论时自动检查晋升。
- 降级看最近 30 张已结的票（不足 15 张不判断）：资深一致率 < 70% 降为正式，正式 < 60% 降为见习。管理员直接任命的层级同样适用这条。
- 原有的自动暂停不变（已结 ≥ 20 张且一致率 < 60%，或质控 ≥ 5 道且正确率 < 60%）。

`/jury/me.tier_progress`：

```jsonc
{ "next_tier": "regular",      // 已是资深时为 null
  "requirements": [
    { "key": "votes_settled", "label": "已出结论的票", "current": 9, "target": 15, "ok": false },
    { "key": "agreement_rate", "label": "一致率", "current": 0.78, "target": 0.7, "ok": true },
    { "key": "quality_rate", "label": "质控正确率", "current": null, "target": 0.7, "ok": true, "note": "答满 3 道后开始计算" },
    { "key": "days_served", "label": "在任天数", "current": 20, "target": 14, "ok": true }
  ] }
```

### 2.7 成就

`GET /jury/me/achievements` → `{ "items": [{ "key", "label", "description", "earned_at": "…" | null, "current": 7, "target": 10 }] }`

默认的成就：`first_vote` 第一案、`votes_10` / `votes_50` / `votes_100`（已出结论的票数）、`sharp_eye`（已结 ≥ 30 张且一致率 ≥ 90%）、`streak_7` / `streak_30`（连续参与天数）、`quality_10`（质控题连续答对 10 道）、`liked_10`（观点累计获赞 10 个）、`exam_first_try`（考试一次通过）、`tier_regular`、`tier_senior`。新获得时发实时事件 `jury.achievement.earned`：`{ key, label }`。

### 2.8 排行榜

`GET /jury/leaderboard?period=week|month|all&limit=50` →

```jsonc
{
  "period": "week",              // 自然周 / 自然月（UTC+8），all 为累计
  "items": [
    { "rank": 1, "points": 86, "tier": "senior", "votes": 14, "me": false,
      "user": { /* UserPublic */ } },           // 公开展示身份的委员
    { "rank": 2, "points": 71, "tier": "regular", "votes": 11, "me": true,
      "user": null }                             // 没公开的显示为「匿名委员」；me 为 true 时客户端可显示「我」
  ],
  "me": { "rank": 2, "points": 71, "tier": "regular", "votes": 11, "me": true, "user": null },
                                                 // 和榜单行同形；不是委员或本期没有得分时为 null
  "generated_at": "…"
}
```

只列在任委员，本期风纪值为 0 的不上榜。服务端缓存 60 秒。

### 2.9 公开展示委员身份

`PUT /jury/me/settings`，body `{ "badge_visible": true }` → `JuryStatus`。默认不展示。

用户对象（`UserPublic`，出现在资料卡、成员列表、消息发送者等所有地方）新增 `jury_badge`：`"trainee"` / `"regular"` / `"senior"` / null。只有在任、且本人选择公开时才有值。

### 2.10 实时事件

| 事件 | data | 发给谁 |
| --- | --- | --- |
| `jury.points.changed` | `{ delta, total, kind, label, case_id }` | 本人 |
| `jury.achievement.earned` | `{ key, label }` | 本人 |
| `jury.status.changed` | `{}` | 本人。晋升、降级、暂停、续任、任期届满也会发 |

### 2.11 实现上的细节

- **「每天」有两种口径。** 评审上限、回避上限、练习上限按最近 24 小时滚动，不在零点恢复；恢复时间看 `daily_resets_at` / `recuse_resets_at` / 练习场的 `resets_at`，429 `jury_daily_limit`、`recuse_limit`、`practice_daily_limit` 的 `detail` 里也带 `resets_at`。每日首票、连续天数、排行榜的周 / 月按 UTC+8 自然日。
- **练习题和计分的题不重叠。** 已核对的题固定分出约三分之一只给练习场，其余只用于质控和考试；未核对的题全部只给练习场。`round.total` 是练习场能出的题数。评审中的质控题也不会出我在考试或练习里见过的题。对计分题调 `POST /jury/practice/{question_id}/answer` 返回 404。
- **自动任命的范围。** `auto_appoint` 只对没有成员记录、或正在审批队列里的人生效；被拒绝或卸任过的人通过考试后仍进审批队列。
- **晋升保护。** 累计条件都满足、但最近的一致率低于新层级降级线的人不晋升，所以 `tier_progress` 里可能全部 `ok` 却还没升。
- **弃权过多的暂停**只在刚投出「拿不准」或「上下文不足」的那一票上判断。
- **加分明细。** `daily_first`、`streak_7` 的明细没有 `case_id`（为 null），不计入 `my_vote.points`。
- **排行榜**连同 `me` 一起缓存 60 秒。
- **另外的错误码**：投票带了不在 `config.categories` 里的类别，422 `invalid_category`；`PUT /jury/me/settings` 在从没当过委员时，403 `not_juror`；交卷时场次不存在或不是本人的，404（`detail.code` 为 `exam_not_found`）。

### 2.12 后台接口

| 接口 | 说明 |
| --- | --- |
| `GET /admin/jury/questions?verified=&source=&q=&limit&offset` | 题库。每项：案件摘要、标准答案（结论 / 等级 / 类别）、说明、`verified`、来源（`admin` / `consensus` / `legacy`）、`pool`（`practice` 只给练习场 / `scored` 用于考试和质控 / null 未核对）、被答次数和正确率 |
| `GET /admin/jury/questions/{case_id}` | 题目内容（匿名视图）+ 以上字段 |
| `PUT /admin/jury/questions/{case_id}` | `{ in_bank?, verified?, outcome?, level?, category?, explanation? }`。把任意已结且允许入库的案件加入 / 移出题库、核对、改标准答案和说明 |
| `GET /admin/jury/exams?passed=&limit&offset` | 考试记录：用户、分数、是否通过、时间 |
| `GET /admin/jury/members` | `JuryMemberRead` 新增 `points`、`exam_score`、`exam_passed_at`、`abstain_rate`、`recuse_rate`、`quality_recused`（在质控题上回避的次数，用来发现专门躲质控的人）、`badge_visible`、`days_served` |
| `POST /admin/jury/members/{user_id}/review` | 新增 `action: "adjust_points"`（`{ points: ±n, note }`） |

## 3. 三期

三期上线后 `config.capabilities` 追加：`panel`、`appeal_review`、`machine_cases`、`precedents`、`hall`，以及（做完的话）`reconsider`。

`/jury/me` 新增 `senior`：资深委员为 `{ "panel": 2, "appeals": 1 }`（现在能分给我的合议 / 复审件数，复审里含举报人复议），其他人为 null。有变化时同样发 `jury.queue.changed`。

`GET /jury/config` 三期新增的部分：

```jsonc
{
  "panel": { "seats": 3, "consensus_votes": 2, "timeout_hours": 48 },
  "appeals": { "seats": 3, "consensus_votes": 2, "timeout_hours": 72 },
  "reconsider": { "window_days": 7, "text_min": 10 },          // 只有 capabilities 里有 reconsider 时才有
  "hall": {
    "title_max": 60, "content_max": 5000, "comment_max": 1000,
    "posts_per_day": 3, "comments_per_day": 50,
    "vote_days": 7, "quorum_ratio": 0.3, "quorum_min": 3,
    // 通过线：服务端按整数比较 yes × pass_den >= (yes + no) × pass_num，恰好 2/3 算通过；pass_ratio 只是给展示用的近似值
    "pass_num": 2, "pass_den": 3, "pass_ratio": 0.6667,
    "guidelines": "……",                                          // 要展示的守则，纯文本，含换行
    "report_reasons": ["人身攻击", "讨论未结案件", "泄露身份或隐私", "与风纪无关", "其他"]
  }
}
```

`points` 多三个键：`panel_agreed`（15）、`panel_disagreed`（5）、`review_vote`（8，每张申诉复审 / 复议票）。

### 3.1 资深合议

**什么案件进合议**：委员投票无共识（5 票没达到 4 票一致）、`need_context` 累计 2 票、或 72 小时没凑够票而超时的案件。

**进合议的前提**：此刻至少有 3 位资深委员可以参加（在任、没受处罚、通过回避规则、没在原审里投过票）。不够就直接转管理员，和现在一样。

**规则**：3 个席位；只能投 `none` / `l1` / `l2` / `l3`（违规票可带 `category`）或 `recuse`，不能弃权；两票一致即定案，等级取违规票的较低中位数；48 小时内没定案转管理员。合议定案的处罚同样受 7 天封禁上限约束。

`GET /jury/panel/next` → `{ "case": PanelCaseView | null }`。只有资深委员能调（否则 403 `not_senior`）。

`PanelCaseView` = `JuryCaseView` 的全部字段，加：

```jsonc
{
  "origin": {
    "reason": "no_consensus",                     // no_consensus / need_context / timeout
    "tally": { "none": 2, "l1": 1, "l2": 2, "l3": 0, "abstain": 0, "need_context": 0 },
    "opinions": [ /* Opinion[]，只读；这里每条都带 vote */ ]
  },
  "panel": { "seats": 3, "votes_cast": 1, "expires_at": "…" }
}
```

`POST /jury/panel/{case_id}/vote`，body 同普通投票（`vote` ∈ `none` / `l1` / `l2` / `l3` / `recuse`）→ `{ case_id, settled, case, reward }`，同普通投票的响应。

投完之后，合议委员就是这个案件的参与者：可以用 `GET /jury/cases/{case_id}` 看详情、在观点区发言，`/jury/votes` 里也有这一条（每项新增 `vote_stage`：这一票是哪一轮投的，`"jury"` / `"panel"`；原有的 `stage` 仍是案件当前所处的阶段）。案件详情的 `verdict.source` 为 `panel`，`tally` 新增 `panel`：`{ "none": 0, "l1": 0, "l2": 2, "l3": 1 }`。

风纪值：合议票与结论一致 +15，不一致 +5。

### 3.2 申诉复审

开关 `appeals`，默认关。关闭时申诉全部直接到管理员（现状）。

**打开后，同时满足这些条件的申诉先进资深复审**，其余仍直接到管理员：

- 处罚来源是委员 / 合议定案，或机器自动处罚；
- 内容是群聊消息或公开动态（私聊、资料、名片等都不算）；
- 类别不是色情 / 涉未成年；
- 处罚不是永久封禁，也不是超过 7 天的封禁；
- 此刻至少有 3 位资深委员可以参加（回避规则同合议，并排除申诉人本人）。

**规则**：3 个席位，每人投 `uphold`（维持）/ `reduce`（减轻）/ `revoke`（撤销）；两票相同即出结果，三票各不相同取 `reduce`；72 小时没出结果转管理员。

| 结果 | 效果 |
| --- | --- |
| `uphold` | 申诉驳回 |
| `reduce` | 保留违规记录和记分，解除这条处罚带来的禁言 / 封禁 |
| `revoke` | 撤销处罚和记分；来自案件的，案件按改判处理（1.10），能恢复的内容恢复 |

管理员随时可以在后台改掉复审结果。每条处罚仍然只能申诉一次。

`ViolationRead`（`GET /moderation/me` 里的处罚记录）新增：

| 字段 | 说明 |
| --- | --- |
| `appeal_stage` | `senior` 资深复审中 / `admin` 等管理员 / null |
| `appeal_result` | `upheld` / `reduced` / `revoked` / null |
| `appeal_decided_by` | `senior` / `admin` / null |

`appeal_status`、`appeal_note` 保持原义（`reduce` 时 `appeal_status` 为 `accepted`）。结果出来时发已有的 `moderation.violation.updated`，通知中心也会有一条。

资深委员侧：

`GET /jury/appeals/next` → `{ "review": AppealReviewView | null }`

```jsonc
{
  "review_id": 12,
  "type": "appeal",                          // appeal 申诉复审 / reconsider 举报人复议（见 3.7）
  "kind": "message",                         // message / post
  "source": "jury",                          // jury / panel / machine（注意 ViolationRead.source 里机器处罚叫 auto_* ，两处取值不同）
  "category": "harassment", "category_label": "辱骂骚扰", "level": 2,
  "penalty": { "kind": "mute", "hours": 24 },
  "messages": [ /* 来自案件的：匿名上下文，同 JuryCaseView.messages */ ],
  "post": null,
  "content": { "text": "……", "image_count": 0 },   // 机器处罚没有案件快照，只有被处罚的那条内容
  "origin": { "tally": { /* … */ }, "opinions": [ /* 只读 */ ] },   // 机器处罚时为 null
  "appeal_text": "我只是在开玩笑……",
  "appealed_at": "…", "expires_at": "…",
  "seats": 3, "votes_cast": 1
}
```

`POST /jury/appeals/{review_id}/vote`，body `{ "vote": "uphold" | "reduce" | "revoke", "note"?: "≤500 字" }` → `{ review_id, settled, outcome, reward }`

`GET /jury/appeals/mine?limit&offset` → 我参与过的复审：`{ items: [{ review_id, type, kind, source, category_label, level, penalty, my_vote, voted_at, outcome, settled_at }], total }`。`outcome` 未出结果时为 null；复议的 `category_label` / `level` / `penalty` 为 null。

错误码：403 `not_senior`；404 没分给我；409 已投过。风纪值：每张复审票 +8。

### 3.3 机器拿不准的内容作为案源

开关 `machine_review`，默认关。打开后，机器判为「拿不准」的内容里，**群聊里的纯文本消息和公开动态的文字**会作为没有举报人的案件进委员队列。图片、私聊、色情类不进。

- 限量：每天进队列的上限 = 在任委员数 × 2（至少 5），到上限后当天不再收。
- 排在所有真实举报的案件后面。
- 这类案件超时或无共识时不转管理员：有资深委员就进合议，否则直接以「未作处理」结案，不记任何处罚。
- 委员看到的 `JuryCaseView`（以及 `PanelCaseView`、`JuryCaseDetail`、公示栏的条目和详情）新增 `case_origin`：`report` / `machine`。机器来源的案件 `report_count` 为 0，`reasons` 是一项 `{ "reason": "机器筛查 · 辱骂骚扰", "count": 1 }`。
- 认定违规后和举报案件一样：撤回内容、按阶梯处罚、进公示栏（群聊消息不带内容）。

### 3.4 判例

`GET /jury/precedents?category=&limit&offset`（所有登录用户）→ `{ items: Precedent[], total }`

```jsonc
{
  "id": 5, "case_id": 42,
  "title": "把别人的账号信息发到群里，即使是玩笑也算泄露隐私",
  "summary": "裁判要旨……",                 // ≤1000 字
  "kind": "message", "outcome": "violation",
  "category": "privacy", "category_label": "泄露隐私", "level": 3,
  "by": "senior",                           // senior / admin
  "mine": false,                            // 是不是我创建的（能否编辑 / 撤销）
  "created_at": "…"
}
```

`GET /jury/precedents/{id}` → 上面的字段，加：

- `content`：`{ text, image_count, created_at }` 或 null，和 `content_hidden_reason` 一起，规则与公示栏详情完全相同（全站可见的部分）。
- `case`：在任委员为 `{ "messages": [ /* 同 JuryCaseView.messages */ ], "post": { /* 同 JuryCaseView.post */ } | null }`，其他人为 null。

和案件互相关联的字段：

| 位置 | 字段 |
| --- | --- |
| `JuryCaseDetail`（1.5） | `precedent`：`{ id, title, mine }` 或 null；`can_mark_precedent`：我现在能不能把它标为判例（资深、已定案、未改判、类别允许、还不是判例） |
| 公示栏的案件（1.8） | `precedent_id`：数字或 null |

`POST /jury/precedents`，body `{ case_id, title: "≤60 字", summary: "≤1000 字" }` → `Precedent`。这里的 `case_id` 是**评审编号**（委员侧案件详情里的那个）；返回的 `Precedent.case_id` 是公开编号。资深委员可以把**自己投过票的已定案件**标为判例；管理员在后台可以标任何已定案件。`PUT /jury/precedents/{id}`、`DELETE /jury/precedents/{id}`：作者本人。

不能成为判例的：未定案、被改判、色情 / 涉未成年类、质控题本身。判例会自动进练习场的题库。

错误码：403 `not_senior` / `not_voted`；409 `precedent_exists`；422 `case_not_eligible`。

### 3.5 议事厅

实名，全站可读。三类帖子：

| `kind` | 谁能发 | 说明 |
| --- | --- | --- |
| `announcement` | 管理员（后台） | 公告 |
| `proposal` | 正式、资深委员 | 规则提案，在任委员表决 7 天 |
| `discussion` | 正式、资深委员 | 讨论 |

评论：所有在任委员（含见习）。表决：所有在任委员，结束前可以改票。

提案结束时：参与表决的人数 ≥ 在任委员数的 30%（至少 3 人）且赞成 ≥ 2/3，状态变 `passed`（通过，待管理员采纳）；否则 `rejected`。管理员可以把 `passed` 的提案标为 `adopted` / `declined` 并写说明。规则本身仍由管理员修改。

`GET /jury/hall/posts?kind=&status=&sort=active|new&limit&offset` → `{ items: HallPost[], total }`。置顶始终在前；其余 `sort=active`（默认）按最后活动时间倒序，`sort=new` 按发帖时间倒序。

```jsonc
{
  "id": 9, "kind": "proposal",
  "title": "建议把刷屏的默认等级从轻微调到一般",
  "content": "……",                           // 列表里是前 200 字，详情里是全文
  "author": { /* UserPublic */ }, "author_tier": "senior",   // 公告的 author_tier 为 null
  "status": "open",                           // open / passed / rejected / adopted / declined / closed
  "pinned": false,
  "vote": { "yes": 6, "no": 2, "ends_at": "…", "my_vote": "yes", "can_vote": true,
            "quorum": 4, "pass_num": 2, "pass_den": 3 },   // 非提案为 null；quorum 是这个提案需要的最少表决人数
  "admin_note": null,
  "comment_count": 12,
  "can_comment": true, "mine": false,
  "can_edit": false, "can_delete": false,      // 作者本人才可能为 true；提案有人表决后 can_edit 为 false
  "reported": false,                           // 我是否举报过
  "created_at": "…", "updated_at": "…"
}
```

| 接口 | 说明 |
| --- | --- |
| `GET /jury/hall/posts/{id}` | 详情 |
| `POST /jury/hall/posts` | `{ kind: "proposal" \| "discussion", title: "≤60 字", content: "≤5000 字" }` |
| `PUT /jury/hall/posts/{id}` | 作者本人；提案有人表决后不能再改 |
| `DELETE /jury/hall/posts/{id}` | 作者本人 |
| `POST /jury/hall/posts/{id}/vote` | `{ vote: "yes" \| "no" }` → `HallPost` |
| `GET /jury/hall/posts/{id}/comments?limit&offset` | `{ items: HallComment[], total }`。**平铺**、时间正序（所以根评论一定排在它的回复前面），用 `parent_id` 归组；`HallComment`：`{ id, parent_id, author, author_tier, content, state, mine, reported, created_at, reply_to: UserPublic \| null }`，回复只有一层；`state` 取值同观点区（visible / deleted / hidden），非 visible 时 `content` 为 null |
| `POST /jury/hall/posts/{id}/comments` | `{ content: "≤1000 字", parent_id? }` |
| `DELETE /jury/hall/comments/{id}` | 作者本人 |
| `POST /jury/hall/reports` | `{ target_type: "post" \| "comment", target_id, reason, details? }`；理由列表在 `config.hall.report_reasons`。任何登录用户都可以举报；重复举报 409 `already_reported`；举报自己的 400 `cannot_report_own` |

帖子和评论发表时过文本审核（明确违规才拒绝，422 `content_rejected`）。每人每天最多 3 帖、50 条评论（429 `hall_rate_limited`）。`config.hall` 下发这些数字和一段要展示的守则（含「不要讨论具体的未结案件」）。

错误码：403 `not_juror` / `tier_required`（见习不能发帖）/ `juror_restricted`；409 `vote_closed`；422 `invalid_post`。

实时事件：`jury.hall.changed`：`{ post_id, kind, action }`（`action` ∈ `created` / `updated` / `closed`），发给在任委员。

### 3.5.1 判例与议事厅实现上的细节

判例：

- 不能成为判例的类别是色情、涉未成年和隐私类（和公示栏不带内容的类别是同一套）。
- `content_hidden_reason` 多一个取值 `unpublished`：案件此刻不在公示栏里（定案不足 72 小时、有待处理的申诉等）。要旨照常可读。
- `DELETE /jury/precedents/{id}` 返回 `{ id, status }`；`PUT` 只改传进来的字段。改和撤只认作者本人。
- 一个案件只有一条判例，撤下后重新标会复用同一个 id。案件被改判时判例自动撤下。

议事厅：

- `HallPost` 新增 `state`（`visible` / `hidden`）和 `content_truncated`（列表里内容被截断时为 true）。被多人举报折叠的帖子 `state: "hidden"`、`content` 为 null、`title` 是占位文字。公告不会被自动折叠。
- 自动折叠只看**在任委员**的举报：同一目标被 `config.hall.auto_hide_reports`（3）名在任委员举报后折叠。其他用户的举报照常记录，由管理员处理。每人每天最多举报 `config.hall.reports_per_day`（20）次，超了返回 429 `hall_rate_limited`（带 `resets_at`）。
- `DELETE /jury/hall/posts/{id}`、`DELETE /jury/hall/comments/{id}` 返回 `{ id, state }`；`POST /jury/hall/reports` 返回 `{ target_type, target_id, state }`。`PUT` 帖子只改传进来的字段。
- 提案还在表决时作者可以撤回（删除），出结果后不能删也不能改；被折叠的帖子不能改。
- `author_tier` 是作者此刻的层级，不在任时为 null。排序用的「最后活动时间」只在有新评论时更新，表决和编辑不算。
- 每天的发帖 / 评论上限按最近 24 小时滚动，429 `hall_rate_limited` 的 `detail` 里有 `resets_at`。
- 实时事件 `jury.hall.changed` 的 `action`：发帖 `created`；评论、表决、编辑、置顶、采纳 / 不采纳、折叠 / 恢复都是 `updated`；关闭和删除是 `closed`（删除后重拉会得到 404）。
- 其它错误码：404 `precedent_not_found` / `post_not_found` / `comment_not_found`；403 `not_precedent_author` / `not_post_author` / `not_comment_author`；409 `post_locked`（提案有人表决后修改、出结果后修改或删除、被折叠后修改）、`post_closed`（帖子已关闭或被折叠时评论）、`vote_closed`（已截止，或对非提案表决）；422 `invalid_precedent` / `invalid_comment` / `invalid_vote` / `invalid_target` / `invalid_reason`（举报理由不在列表里，或选「其他」时说明不足 5 个字）。

### 3.6 待审期间折叠：不做

规格里让评估「被多人举报、尚未定案的公开内容是否先折叠」。结论是不做：累计 575 个案件里只有 9 个有 3 人及以上举报，被举报的公开动态一共 2 条，这个功能几乎不会触发；而给聊天消息加折叠标记要改消息序列化的热路径。举报时的机器复审（明确违规当场撤回）继续保留。以后量上来了再做。

### 3.7 重复举报与举报人复议

**堵漏（做）**：一条消息 / 动态在 30 天内已经被认定不违规的，再次举报返回 409 `already_reviewed`（`detail.case_id`）。现在是可以无限重复举报、每次开一个新案件的。

**复议**（排在三期最后实现；做完后 `capabilities` 里才有 `reconsider`，没有时客户端不要显示入口）：举报人对「委员 / 合议认定不违规」的结论可以申请复议，一个案件只能复议一次（不管有几个举报人）。

举报人一侧：`POST /reports/{report_id}/reconsider`，body `{ "text": "≥10 字" }` → `ReportRead`。`ReportRead` 新增：

| 字段 | 说明 |
| --- | --- |
| `can_reconsider` | 现在能不能申请：我的举报、案件由委员 / 合议认定不违规、定案后 7 天内、这个案件还没人申请过 |
| `reconsider_status` | `none` / `pending` / `upheld`（维持原结论）/ `overturned`（改判为违规） |

错误码：404 不是我的举报；409 `reconsider_unavailable`（不满足上面的条件）；422 `text_required`。结果出来时发已有的 `report.resolved`，通知中心也有一条。

资深委员一侧：复用申诉复审的接口。`GET /jury/appeals/next` 返回的 `AppealReviewView` 里 `type: "reconsider"`，此时：

- `messages` / `post`、`origin`（原审票数分布和观点）照常给；
- `verdict`：`{ "outcome": "clean", "source": "jury" | "panel", "closed_at": "…" }`（原结论）；
- `reconsider_text`、`requested_at` 代替 `appeal_text`、`appealed_at`（后两个为 null）；
- `category` / `category_label` / `level` / `penalty` / `content` 为 null。

`POST /jury/appeals/{review_id}/vote` 对复议的取值是 `uphold` / `overturn`；投 `overturn` 时可以带 `level`（1–3）和 `category`，和普通违规票一样。两票相同即出结果；`overturn` 时案件按改判处理（1.10），等级取 `overturn` 票的较低中位数。凑不齐 3 位资深委员或 72 小时没结果时转管理员，管理员用 `POST /admin/jury/reviews/{review_id}/decide` 维持或改判（直接改判案件也会了结这条复议）。

### 3.7.1 合议、复审、机器案源、复议实现上的细节

- **合议**
  - `tally.panel` 只在进过合议的案件上出现；公示栏的 `tally` / `votes` 仍只是原审的票。
  - 合议票计入总票数、一致率和每日投票数；但 `/jury/panel/next` 不受每日 30 件上限和弃权冷却限制。合议里的回避和原审共用每天 5 次的上限。
  - 在这个案件上有过任何分配记录的人（投过、回避过、分到手没投的）都不进它的合议。
  - 错误码：422 `invalid_vote`（合议里投了「拿不准」或「上下文不足」）；409 `already_voted`、`already_recused`。
- **申诉复审**
  - 除 3.2 列的条件外，还有两种申诉直接到管理员：被 7 天上限压住、正在等管理员确认更重处置的处罚；低俗 / 擦边类。
  - 机器处罚被撤销时，被撤回的内容不会自动恢复（只有来自案件的处罚会尝试恢复内容）。
  - 复审转给管理员时（超时或凑不齐人），`moderation.violation.updated` 的 `action` 是 `appeal_escalated`，`appeal_stage` 变成 `admin`。
  - 结果通知在通知中心里是 `type="jury"`，`payload.kind` 为 `appeal_result`；举报人复议的是 `reconsider_result`。
  - 每张复审 / 复议票的 +8 只在投票响应的 `reward` 里返回，不另发 `jury.points.changed`；这条明细的 `case_id` 为 null。
  - `/jury/appeals/mine` 对非资深委员返回空列表。
  - 错误码：404 `review_not_found`；409 `review_closed`（已结束或已转管理员）、`already_voted`；422 `invalid_vote`。
- **机器来源的案件**
  - 只收纯文本的群聊消息（不带图），以及文字拿不准、图片全部被机器放行的公开动态；类别为色情、涉未成年、低俗、擦边的不收；同一条内容已经有过案件（包括已结的）就不再建。
  - 之后有人举报了同一条内容，这个案件就按普通举报案件走（凑不齐人会转管理员）。
  - 没人举报的机器案件在原审或合议超时 / 无共识、又没有合议可用时，以「未作处理」结案：不处罚、不进公示栏；投过票的委员仍会收到 `jury.case.settled`，这些票不计一致率（`my_vote.agreed` 为 null），`verdict.outcome` 为 `clean`、`source` 为 `auto`。
- **重复举报**：`already_reviewed` 对管理员认定的不违规同样生效。
- **复议**：一个案件只能复议一次。转给管理员后，管理员可以维持或改判；结果出来前 `reconsider_status` 一直是 `pending`。复议时不是我的举报返回 404 `report_not_found`。

### 3.8 后台接口

| 接口 | 说明 |
| --- | --- |
| `GET /admin/cases?queue=panel` | 合议中的案件；`CaseRead.queue` 多一个取值 `panel`，新增 `case_origin`（`report` / `machine`）、`panel_outcome`、`reconsider_status` |
| `POST /admin/cases/{id}/resolve` | 对合议中的案件同样可用（管理员直接接手） |
| `GET /admin/jury/reviews?kind=appeal\|reconsider&status=open\|closed&limit&offset` | 资深复审的监控：处罚 / 案件、申诉内容、每位复审委员的票（实名）、结果 |
| `POST /admin/violations/{id}/appeal` | 原有。复审进行中或已出结果时也可以调，管理员的结论覆盖复审结果（复审已经撤销了处罚的，不能再驳回：409 `appeal_already_revoked`） |
| `POST /admin/jury/reviews/{review_id}/decide` | `{ action: "uphold" \| "overturn", level?: 1-3, category?, note? }`。对举报人复议结案（转给管理员的和还在资深阶段的都可以）；对申诉复审返回 409 `appeal_review` |
| `GET /admin/jury/precedents`、`POST`、`PUT /{id}`、`DELETE /{id}` | 判例管理（可操作任何人创建的） |
| `POST /admin/jury/hall/posts` | 发公告：`{ title, content, pinned? }` |
| `GET /admin/jury/hall/posts?kind=&status=&state=&limit&offset`、`GET /admin/jury/hall/posts/{id}` | 后台看帖子；详情带全部评论的原文（含已删除和被折叠的） |
| `POST /admin/jury/hall/posts/{id}/decision` | `{ action: "adopt" \| "decline" \| "close" \| "delete" \| "pin" \| "unpin", note? }` |
| `POST /admin/jury/hall/comments/{id}/moderate` | `{ action: "delete" \| "restore", note? }`；作者自己删的不能恢复（409 `comment_not_restorable`） |
| `GET /admin/jury/hall/reports?status=`、`POST /admin/jury/hall/reports/{id}/handle` | `{ action: "delete" \| "dismiss", note? }` |
