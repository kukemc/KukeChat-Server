# 好友亲密度与好友成就

两个好友之间有一个**亲密度**：多聊天、送礼物、生日送礼都会涨，但每天能涨的有上限，涨得很慢。亲密度对应一个**等级**（点头之交 → 永恒之友）。
好友之间还有一套**好友成就**：深夜互聊、一天互发几百条、连续聊天多少天……达成了解锁一枚**徽章**，徽章可以挂在好友的名字旁。
**好友亲密榜**是我自己的好友按亲密度排的名次。

亲密度和徽章属于**一对好友**，双方看到的数值一样；挂哪一枚徽章各选各的。

所有路径前缀 `/api/v1`，都要求登录。错误的 `detail` 是 `{ code, message }`，`message` 可以直接展示。「今天」「每天」一律按 UTC+8。

## 1. 亲密度怎么涨

每对好友每天（UTC+8 自然日）：

| 来源 | key | 规则（默认值） | 单项上限 |
| --- | --- | --- | --- |
| 互相聊天 | `chat` | 当天双方都在私聊里发过消息 +5，之后每「一来一回」（两边条数里较少的那个）+1 | 25 |
| 送礼物 | `gift` | 每送一次（`docs/gifts.md`，礼物或商品都算，谁送谁都算）+3，每 10 币再 +1 | 15 |
| 连续互动 | `streak` | 连续 3 天以上每天都互相聊过，当天 +3 | 3 |
| 生日礼物 | `birthday` | 在对方生日那天（`GiftRecord.on_birthday`）送礼 +30。每人每年一次（我给你过生日、你给我过生日各算一次） | — |

- 前三项加起来每天最多 **40**（`daily_cap`）；**生日礼物不占这个上限**。
- 只有一方在说话涨不了亲密度（聊天这一项按较少的那一方算）。
- 只算**现在是好友**的两个人之间的**私聊**：群聊、临时会话（不是好友）不算；系统消息不算；礼物消息按礼物算，不重复按聊天算。撤回不扣回。
- 删好友不清数据：重新加回好友，亲密度和徽章都还在；不是好友期间不涨。
- 数值都是后台可改的设置（第 6 节），客户端从 `GET /intimacy/rules` 读，不要写死。

### 1.1 等级

| 等级 | 名称 | 门槛 |
| --- | --- | --- |
| 0 | 点头之交 | 0 |
| 1 | 初识 | 20 |
| 2 | 熟络 | 100 |
| 3 | 好友 | 300 |
| 4 | 密友 | 700 |
| 5 | 挚友 | 1500 |
| 6 | 知己 | 3000 |
| 7 | 莫逆之交 | 6000 |
| 8 | 灵魂之友 | 10000 |
| 9 | 永恒之友 | 16000 |

## 2. 好友成就

一个**系列**盯着一项统计，统计跨过一个门槛就解锁这一系列的下一级。每一级是一枚徽章，有自己的名字和外框 `tier`（1 铜 / 2 银 / 3 金 / 4 彩虹）。
`icon` 是客户端内置图形的名字，不认识的画通用徽章。已经解锁的不会收回（以后改门槛也不收回）。

| 系列 key | 名称 | 统计 | 图形 | 各级（门槛） |
| --- | --- | --- | --- | --- |
| `clover` | 友谊幸运草 | 互发消息总数 | `clover` | 友谊幸运草 30 · 三叶之约 300 · 四叶奇迹 1500 · 幸运草原 6000 · 永恒幸运草 20000 |
| `together` | 朝夕相伴 | 互相聊过天的天数 | `sparkle` | 初次相识 7 · 常来常往 30 · 心照不宣 60 · 如影随形 120 · 形影不离 240 · 岁月同行 500 |
| `streak` | 约定 | 最长连续互动天数 | `rings` | 三日之约 3 · 一周不断 7 · 月月相守 30 · 百日同行 100 · 年轮 365 |
| `night` | 月下夜话 | 深夜（0:00–4:59）双方都发过消息的天数 | `moon` | 月下夜话 1 · 夜猫搭档 7 · 星河守望 30 · 永夜同盟 100 |
| `dawn` | 晨光 | 清晨（5:00–7:59）双方都发过消息的天数 | `sun` | 第一缕晨光 1 · 早起二人组 7 · 晨曦之约 30 · 永昼之光 100 |
| `chatter` | 话匣子 | 单日互发消息最多的一天 | `balloons` | 聊不停 100 · 话匣子 300 · 停不下来 666 |
| `weekend` | 周末搭子 | 周六日互相聊过天的天数 | `weekend` | 周末见 4 · 周末搭子 20 · 假日常客 52 · 无尽周末 150 |
| `gifts` | 心意 | 互相送礼次数 | `gems` | 第一份心意 1 · 礼尚往来 10 · 心意满满 50 · 宝藏收藏家 200 |
| `birthday` | 生日派对 | 生日礼物次数（每人每年最多算一次） | `cake` | 生日快乐 1 · 年年有你 3 · 岁岁相伴 6 |
| `bond` | 羁绊 | 亲密度 | `hearts` | 友谊的火花 1000 · 默契之信 5000 |
| `oldfriend` | 老朋友 | 成为好友的天数 | `house` | 新邻居 30 · 老朋友 100 · 一周年 365 · 千日之交 1000 |

