# 账号等级

账号等级回答的是「这个人在 KukeChat 待了多久、有多活跃」。它和群等级是两套东西：群等级只在一个群里有效，账号等级跟着账号走，全站可见。

设计参照 QQ 等级：等级由**活跃天数**决定，升得很慢；图标是四进制的星星 / 月亮 / 太阳 / 皇冠。

所有路径前缀 `/api/v1`，都要求登录。新错误的 `detail` 是 `{ code, message }`。

## 1. 规则

### 1.1 活跃天数

每天（UTC+8 自然日）最多能攒 `daily_cap`（默认 2.0）个活跃天，由两部分相加：

**基础（在线时长）**

| 当天在线 | 活跃天 |
| --- | --- |
| 满 2 小时 | 1.0 |
| 满 30 分钟、不到 2 小时 | 0.5 |
| 不到 30 分钟 | 0 |

在线 = 有实时连接（扩展、桌面端、移动端任意一个开着）。服务端每 5 分钟给当时在线的人记 5 分钟，多端同时在线不重复算。

**加成（真的在用，而不是挂着）**，要先拿到基础的活跃天（至少 0.5）才生效：

| 条件 | 加成 | key |
| --- | --- | --- |
| 当天发言 ≥ 20 条 | +0.3 | `messages` |
| 当天在任意一个群签到 | +0.1 | `checkin` |
| 已绑定 CCW 账号 | +0.1 | `ccw_bound` |
| 在任风纪委员，且当天投过票 | +0.2 | `jury` |
| 连续活跃 ≥ 7 天 | +0.1 | `streak_7` |
| 连续活跃 ≥ 30 天（替代上一条） | +0.3 | `streak_30` |

「连续活跃」指连续拿到基础活跃天的天数。全部数值都是配置项，客户端从 `GET /levels/rules` 读，不要写死。

当天的活跃天是实时累计的：条件一满足就记上，不用等到第二天。

### 1.2 等级

升到 N 级需要的累计活跃天数是 `N² + 4N`（1 级 5 天，4 级 32 天，16 级 320 天，32 级 1152 天，64 级 4352 天）。等级上限 256。

图标是四进制的：`1 级 = 1 颗星`，`4 颗星 = 1 个月亮`，`4 个月亮 = 1 个太阳`，`4 个太阳 = 1 个皇冠`。例如 23 级 = 1 太阳 1 月亮 3 星。0 级没有图标。

### 1.3 老用户的起点

上线时按已有的记录给老用户折算一次：有过群发言或群签到记录的每一天算 1 个活跃天。之后都按 1.1 的规则走。

## 2. 用户对象

`UserPublic`（资料卡、成员列表、消息发送者等所有地方）新增：

| 字段 | 说明 |
| --- | --- |
| `account_level` | 整数，0–256 |

客户端用它在昵称旁、资料卡上画等级图标。旧客户端不认识这个字段，不受影响。

## 3. 接口

### 3.1 `GET /levels/rules`

```jsonc
{
  "max_level": 256,
  "formula": "N² + 4N",                 // 给人看的；days_for_level 才是准数
  "icons": [                            // 从大到小
    { "key": "crown", "label": "皇冠", "levels": 64 },
    { "key": "sun",   "label": "太阳", "levels": 16 },
    { "key": "moon",  "label": "月亮", "levels": 4 },
    { "key": "star",  "label": "星星", "levels": 1 }
  ],
  "daily_cap": 2.0,
  "online": [ { "minutes": 120, "days": 1.0 }, { "minutes": 30, "days": 0.5 } ],
  "bonuses": [
    { "key": "messages", "label": "当天发言满 20 条", "days": 0.3, "target": 20 },
    { "key": "checkin",  "label": "当天在任意群签到", "days": 0.1 },
    { "key": "ccw_bound", "label": "已绑定 CCW 账号", "days": 0.1 },
    { "key": "jury",     "label": "风纪委员当天参与评审", "days": 0.2 },
    { "key": "streak_7", "label": "连续活跃 7 天", "days": 0.1, "target": 7 },
    { "key": "streak_30", "label": "连续活跃 30 天", "days": 0.3, "target": 30 }
  ],
  "milestones": [ { "level": 4, "days": 32 }, { "level": 16, "days": 320 }, { "level": 64, "days": 4352 } ]
}
```

