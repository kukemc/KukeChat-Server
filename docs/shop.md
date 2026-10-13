# 商店

用 CCW 金币换 KukeChat 里的装饰（气泡皮肤、头像框、昵称颜色……）。金币是 CCW 社区签到就能拿到的娱乐货币，没有现金价值。

所有路径前缀 `/api/v1`，都要求登录。新错误的 `detail` 是 `{ code, message }`。

## 1. 金币余额

KukeChat 自己不发币、不存币。余额是算出来的：

```
余额 = 这个人给 KukeChat 官方作品累计投出的币 − 在商店里已经花掉的币
```

- 「累计投出的币」由**服务端**通过可插拔的余额来源核实（按用户绑定的 CCW 账号匹配，见 1.1），不采信客户端报的数。按用户投出的数量算，不扣平台回收的那部分。
- 以前给官方作品投过币的人，绑定 CCW 账号后自带余额。
- 必须先绑定 CCW 账号。没绑定的人能逛商店，不能买。
- 充值就是去官方作品投币：客户端打开 `shop.donate_url`（作品页），用户投完回来点「刷新余额」。任何平台（扩展、桌面端、移动端）都是这个流程，客户端不调用任何投币接口。

### 1.1 余额来源

「某个 CCW 账号累计投了多少币」不是这个仓库里的代码去查的。商店只依赖一个接口 —— `app/integrations/coin_ledger.py` 的 `CoinLedger`：

| 方法 | 含义 |
| --- | --- |
| `configured()` | 现在能不能核实余额 |
| `credited_to(account_id)` | 这个账号累计入账多少；确实没有返回 0，核实不了抛 `CoinsUnavailable` |
| `recent_credits(limit)` / `all_credits(fresh=)` | 最近入账的账号 / 全部账号的名单（后台对账用全量） |
| `topup_url()` | 充值页地址，就是接口里的 `donate_url` |
| `source()` | 余额来源的标识和它取自哪个配置项，后台设置页展示用 |

实现由部署方提供（见 README 的「集成点」）。核实不了一律按「现在不知道」处理，绝不当成 0。**没有接入余额来源时**和「没配置官方作品」一样：能逛、能领等级解锁的商品，`donate_url` 为 null，已绑定用户的余额恒为 `stale`，花币购买返回 503 `verification_unavailable`。

## 2. 商品

商品目录在服务端，后台可以上架、下架、改价，不需要发版。

```jsonc
{
  "id": 12,
  "sku": "bubble.aurora",            // 稳定的标识
  "category": "bubble",              // 见 2.1
  "name": "极光",
  "description": "会缓缓流动的渐变气泡",
  "price": 200,                      // 金币；0 表示不卖，只能靠解锁或赠送获得
  "unlock_level": null,              // 账号等级达到多少免费解锁；null 表示不能靠等级解锁
  "style": { /* 见 2.2 */ },
  "preview_text": "今天也要开心呀",    // 预览用的示例文字，可空
  "tag": "new",                      // new / hot / limited / null
  "available_until": null,           // 限时商品的下架时间
  "is_active": true,                 // false 表示已下架（只会出现在我已拥有的商品上）
  "owned": false,
  "equipped": false,
  "obtainable": "buy"                // 我现在能怎么得到它：buy 可以买 / level 等级够了可以免费领 / locked 等级不够且不卖 / owned 已拥有
}
```

### 2.1 类别

`GET /shop` 的 `categories` 给出当前有哪些类别：`[{ key, label, slot }]`。每个类别对应一个装备槽，一个槽同时只能装备一件。一期只有：

| key | 说明 |
| --- | --- |
| `bubble` | 聊天气泡皮肤，只改自己发出的消息的气泡 |

客户端遇到不认识的类别就不显示它的商品。

### 2.2 气泡皮肤的样式

`style` 是一组受限的字段，客户端按它画气泡；不是任意 CSS。服务端保存时会校验。

```jsonc
{
  "version": 1,
  "background": "linear-gradient(135deg, #7F7FD5 0%, #86A8E7 50%, #91EAE4 100%)",   // 纯色或渐变
  "text": "#ffffff",                  // 文字颜色
  "link": "#e8f4ff",                  // 链接颜色，可空（空则沿用 text）
  "border": { "color": "#ffffff33", "width": 1 },        // 可空；width 0–3
  "radius": 14,                       // 4–22，可空（空则用主题默认）
  "shadow": "0 2px 8px #7F7FD566",    // 可空
  "animation": "flow",                // none / flow（渐变缓慢流动）/ shimmer（扫光）/ breathe（光晕呼吸），可空
  // 叠在底色上的纹样，可空。key 是客户端内置的图案
  "pattern": { "key": "stars", "color": "#ffffff", "opacity": 0.18, "scale": 1 },
  // 贴在气泡角上的小装饰，可空，最多 2 个。key 是客户端内置的矢量图形
  "ornaments": [
    { "key": "sparkle", "corner": "tr", "color": "#fff6c4", "size": 18, "animated": true },
    { "key": "moon", "corner": "bl", "color": "#ffe9a8", "size": 14 }
  ],
  "dark": { "background": "…", "text": "…", "border": { … }, "shadow": "…", "pattern": { … } }      // 深色模式下的覆盖，可空
}
```

