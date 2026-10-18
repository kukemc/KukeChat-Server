# 好友火花

好友之间的私聊里有一团「火花」：每天双方都说过话就续上一天，天数越多等级越高；断两天就熄灭，熄灭后短时间内可以花金币重燃。

所有路径前缀 `/api/v1`，都要求登录。错误的 `detail` 是 `{ code, message }`，`message` 可以直接展示。「今天」一律按 UTC+8 自然日。

## 1. 规则

数值是 `app/services/sparks.py` 顶部的常量，客户端从 `GET /sparks/rules`（或详情里的 `rules`）读，不要写死。

- 只算**现在是好友**的两个人的**私聊**。
- 某天双方都至少发了一条消息（文字、图片、表情、语音、转发、礼物、红包都算；系统消息不算，戳一戳是系统消息）＝这一天**续上了**，天数 +1，一天只加一次。只有一方发不算。
- 当天发的消息撤回了不算：如果它是发的人当天唯一的一条，今天的发言和今天的续火都撤回去（更早的日子不追溯）。
- **快熄灭**：昨天没续上。今天续上照样接着数。
- **熄灭**：前天、昨天都没续上，天数清零（服务端每 10 分钟扫一次，把原来的天数记成 `lost_days`）。
- **重燃**：熄灭后 3 天内（从熄灭那天零点算），任何一方可以花 100 金币恢复原来的天数；熄灭后又重新聊起来的，恢复成「原来的 + 新攒的」。每对好友 30 天内只能重燃一次。扣币走商店余额（`spark_relights` 进余额公式，`balance.spark_spent`），要先绑定 CCW。
- **等级**（按当前连续天数）：

| level | 名称 | key | 起始天数 |
| --- | --- | --- | --- |
| 1 | 火苗 | `ember` | 1 |
| 2 | 小火花 | `spark` | 3 |
| 3 | 火花 | `flame` | 7 |
| 4 | 大火花 | `blaze` | 30 |
| 5 | 烈焰 | `inferno` | 100 |
| 6 | 永恒之火 | `eternal` | 365 |

- **里程碑**：升到 2 级及以上的那一刻，私聊里发一条系统消息（`metadata.system_event = "spark.level_up"`，`metadata.spark = { level, level_name, level_key, days }`），比如「你们的火花升级为「大火花」，已经连续 30 天」。重燃也发一条（`spark.relit`）。双方照常收到 `message.created`。
- 记录历史最高（`best_days`）。

## 2. Spark 对象

会话列表、详情、事件里都是同一个结构（按看的人的视角）：

```jsonc
{ "status": "lit",            // none 还没有 / lit 今天已续 / pending 今天还没续 / dying 快熄灭 / out 已熄灭
  "days": 12,                 // 当前连续天数（out / none 为 0）
  "level": 3, "level_name": "火花", "level_key": "flame",   // days 为 0 时 level 0、其余 null
  "today_lit": true, "i_spoke_today": true, "friend_spoke_today": true, "dying": false,
  "best_days": 40,
  "next_level": 4, "next_level_name": "大火花", "next_level_days": 30, "days_to_next": 18,   // 满级为 null
  "lit_on": "2026-10-14",     // 最后续上的那天
  "extinguished_at": null,    // 最近一次熄灭的时刻
  "lost_days": 0, "lost_level_key": null,                  // 能恢复的天数（熄灭后才有）
  "can_relight": false, "relight_block": "not_out",        // not_out / expired / cooldown；能重燃为 null
  "relight_cost": 100, "relight_deadline": null, "relight_cooldown_until": null, "relit_at": null }
```

## 3. 接口

| 接口 | 说明 |
| --- | --- |
| `GET /conversations` 等会话读模型 | 好友私聊带 `spark`（Spark 对象）；没有可画的（没天数，也不在可重燃期限里）为 `null`。群聊、临时会话恒为 `null` |
| `GET /friends/{friend_id}/spark` | `{ friend_id, conversation_id, spark, rules }`。不是好友 404 `not_friend` |
| `GET /sparks` | `{ items: [{ friend_id, spark }] }`：我和所有好友之间有得画的火花，天数从多到少 |
| `GET /sparks/rules` | `{ timezone, levels: [{ level, name, key, min_days }], grace_missed_days, relight_cost, relight_window_days, relight_cooldown_days, rules: [文案] }` |
| `POST /friends/{friend_id}/spark/relight` | 重燃。返回 `{ friend_id, restored_days, spark, balance }`。404 `not_friend`；409 `spark_not_relightable`（`detail.reason` = not_out / expired / cooldown）；403 `ccw_not_bound`；402 `insufficient_balance`（带 `balance` / `price` / `shortfall` / `donate_url`）；503 余额核实不了 |
| `GET /intimacy/friends/{friend_id}` | 好友亲密度详情里也带 `spark`，资料页不用再单独请求 |

## 4. 实时事件

`spark.updated`，双方各收一份：`{ friend_id, conversation_id, reason, actor_id, spark }`。`reason`：

| reason | 什么时候 |
| --- | --- |
| `spoke` | 一方今天第一次发言（另一方还没说） |
| `lit` | 今天续上了（可能同时升级，升级另有系统消息） |
| `recalled` | 今天唯一的一条发言撤回了 |
| `extinguished` | 熄灭（扫描清零时） |
| `relit` | 有人重燃了 |

重燃的人另外收到 `shop.balance.changed`（`change = spark.relit`）。

## 5. 表

`friend_sparks`（一对好友一行，`user_low_id < user_high_id` 唯一：当前天数、最后续上日期、双方各自最后发言日期、历史最高、等级、熄灭时间与熄灭前天数、上次重燃、撤回用的快照）、`spark_relights`（一次重燃一行，进余额公式）。都是新表，启动时自动创建。

和亲密度的关系：亲密度自己的「连续互动」加成和「约定」徽章照旧（规则更严：断一天就重新数），火花不改亲密度数值，只把火花挂进亲密度详情。