### 3.2 `GET /levels/me`

```jsonc
{
  "level": 23,
  "active_days": 631.5,                 // 累计，一位小数
  "level_days": 621,                    // 当前等级需要的累计天数
  "next_level": 24,                     // 满级时为 null
  "next_level_days": 672,               // 满级时为 null
  "days_to_next": 40.5,                 // 满级时为 null
  "icons": { "crown": 0, "sun": 1, "moon": 1, "star": 3 },
  "streak": 12,                         // 连续活跃天数（含今天，如果今天已拿到基础）
  "rank": 37,                           // 全站名次；没有活跃天时为 null
  "today": {
    "date": "2026-10-12",
    "online_minutes": 95,
    "base": 0.5,                        // 今天已拿到的基础活跃天
    "bonuses": [                        // 每一项今天的状态，顺序同 rules.bonuses
      { "key": "messages", "earned": false, "days": 0.3, "current": 12, "target": 20 },
      { "key": "checkin",  "earned": true,  "days": 0.1 }
    ],
    "earned": 0.6,                      // 今天已记入的活跃天
    "cap": 2.0
  },
  "registered_days": 151                // 注册到今天的自然日数
}
```

### 3.3 `GET /users/{user_id}/level`

别人的等级卡，任何登录用户可读：

```jsonc
{ "user_id": 42, "level": 23, "active_days": 631.5, "icons": { "crown": 0, "sun": 1, "moon": 1, "star": 3 },
  "next_level_days": 672, "days_to_next": 40.5, "streak": 12, "rank": 37, "registered_days": 151 }
```

不含当天的在线时长等明细。

### 3.4 `GET /levels/leaderboard?period=all|month&limit=50&offset=0`

```jsonc
{
  "period": "all",                      // all 按累计活跃天；month 按本月（UTC+8 自然月）新增的活跃天
  "items": [
    { "rank": 1, "user": { /* UserPublic */ }, "level": 31, "active_days": 1085.0, "gained": 41.5 }
  ],                                    // gained：本月新增（period=all 时也给）
  "total": 2140,                        // 上榜人数
  "me": { "rank": 37, "user": { /* … */ }, "level": 23, "active_days": 631.5, "gained": 28.0 },   // 没上榜时为 null
  "generated_at": "…"
}
```

`limit` 1–100。封禁中的账号不上榜。服务端缓存 60 秒。

### 3.5 `GET /levels/history?days=30`

近 N 天（1–90）每天拿到的活跃天，按日期正序，没有记录的日子给 0：

```jsonc
{ "items": [ { "date": "2026-10-11", "online_minutes": 184, "base": 1.0, "bonus": 0.4, "earned": 1.4 } ] }
```

### 3.6 细节

- 没有值的可选键（`target`、`current`）直接不出现，不是 null。`today.bonuses` 里发言和两个连续活跃项带 `current` / `target`。
- `registered_days`：注册当天算第 1 天。
- `rank` 来自 60 秒的缓存：新用户刚拿到第一个活跃天时，最多 60 秒内还是 null。
- 排行榜不含机器人和封禁中的账号；月榜的 `gained` 只算每天结算记入的，不含老用户折算和后台手动调整。
- 群发言、签到都按 UTC+8 的自然日归属。下线之后才满足的加成（比如 23:58 发的言），会在第二天第一次结算时补记到前一天。
- 默认数值下，所有加成拿满正好是每日上限 2.0。
- 通知中心里的升级通知：`id` 形如 `account_level:12`，`payload` 是 `{ level, icons }`。
- 错误码：404 `user_not_found`。

## 4. 实时事件与通知

| 事件 | data | 说明 |
| --- | --- | --- |
| `account.level.up` | `{ level, icons, active_days }` | 升级时发给本人 |

升级同时写一条通知中心的通知（`type="account_level"`、`category="system"`）。每升一级都发事件；通知只在升到 4 的倍数（多一个月亮）时写，避免打扰。

## 5. 后台

| 接口 | 说明 |
| --- | --- |
| `GET /admin/levels/users?q=&limit&offset` | 按用户查等级：用户、等级、累计活跃天、连续天数、最近 7 天明细 |
| `POST /admin/levels/users/{user_id}/adjust` | `{ days: ±n, note }`，手动增减活跃天，写审计日志 |
| `GET /admin/levels/settings` | 全部阈值（只读） |