- `pattern.key` ∈ `dots` 圆点 / `grid` 细格 / `stripes` 斜纹 / `waves` 波浪 / `stars` 星点 / `hearts` 小爱心 / `pixels` 像素块 / `clouds` 云朵 / `petals` 花瓣 / `noise` 细颗粒。`opacity` 0–0.6，`scale` 0.5–2。
- `ornaments[].key` ∈ `sparkle` 闪光 / `star` 星星 / `moon` 月亮 / `sun` 太阳 / `heart` 爱心 / `petal` 花瓣 / `cloud` 云 / `paw` 猫爪 / `leaf` 叶子 / `crown` 皇冠 / `pixel_heart` 像素爱心 / `bolt` 闪电 / `snow` 雪花。`corner` ∈ `tl` / `tr` / `bl` / `br`（装饰骑在气泡的角上，略微探出去一点）；`size` 10–28；`animated` 为 true 时轻微地闪 / 晃（同样受「减少动态效果」约束）。
- `dark` 里只能出现 `background` / `text` / `link` / `border` / `shadow` / `pattern`，没写的沿用浅色的值；圆角、动效、角饰两种配色共用。
- 图案和装饰都是**客户端内置**的，样式里只给名字和颜色，不能带图片地址。客户端遇到不认识的 `key` 就跳过那一项，其余照常画——所以以后加新图案要发版，但用已有图案组合出新皮肤不用。

- 颜色只接受 `#rgb` / `#rrggbb` / `#rrggbbaa`；`background` 只接受一个颜色或一个 `linear-gradient(...)` / `radial-gradient(...)`（里面只能有角度、颜色和百分比）；`shadow` 只接受「偏移 模糊 [扩散] 颜色」的形式，最多两层。
- 客户端必须尊重「减少动态效果」的系统设置（此时 `animation` 当作 `none`）。
- 不认识的 `version` 或字段：忽略这件皮肤，按没装备处理。

### 2.3 别人怎么看到我的皮肤

`UserPublic` 新增 `cosmetics`：`{ "bubble": { "sku": "bubble.aurora", "style": { … } } }`，没装备时是 `{}`。消息的发送者对象里带着它，客户端据此画别人的气泡。旧客户端不认识，不受影响。

装备变化时发实时事件 `user.cosmetics.updated`：`{ "user_id": 42, "cosmetics": { … } }`，发给所有在线用户（同一个人 5 秒内最多一次，取最后的状态）。客户端收到后更新本地缓存里这个用户的 `cosmetics`，已经显示出来的消息气泡跟着变。

## 3. 接口

### 3.1 `GET /shop`

```jsonc
{
  "categories": [{ "key": "bubble", "label": "气泡", "slot": "bubble" }],
  "items": [ /* 上架中的商品，以及我已拥有的（哪怕已下架） */ ],
  "balance": {
    "bound": true,                    // 是否已绑定 CCW 账号
    "donated": 320,                   // 服务端核实到的累计投币
    "spent": 260,
    "balance": 60,
    "verified_at": "…",               // 上次向 CCW 核实的时间；从没核实过为 null
    "stale": false                    // true 表示上次核实失败，余额可能不是最新的
  },
  "equipped": { "bubble": "bubble.aurora" },     // 每个槽装备的 sku
  "donate_url": "https://www.ccw.site/detail/…",
  "level": 23                         // 我的账号等级（用来判断等级解锁）
}
```

### 3.2 `POST /shop/balance/refresh`

向 CCW 重新核实一次累计投币，返回 `balance` 对象。每人 20 秒内最多一次（429 `refresh_too_fast`，`detail.retry_after` 秒）。没绑定 CCW：409 `ccw_not_bound`。CCW 那边查不到或出错：返回上次的数并且 `stale: true`，不报错。

### 3.3 `POST /shop/items/{item_id}/purchase`

没有请求体。服务端会先重新核实一次余额再扣。成功返回：