**隐藏成就（彩蛋）**：解锁第一级之前，接口不给名字、条件、门槛，图形也统一给 `unknown`（客户端画问号），达成那一刻才揭晓。

| 系列 key | 名称 | 条件 | 图形 |
| --- | --- | --- | --- |
| `newyear` | 零点钟声 · 岁岁年年 | 元旦当天互相聊过天 1 / 3 次 | `firework` |
| `sunmoon` | 千年之后 · 长夜将尽 | 同一天里深夜、清晨双方都聊过，1 / 10 天 | `sunmoon` |
| `pinkie` | 以糖为誓 | 连续互动 30 天 | `rings` |
| `rainboom` | 七色音障 | 单日互发 1000 条 | `rainbow` |
| `discord` | 混沌茶会 | 单日深夜互发 200 条 | `chaos` |
| `harmony` | 和谐之元 | 集齐 6 个不同的台面系列（隐藏的不算） | `harmony` |
| `magic` | 友谊之书 | 亲密度 15000 | `horn` |

- 「互相聊过天」= 当天双方都在私聊里发过至少一条。「连续互动」断一天就从 1 重新数，最长纪录留着。
- 「成为好友的天数」没有聊天去触发，服务端每小时扫一遍补上。

## 3. 用户侧接口

### 3.1 `GET /intimacy/rules`

```jsonc
{ "enabled": true,
  "daily_cap": 40,
  "sources": [ { "key": "chat", "label": "互相聊天", "cap": 25, "detail": "当天双方都发过消息 +5，之后每一来一回 +1" } /* gift / streak / birthday（cap 为 null） */ ],
  "levels": [ { "level": 0, "name": "点头之交", "threshold": 0 } ],
  "series": [ { "key": "clover", "hidden": false, "name": "友谊幸运草", "condition": "你们互相发的消息累计 N 条", "icon": "clover",
                "levels": [ { "level": 1, "tier": 1, "name": "友谊幸运草", "threshold": 30 } ] },
              { "key": "newyear", "hidden": true, "name": null, "condition": null, "icon": "unknown", "levels": [ { "level": 1, "tier": 3, "name": null, "threshold": null } ] } ],
  "total_badges": 53 }
```

`enabled = false` 时亲密度不再涨（读接口照常返回）；客户端自己把入口藏起来。

### 3.2 `GET /intimacy/friends` —— 好友亲密榜

我的**全部**好友（没有任何互动的也在，亲密度 0），按亲密度从高到低，一样多时先成为好友的在前。也是客户端画「好友名字旁的徽章」的数据源。

```jsonc
{ "enabled": true, "daily_cap": 40, "total": 2,
  "items": [ {
    "rank": 1,
    "friend": { /* UserPublic */ },
    "intimacy": 1260, "level": 4, "level_name": "密友",
    "today_gained": 12,          // 今天涨了多少（含生日加成）
    "streak_days": 5,            // 当前连续互动天数（最近一次是今天或昨天才算，否则 0）
    "badge": { "series": "night", "series_name": "月下夜话", "level": 2, "name": "夜猫搭档", "tier": 2, "icon": "moon", "unlocked_at": "…" },   // 我在 TA 名字旁挂的那枚；不挂 / 一枚都没有是 null
    "badge_count": 7,            // 已解锁的徽章数
    "new_badges": 1,             // 其中我还没看过的
    "friends_since": "…" } ] }
```

