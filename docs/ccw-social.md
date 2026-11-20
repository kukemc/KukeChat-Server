# 在共创世界上：正在玩 / 名片

油猴脚本 **KukeChat for CCW**（`KukeChat-Client/userscript/`）跑在用户自己的浏览器里，同时带着 CCW 和 KukeChat 两边的登录，
知道用户此刻在 CCW 的哪个页面。它用这组接口把 KukeChat 接进共创世界：

- 上报「正在玩《X》/ 正在创作《X》」，KukeChat 各端和 CCW 页面上都能看到；
- 在别人的 CCW 主页、评论区里显示「这个人有没有 KukeChat、在不在线、正在玩什么」，一键加好友、发私信。

代码：`app/api/v1/ccw_social.py`、`app/services/ccw_activity.py`、`app/services/ccw_social.py`；测试 `tests/test_ccw_social.py`。

## 隐私开关

用户表上两列，都走 `PATCH /users/me`（`UserUpdate`），`GET /users/me`（`UserRead`）返回当前值：

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `share_ccw_activity` | 开 | 公开「正在玩 / 正在创作」。对所有人公开，不只是好友。关掉时立刻清掉已有的活动 |
| `ccw_discoverable` | 开 | 别人能从 CCW 主页、评论区查到我的 KukeChat。关掉后对别人一律当作「没有账号」 |

## 活动（activity）

```http
PUT /api/v1/ccw/activity
{ "kind": "play", "ccw_oid": "6aead64c19ff795b85998e62", "title": "灯塔归航", "cover_url": "https://m.ccw.site/..." }
```

- `kind`：`play`（玩作品）、`create`（在 Gandi 里创作）、`read`（看文章）。
- `ccw_oid`：作品或文章的 24 位 oid；创作一个还没保存过的新作品时可以不传。
- `title`：1–80 字，连续空白会被压成一个空格。
- `cover_url`：只接受 CCW 图床（`*.ccw.site`、`*.xiguacity.cn`、`*.aliyuncs.com`、`*.myqcloud.com`）的 https 地址，其他地址静默丢弃。

这个接口同时是**心跳**：脚本每 60 秒调一次。活动存 Redis（没配 Redis 时存进程内存），**180 秒**没有心跳自动消失。
同一个作品的心跳不改 `started_at`，客户端据此显示「已玩 23 分钟」。限速每人每分钟 30 次（多开标签页也够用）。

`share_ccw_activity` 关着时返回 `null`，什么都不存。

```http
DELETE /api/v1/ccw/activity      # 离开作品页时调；204
GET    /api/v1/ccw/activity/friends
       → [{ "user_id": 12, "activity": ActivityRead }]
```

`ActivityRead`：`kind`、`ccw_oid`、`title`、`cover_url`、`started_at`、`updated_at`、`expires_at`。
**过了 `expires_at` 还没收到新的推送，客户端就当这个活动已经结束**，不需要服务端再发「结束」。

### 实时推送

WebSocket 事件 `ccw.activity.updated`，数据 `{ "user_id": 12, "activity": ActivityRead | null }`，发给本人的好友和本人自己的其他连接。

推送节流：作品（或标题、封面）变了立刻推；同一个作品的心跳最多每 **120 秒**推一次，只为把好友那边的 `expires_at` 往后顺延。
`activity: null` 表示主动结束（调了 DELETE）。

非好友看不到推送，需要时用下面的名片接口按需查。

## 名片

```http
GET /api/v1/ccw/users/{ccw_oid}
→ {
    "ccw_oid": "...",
    "has_account": true,
    "user": UserPublic,          // 对方的 KukeChat 资料（含等级、装扮）
    "online": true,
    "activity": ActivityRead | null,
    "friendship": "friend",      // self / friend / outgoing（我发出的申请待处理）/ incoming（对方发来的）/ none
    "mutual_friend_count": 3,
    "mutual_group_count": 2
  }
```

没绑定 CCW、被封禁（`status = disabled`）、或关掉了 `ccw_discoverable` 的人，统一返回 `has_account: false`，
其余字段为空 —— 不区分这几种情况，免得被用来探测谁关了开关。**看自己永远能看到**（`friendship: "self"`）。

加好友、发私信直接用名片里的 `user.id` 调现有接口：`POST /friends/requests`、`POST /conversations/direct`。

```http
POST /api/v1/ccw/users/batch
{ "oids": ["...", "..."] }       // 1–50 个；重复、格式不对的会被丢掉
→ [{ "ccw_oid", "user_id", "nickname", "avatar_url", "account_level", "online", "activity" }]
```

评论区、粉丝列表这类一次显示很多人的地方用它，只返回有账号且可见的人，顺序跟请求一致。限速每人每分钟 60 次；
单个名片每人每分钟 120 次。

## 安全

- 所有接口都要登录，并和其他 `/api/v1` 接口一样带客户端证明头；脚本用的客户端标识是 `kukechat-ccw/<版本>`。
- 活动是用户自己上报的，和 Discord 的「正在玩」一样只代表本人愿意公开的状态；服务端只保证「只能改自己的」。
- `ccw_oid` 和用户 CCW 账号的对应关系来自现有的绑定流程（评论口令核验），脚本不能自己声称自己是谁。