```jsonc
{ "item": { /* 商品对象，owned: true */ }, "balance": { /* … */ }, "equipped": { /* 没变 */ } }
```

等级够了可以免费领的商品也走这个接口，不扣币。

| HTTP | `detail.code` | 含义 |
| --- | --- | --- |
| 404 | `item_not_found` | 商品不存在 |
| 409 | `item_unavailable` | 已下架或已过期 |
| 409 | `already_owned` | 已经有了 |
| 409 | `ccw_not_bound` | 没绑定 CCW 账号 |
| 409 | `item_locked` | 不卖，且等级不够 |
| 402 | `insufficient_coins` | 余额不够。`detail` 里带 `balance`（数字，当前余额）、`price`、`shortfall`、`donate_url` |
| 503 | `verification_unavailable` | 现在核实不了余额（余额来源不通，或者没有接入），稍后再试。不会扣币 |

### 3.4 `PUT /shop/equipped`

body `{ "slot": "bubble", "sku": "bubble.aurora" }`，`sku` 传 null 表示卸下。→ `{ "equipped": { … } }`。只能装备自己拥有的（403 `not_owned`）。

### 3.5 `GET /shop/purchases?limit&offset`

我的购买记录：`{ items: [{ id, item: { sku, name, category }, price, source, created_at }], total }`。`source` ∈ `purchase` 买的 / `level` 等级解锁 / `grant` 赠送。

### 3.6 细节

- `GET /shop` 不会向 CCW 发请求，只返回上次核实的结果。**绑定后从没核实过的人 `verified_at` 是 null、余额是 0**——客户端打开商店时如果 `bound` 为 true 且 `verified_at` 为 null，应当自动调一次 `POST /shop/balance/refresh`。
- 没配置官方作品（或者没有接入余额来源，见 1.1）时 `donate_url` 为 null，余额恒为 `stale`，购买返回 503。
- 购买时的检查顺序：`item_not_found` → `already_owned` → `item_unavailable` → 等级免费领 / `item_locked` → `ccw_not_bound` → 503 → 402。等级够了的免费领取不要求绑定 CCW。
- 同一个 CCW 账号的币不能换绑到别的 KukeChat 账号再花一遍：`balance.spent` 里包含这个 CCW 账号在别的 KukeChat 账号上已经花掉的部分。
- `GET /shop/purchases` 的每条多 `revoked_at`（被后台收回的时间，否则 null）和 `refunded`。被收回的商品可以再次获得。
- 被后台收回时，本人只会在「正装备着这件」的情况下收到 `user.cosmetics.updated`；商店页的「已拥有」要到下次拉取才变。
- `user.cosmetics.updated` 第一次变化立即发，5 秒内的后续变化合并成最后一次。
- 其它错误码：422 `invalid_slot`（装备时槽不存在）。

## 4. 实时事件

| 事件 | data | 说明 |
| --- | --- | --- |
| `shop.changed` | `{}` | 商品目录有变化（上架、下架、改价），发给所有在线用户，同一进程 30 秒内最多一次。收到后在商店页打开时重拉 |
| `shop.item.granted` | `{ item: { sku, name, category }, source }` | 我得到了一件商品（赠送，或升级后自动解锁） |
| `user.cosmetics.updated` | `{ user_id, cosmetics }` | 有人换了装扮，见 2.3 |

## 5. 后台

| 接口 | 说明 |
| --- | --- |
| `GET /admin/shop/items` | 全部商品（含下架的），每项带已售数量 |
| `POST /admin/shop/items`、`PUT /admin/shop/items/{id}` | 新建 / 修改：`sku`（新建后不可改）、`category`、`name`、`description`、`price`、`unlock_level`、`style`、`preview_text`、`tag`、`is_active`、`sort`、`available_until`。`style` 不合法返回 422 `invalid_style` 并说明哪个字段 |
| `GET /admin/shop/purchases?user_id=&item_id=&limit&offset` | 购买记录 |
| `GET /admin/shop/accounts?q=&mismatch=&limit&offset` | 每个用户的累计投币、已花、余额、上次核实时间；`mismatch=true` 只看「花的比投的多」的 |
| `POST /admin/shop/reconcile` | 向余额来源拉一次完整的投币名单，更新所有已绑定用户的累计投币，返回 `{ donors, matched, updated, mismatched: [...] }`。拉不到完整名单时 503，什么都不改 |
| `POST /admin/shop/users/{user_id}/grant` | `{ item_id, note }` 赠送 |
| `POST /admin/shop/users/{user_id}/revoke` | `{ item_id, note }` 收回（同时卸下） |
| `GET /admin/shop/settings` | 官方作品、各项阈值（只读） |