### 3.3 `GET /intimacy/friends/{friend_id}`

```jsonc
{ "friend": { /* UserPublic */ }, "friends_since": "…", "friend_days": 120,
  "intimacy": 1260,
  "level": { "level": 4, "name": "密友", "threshold": 700, "next_level": 5, "next_name": "挚友", "next_threshold": 1500 },   // 满级时 next_* 为 null
  "rank": 1, "friend_count": 23,           // TA 在我的好友亲密榜上第几
  "display": "auto",                       // 我在 TA 名字旁挂什么：auto / none / 某个系列的 key
  "badge": { /* BadgeRef，同 3.2 */ },
  "today": { "date": "2026-10-14", "gained": 12, "counted": 12, "cap": 40,   // counted = 计入每日上限的部分（不含生日）
             "chat": 9, "gift": 3, "streak": 0, "birthday": 0,
             "messages_mine": 14, "messages_theirs": 9, "chat_cap": 25, "gift_cap": 15 },
  "stats": { "messages_total": 5320, "messages_mine": 2700, "messages_theirs": 2620, "mutual_days": 84, "streak_days": 5, "best_streak": 21,
             "night_days": 9, "dawn_days": 2, "weekend_days": 20, "best_day_messages": 412, "gift_count": 6, "birthday_gifts": 1 },
  "badge_count": 7, "total_badges": 53,
  "series": [ {
    "key": "clover", "hidden": false, "revealed": true, "name": "友谊幸运草", "condition": "你们互相发的消息累计 {n} 条", "icon": "clover",
    "current": 5320,                       // 这一系列盯着的统计的当前值
    "level": 3, "max_level": 5, "next_threshold": 6000,   // level = 已解锁的最高级；全满时 next_threshold 为 null
    "levels": [ { "level": 1, "name": "友谊幸运草", "tier": 1, "threshold": 30, "unlocked_at": "…", "is_new": false } ] } ] }
```

- `condition` 里的 `{n}` 由客户端换成要展示的门槛。
- 没揭晓的隐藏系列：`revealed = false`，`icon` 是 `unknown`，`name` / `condition` / `current` / `next_threshold` 和每一级的 `name` / `threshold` 都是 null（`tier` 照给，用来画问号徽章的外框）。
- 不是好友（或者是自己）：404 `not_friend`。

### 3.4 `PUT /intimacy/friends/{friend_id}/badge`

`{ "badge": "auto" | "none" | "<series key>" }` → `{ friend_id, display, badge }`。

- `auto`：自动挂外框最高的那枚，一样高挑最近解锁的。指定系列：挂这一系列已解锁的最高级（之后升级了自动跟着变）。
- 只影响我自己看到的；对方给我挂什么是对方的事。
- 422 `invalid_badge`（没有这个系列）、422 `badge_locked`（这一系列一级都还没解锁）、404 `not_friend`。

### 3.5 `POST /intimacy/friends/{friend_id}/seen`

把我和这个好友的徽章都标成看过（`is_new` / `new_badges` 清零）→ `{ cleared }`。只影响我这一边。客户端打开徽章墙时调。

### 3.6 `GET /intimacy/friends/{friend_id}/history?days=30`

近 N 天（1–90）每天涨了多少，按日期正序，没有记录的日子给 0：

```jsonc
{ "items": [ { "date": "2026-10-14", "gained": 12, "chat": 9, "gift": 3, "streak": 0, "birthday": 0, "mutual": true } ] }
```

## 4. 实时事件

都是**双方各收一份**，内容按收的人的视角（`friend_id` 是对方，`badge` 是收的人自己挂的那枚）。

| 事件 | data | 什么时候 |
| --- | --- | --- |
| `intimacy.updated` | `{ friend_id, intimacy, level, level_name, today_gained, badge }` | 亲密度涨了、解锁了徽章或者等级变了 |
| `intimacy.level.up` | `{ friend_id, friend: { id, display_name, avatar_url }, level, level_name }` | 等级升了 |
| `intimacy.badge.unlocked` | `{ friend_id, friend, badges: [BadgeRef] }` | 解锁了新徽章（一次可能好几枚） |

当天已经涨满、又没有新徽章时不发事件。

## 5. 记账的时机

- 私聊里发消息（`POST /conversations/{id}/messages`、合并转发）和送礼（`POST /gifts`）**提交之后**，服务端另起一个任务记亲密度，不拖慢发送。这一步失败只记日志，消息和礼物不受影响。
- 两个进程同时记同一对：写事务第一条语句就是写，拿着 SQLite 的写锁直到提交；同一个进程里先排队。每条消息只记一次。
- **老好友的回填**：上线后第一次启动时，按库里还留着的私聊消息（每个会话只留最近 1000 条）和送礼记录，给每一对还没有亲密度记录的好友按同样的规则（含每日上限）算一遍、解锁该有的徽章，不发事件（徽章墙上照样是「新」）。只算成为好友之后的。后台可以手动再跑（只处理还没有记录的，不会重复加）。

## 6. 后台

都在 `/admin/intimacy` 下，要求管理员身份。

**隐私边界（硬性）**：后台只能看到每对好友的亲密度、等级、徽章**数量**。看不到聊天条数、深夜 / 清晨互动这类私聊行为的明细，也看不到具体是哪几枚徽章（徽章本身就透露了「什么时候聊天」）。

| 接口 | 说明 |
| --- | --- |
| `GET /admin/intimacy/settings` | 全部设置 |
| `PUT /admin/intimacy/settings` | 只改传了的项。不合法是 422 `invalid_settings`（`detail.field`）。有变化才写审计 `intimacy.settings.update`。另一个进程最多 5 秒后生效 |
| `GET /admin/intimacy/overview` | `{ friend_pairs, bonds, total_intimacy, active_today, gained_today, levels: [{ level, name, threshold, count }], series: [{ key, name, condition, icon, hidden, stat, levels: [{ level, name, tier, threshold, count }] }], total_badges, unlocked_badges, backfill }`。`count` 是全站有多少对达到 / 解锁了；没有记录的好友算 0 级。`backfill` 是首次回填的结果 `{ at, pairs, badges, skipped }`，还没跑过是 null |
| `GET /admin/intimacy/users/{user_id}` | `{ user, items: [{ friend, friends_since, intimacy, level, level_name, badge_count, updated_at }], total }`，这个人的全部好友。404 `user_not_found` |
| `POST /admin/intimacy/adjust` | `{ user_id, friend_id, delta, note }`：手动增减（±1 到 ±100000，不受每日上限，最低减到 0），必须写原因（≤200 字）。返回 `{ intimacy, level, level_name, new_badge_count }`（只给新解锁了几枚，不给是哪几枚）。双方会收到 `intimacy.updated`。写审计 `intimacy.adjust`。422 `invalid_delta` / `invalid_note`，404 `not_friend` |
| `POST /admin/intimacy/backfill` | 再跑一遍回填，返回 `{ pairs, badges, skipped }`。写审计 `intimacy.backfill` |

设置项（括号里是范围）：

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | true | 关掉后不再涨，读接口照常 |
| `daily_cap` | 40（1–1000） | 聊天 + 送礼 + 连续互动每天合计上限 |
| `chat_mutual` | 5（0–100） | 当天双方都开口的那一下 |
| `chat_per_round` | 1（0–100） | 每一来一回 |
| `chat_cap` | 25（0–1000） | 聊天每天上限 |
| `gift_base` | 3（0–100） | 每次送礼 |
| `gift_coins_per_point` | 10（1–100000） | 每多少币再 +1 |
| `gift_cap` | 15（0–1000） | 送礼每天上限 |
| `streak_min` | 3（1–365） | 连续几天起有加成 |
| `streak_bonus` | 3（0–100） | 连续互动加成 |
| `birthday_bonus` | 30（0–1000） | 生日礼物加成 |

## 7. 表

`friend_bonds`（一对好友一行，`user_low_id < user_high_id`，累计和各自挂的徽章都在上面）、`friend_bond_days`（一对好友的一天）、`friend_badges`（一行一枚已解锁的徽章，带双方各自的「看过」标记）。都是新表，启动时自动创建。目录（等级表、成就系列）写在 `app/services/intimacy_catalog.py`。
