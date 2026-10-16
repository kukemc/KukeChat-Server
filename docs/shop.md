# 商店

用 CCW 金币换 KukeChat 里的装饰（气泡皮肤、头像框、昵称颜色……）。金币是 CCW 社区签到就能拿到的娱乐货币，没有现金价值。

所有路径前缀 `/api/v1`，都要求登录。新错误的 `detail` 是 `{ code, message }`。

## 1. 金币余额

KukeChat 自己不发币、不存币。余额是算出来的：

```
余额 = 这个人给 KukeChat 官方作品累计投出的币 − 在商店里已经花掉的币 − 送礼花掉的币 − 发出的红包总额 + 抢到的红包 + 退回的红包
```

后三项是红包（[`red-packets.md`](red-packets.md)）：发红包时立刻扣整包金额，没被领完的部分在红包结束时退回；抢到的币和投出来的币一样能花。币只在用户之间流动，总量不变。
「送礼花掉的」是礼物（[`gifts.md`](gifts.md)）：送礼物、送商店商品给别人，是真消耗，对方不得币。

- 「累计投出的币」由**服务端**通过可插拔的余额来源核实（按用户绑定的 CCW 账号匹配，见 1.1），不采信客户端报的数。按用户投出的数量算，不扣平台回收的那部分。
- 以前给官方作品投过币的人，绑定 CCW 账号后自带余额。
- 必须先绑定 CCW 账号。没绑定的人能逛商店，不能买。没绑定的人也能抢红包，币记在账上，绑定之后才能花。
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
  "style": { /* 按类别不同：气泡见 2.2，头像框见 2.4 */ },
  "preview_text": "今天也要开心呀",    // 预览用的示例文字，可空
  "tag": "new",                      // new / hot / limited / null
  "available_until": null,           // 限时销售：到这个时间自动停售（和下面的 duration_days 不是一回事）
  "duration_days": null,             // 买一次拥有多少天；null 是永久。见 2.5
  "is_active": true,                 // false 表示已下架（只会出现在我已拥有的商品上）
  "owned": false,
  "equipped": false,
  "obtainable": "buy",               // 我现在能怎么得到它：buy 可以买 / level 等级够了可以免费领 / locked 等级不够且不卖 / owned 已拥有 / renew 我拥有的是限时的，可以再买来续期
  "expires_at": null                 // 我拥有的这一件什么时候到期；永久或者没拥有为 null
}
```

### 2.1 类别

`GET /shop` 的 `categories` 给出当前有哪些类别：`[{ key, label, slot }]`。每个类别对应一个装备槽，一个槽同时只能装备一件；不同的槽互不影响，可以同时装备一件气泡和一件头像框。目前有：

| key | slot | 说明 | `style` |
| --- | --- | --- | --- |
| `bubble` | `bubble` | 聊天气泡皮肤，只改自己发出的消息的气泡 | 2.2 |
| `frame` | `frame` | 头像框：套在自己头像外面的一圈环，别人在消息、成员列表、资料卡上看到的我的头像都带着它 | 2.4 |

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

`UserPublic` 新增 `cosmetics`，按槽给出装备着的商品，没装备的槽不出现，一件都没装备时是 `{}`：

```jsonc
{
  "bubble": { "sku": "bubble.aurora", "style": { /* 2.2 */ } },
  "frame": { "sku": "frame.galaxy", "style": { /* 2.4 */ } }
}
```

消息的发送者对象里带着它，客户端据此画别人的气泡和头像框。旧客户端不认识这个字段、或者不认识其中某个槽，不受影响（不认识的槽直接忽略）。

装备变化时发实时事件 `user.cosmetics.updated`：`{ "user_id": 42, "cosmetics": { … } }`，发给所有在线用户（同一个人 5 秒内最多一次，取最后的状态）。`cosmetics` 是这个人**全部槽**的最新状态，不是只有变了的那个槽——客户端收到后整个替换本地缓存里这个用户的 `cosmetics`，已经显示出来的消息气泡和头像框跟着变。

### 2.4 头像框的样式

和气泡一样，`style` 是一组受限的字段，客户端按它在头像外面画一圈环；不是任意 CSS，服务端保存时会校验。所有尺寸都**按 40px 的头像计**，客户端按头像实际大小等比缩放。

```jsonc
{
  "version": 1,
  "ring": {
    "background": "conic-gradient(from 0deg, #7F7FD5, #91EAE4, #7F7FD5)",  // 纯色，或 linear / radial / conic 渐变
    "width": 3,        // 2–6
    "gap": 1           // 环和头像之间的留白 0–3，可空（默认 0）
  },
  "glow": { "color": "#7F7FD588", "blur": 8 },   // 外发光，可空；blur 0–16
  "animation": "spin",   // none / spin（环的渐变缓慢旋转）/ pulse（发光呼吸）/ shimmer（环上扫光），可空
  // 贴在环上的装饰，可空，最多 3 个。key 是客户端内置的矢量图形
  "ornaments": [
    { "key": "crown", "position": "top", "color": "#ffd76a", "size": 18, "animated": false }
  ],
  "dark": { "ring": { "background": "…" }, "glow": { … } }   // 深色模式下的覆盖，可空
}
```

- `ring` 必填，其中 `background` 和 `width` 必填。`glow` 写了的话 `color` 和 `blur` 都要有。
- `ring.background` 接受一个颜色，或者一个 `linear-gradient(...)` / `radial-gradient(...)` / `conic-gradient(...)`。前两种的规则和气泡的 `background` 完全一样；`conic-gradient(...)` 里只能有开头可选的 `from <角度>deg`（-360 到 360）、颜色和百分比，2–8 个颜色——不能写 `at <位置>`、不能用角度当色标、不能用 `turn` / `rad`。**只有这里放开了 conic**，气泡的 `background` 仍然不接受。要让 `spin` 转起来没有接缝，首尾写同一个颜色。
- `ornaments[].key` ∈ 气泡的全部角饰（见 2.2），再加头像框专用的：`cat_ears` 猫耳 / `halo` 光环 / `horns` 小恶魔角 / `laurel` 月桂 / `wings` 翅膀 / `bow` 蝴蝶结。这几个专用的不能用在气泡上。
- `ornaments[].position` ∈ `top` / `tr` / `right` / `br` / `bottom` / `bl` / `left` / `tl`（环上的八个方位），同一个位置只能放一个；`size` 10–28；`animated` 同气泡。注意位置字段叫 `position`，不是气泡的 `corner`。
- `dark` 里只能出现 `ring.background` 和 `glow`（`glow` 要写就写完整的 `color` + `blur`），没写的沿用浅色的值；环的粗细、留白、动效、装饰两种配色共用。装饰没有深色覆盖，颜色要选在浅色和深色背景上都看得见的。
- 颜色格式、「减少动态效果」（此时 `animation` 当作 `none`、`animated` 当作 false）、不认识的 `version` / 字段 / `key` 的处理，都和气泡一致（见 2.2）。
- 服务端首次启动时播种 11 件默认头像框（只在 sku 不存在时插入，后台改过的不动）：`frame.mint` 薄荷 10 / `frame.sakura` 樱花 30 / `frame.ocean` 海盐 60 / `frame.neko` 猫耳 60 / `frame.galaxy` 星环 100 / `frame.aurora` 极光 200 / `frame.halo` 光环（4 级解锁）/ `frame.crown` 加冕（16 级解锁）/ `frame.bow` 蝴蝶结 30 / `frame.devil` 小恶魔 **50 币 / 30 天** / `frame.angel` 天使 **120 币 / 30 天**（这两件是限时的，见 2.5；其余永久）。它们的样式和客户端 `dev/mocks/shop.ts` 的 `FRAMES` 是同一套，要调先在客户端看效果，再两边一起改。
- 后台编辑器需要的几张表在 `GET /admin/shop/settings` 里：`frame_animations`、`frame_ornaments`、`frame_positions`。

### 2.5 限时商品

商品的 `duration_days` 为 null 是永久（一直以来的行为）；为 N 是「买一次拥有 N 天」。

- **购买**（`POST /shop/items/{id}/purchase`，等级够了免费领也一样）：没拥有（或者已经到期）→ 到期时间 = 现在 + N 天；已拥有、还没到期 → **在原到期时间上再加 N 天**（续期），每续一次扣一次钱。永久拥有着的商品重复购买仍是 409 `already_owned`。
- 我拥有一件限时商品、还没到期、而且它现在还能买（上架中，卖或者等级够了能领）时，`obtainable` 是 `renew`；不能买了就是 `owned`。商品对象的 `expires_at` 是我这一件的到期时间。
- **到期**：视同不再拥有——`owned` 为 false、`cosmetics` 里消失、装备接口返回 403 `not_owned`。这些都是**读的时候现场判断**的，不依赖定时清扫：到期的那一刻起，任何接口、任何消息里的用户对象上都不再有它。装备着的限时商品的到期时间和样式一起记在用户行上，所以序列化用户仍然不查别的表。
- **定时清扫**（每个进程每 `SHOP_EXPIRY_SWEEP_INTERVAL_SECONDS` 秒，默认 60）只做读路径做不了的两件事：把到期的那一件从装备里真的卸下并照常发 `user.cosmetics.updated`，以及通知本人（见第 4 节的 `shop.item.expired` / `shop.item.expiring`）。多个进程同时清扫是幂等的，每件事每次到期只发生一次。所以事件最多比真正的到期时间晚一分钟——客户端收到用户对象时 `cosmetics` 已经是对的，自己的商店页按 `expires_at` 判断即可。
- 到期提醒在**到期前 3 天**发一次。续期之后到期时间往后推了，快到新的到期时间时会再提醒一次。一得到就不足 3 天的（比如后台送了 1 天的体验）不提醒。
- 到期之后可以再买，从那时起重新算 N 天。
- 等级解锁、后台赠送也按商品的 `duration_days` 计期；后台赠送可以另传 `duration_days` 覆盖（见第 5 节）。等级解锁的限时商品到期之后不会自动再发，等级够的人自己去商店再领一次（`obtainable = level`，免费）。
- 别人送的商品（[`gifts.md`](gifts.md)）走同一套规则：对方没有就给 N 天，已有限时的就续期，已经永久拥有则送不出去。
- **修改商品的 `duration_days` 只影响之后的购买 / 赠送**，不动已有的拥有记录。把限时商品改成永久之后，已经拥有限时版的人再买一次会换成永久的。
- 购买记录（3.5）每续一次各有一条；被后台收回时收回的是当前的那一条——选了退币也只退最后一次的钱。

## 3. 接口

### 3.1 `GET /shop`

```jsonc
{
  "categories": [{ "key": "bubble", "label": "气泡", "slot": "bubble" }, { "key": "frame", "label": "头像框", "slot": "frame" }],
  "items": [ /* 上架中的商品，以及我已拥有的（哪怕已下架） */ ],
  "balance": {
    "bound": true,                    // 是否已绑定 CCW 账号
    "donated": 320,                   // 服务端核实到的累计投币
    "spent": 260,
    "balance": 60,
    "verified_at": "…",               // 上次向 CCW 核实的时间；从没核实过为 null
    "stale": false,                   // true 表示上次核实失败，余额可能不是最新的
    "red_packet_sent": 0,             // 红包：发出的整包金额之和 / 抢到的 / 没被领完退回来的
    "red_packet_received": 0,
    "red_packet_refunded": 0,
    "gift_spent": 0                   // 送礼物 / 送商品花掉的币。balance = donated − spent − gift_spent − red_packet_sent + red_packet_received + red_packet_refunded
  },
  "equipped": { "bubble": "bubble.aurora", "frame": "frame.galaxy" },     // 每个槽装备的 sku，没装备的槽不出现
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

等级够了可以免费领的商品也走这个接口，不扣币。限时商品的购买和续期也是这个接口（2.5）：返回的商品对象里 `expires_at` 是新的到期时间。

| HTTP | `detail.code` | 含义 |
| --- | --- | --- |
| 404 | `item_not_found` | 商品不存在 |
| 409 | `item_unavailable` | 已下架或已过期 |
| 409 | `already_owned` | 已经永久拥有了（拥有的是限时商品时不是错误，是续期） |
| 409 | `ccw_not_bound` | 没绑定 CCW 账号 |
| 409 | `item_locked` | 不卖，且等级不够 |
| 402 | `insufficient_coins` | 余额不够（余额按第 1 节的完整公式算：送礼花掉的、发出去的红包已经扣掉，抢到的算在里面）。`detail` 里带 `balance`（数字，当前余额）、`price`、`shortfall`、`donate_url` |
| 503 | `verification_unavailable` | 现在核实不了余额（余额来源不通，或者没有接入），稍后再试。不会扣币 |

### 3.4 `PUT /shop/equipped`

body `{ "slot": "bubble", "sku": "bubble.aurora" }`，`sku` 传 null 表示卸下。→ `{ "equipped": { … } }`（所有槽的状态）。只能装备自己拥有的（403 `not_owned`）。头像框是 `{ "slot": "frame", "sku": "frame.galaxy" }`；一次只动一个槽，别的槽不变。

### 3.5 `GET /shop/purchases?limit&offset`

我的购买记录：`{ items: [{ id, item: { sku, name, category }, price, source, created_at, expires_at, duration_days, from_user }], total }`。

- `source` ∈ `purchase` 买的 / `level` 等级解锁 / `grant` 后台赠送 / `gift` 别的用户送的（[`gifts.md`](gifts.md)；`price` 为 0，`from_user` 是送礼人的 `UserPublic`，其余来源 `from_user` 为 null）。
- `duration_days` 是这一次得到了多少天，永久的为 null。`expires_at` 是这一条记录到什么时候为止：当前的那一条就是这件商品的到期时间；续期之前的那几条到续期的那一刻为止（它剩下的时间已经并进了接替它的那一条）；永久的为 null。

### 3.6 细节

- `GET /shop` 不会向 CCW 发请求，只返回上次核实的结果。**绑定后从没核实过的人 `verified_at` 是 null、余额是 0**——客户端打开商店时如果 `bound` 为 true 且 `verified_at` 为 null，应当自动调一次 `POST /shop/balance/refresh`。
- 没配置官方作品（或者没有接入余额来源，见 1.1）时 `donate_url` 为 null，余额恒为 `stale`，购买返回 503。
- 购买时的检查顺序：`item_not_found` → `already_owned` → `item_unavailable` → 等级免费领 / `item_locked` → `ccw_not_bound` → 503 → 402。等级够了的免费领取不要求绑定 CCW。
- 同一个 CCW 账号的币不能换绑到别的 KukeChat 账号再花一遍：`balance.spent` 里包含这个 CCW 账号在别的 KukeChat 账号上已经花掉的部分，以及在别的账号上用红包发出去（没退回）的部分、送礼花掉的部分。
- `GET /shop/purchases` 的每条多 `revoked_at`（被后台收回的时间，否则 null）和 `refunded`。被收回的商品可以再次获得。
- 被后台收回时，本人只会在「正装备着这件」的情况下收到 `user.cosmetics.updated`；商店页的「已拥有」要到下次拉取才变。
- `user.cosmetics.updated` 第一次变化立即发，5 秒内的后续变化合并成最后一次。
- 其它错误码：422 `invalid_slot`（装备时槽不存在，或者这件商品不是这个槽的——比如把头像框往 `bubble` 槽里装）。

## 4. 实时事件

| 事件 | data | 说明 |
| --- | --- | --- |
| `shop.changed` | `{}` | 商品目录有变化（上架、下架、改价），发给所有在线用户，同一进程 30 秒内最多一次。收到后在商店页打开时重拉 |
| `shop.item.granted` | `{ item: { sku, name, category }, source }` | 我得到了一件商品（后台赠送，或升级后自动解锁）。别的用户送我商品时不发这一条，只发 `gift.received`（[`gifts.md`](gifts.md)） |
| `shop.item.expiring` | `{ item: { sku, name, category }, expires_at }` | 我的一件限时商品快到期了（到期前 3 天，每次到期只发一次），**只发给本人**。见 2.5 |
| `shop.item.expired` | `{ item: { sku, name, category } }` | 我的一件限时商品到期了，装备着的话已经被卸下，**只发给本人**（每次到期只发一次）。最多比到期时间晚一分钟 |
| `user.cosmetics.updated` | `{ user_id, cosmetics }` | 有人换了装扮，见 2.3 |
| `shop.balance.changed` | `{ reason, change, balance, red_packet_id }` | 我的余额变了，**只发给本人**（别的设备跟着变）。`balance` 是完整的 balance 对象（3.1），可以直接整个替换本地的，也可以收到就重拉。`reason` 是来源：`"red_packet"` 或 `"gift"`；`change` 是具体哪一种：`red_packet.sent` 发了红包 / `red_packet.claimed` 领到了 / `red_packet.refunded` 红包结束、没领完的退回来了（带 `red_packet_id`）/ `gift.sent` 送了礼（带 `gift_id`，送礼记录的 id）。商店购买、后台退款暂时不发（购买接口的返回里已经带了最新的 balance）。客户端遇到不认识的 `reason` / `change` 也照样更新余额，以后别的来源加进来不用改 |

## 5. 后台

| 接口 | 说明 |
| --- | --- |
| `GET /admin/shop/items` | 全部商品（含下架的），每项带已售数量 |
| `POST /admin/shop/items`、`PUT /admin/shop/items/{id}` | 新建 / 修改：`sku`（新建后不可改）、`category`、`name`、`description`、`price`、`unlock_level`、`style`、`preview_text`、`tag`、`is_active`、`sort`、`available_until`、`duration_days`（有效期，1–3650 天；null 是永久。不合法是 422 `invalid_item`，`detail.field = "duration_days"`。改它只影响之后的购买 / 赠送）。`style` 按 `category` 校验（气泡 2.2，头像框 2.4），不合法返回 422 `invalid_style` 并说明哪个字段；只改 `category` 不改 `style` 时，旧样式也得符合新类别的规则 |
| `GET /admin/shop/purchases?user_id=&item_id=&limit&offset` | 购买记录。每项带 `expires_at`、`duration_days`（含义同 3.5）、`superseded_at`（被续期接替的时间，当前的那一条为 null）、`from_user`（`source = gift` 时是谁送的：`{ id, username, nickname, avatar_url }`）。按 `user_id` 筛出来的就是这个人的拥有列表：没被收回、`expires_at` 为 null 或者还没到的那几条是现在拥有的 |
| `GET /admin/shop/accounts?q=&mismatch=&limit&offset` | 每个用户的累计投币、已花、送礼花掉的（`gift_spent`）、红包收支（`red_packet_sent` / `red_packet_received` / `red_packet_refunded`）、余额、上次核实时间；`mismatch=true` 只看**余额为负**的（每项的 `mismatch` 也是这个口径）。抢到过红包的人也在这个列表里 |
| `POST /admin/shop/reconcile` | 向余额来源拉一次完整的投币名单，更新所有已绑定用户的累计投币，返回 `{ donors, matched, updated, mismatched: [...], red_packets: {...}, gifts: {...} }`（`gifts` 是送礼的账的守恒检查，见 [`gifts.md`](gifts.md) 5.1）。拉不到完整名单时 503，什么都不改。有了红包之后「花的比投的多」不再必然是异常（抢到的币也能花），所以查的是：`mismatched` 余额为负的账户，和 `red_packets` 全站红包账的守恒（`{ total_sent, total_claimed, total_refunded, outstanding, balanced, problems }`，见 [`red-packets.md`](red-packets.md) 5.1） |
| `POST /admin/shop/users/{user_id}/grant` | `{ item_id, note, duration_days }` 赠送。`duration_days` 不传 = 按商品的设置；正整数（≤ 3650）= 这一份只给这么多天；`0` = 送成永久。给已经拥有这件**限时**商品的人再送 = 续期（在原到期时间上加）；已经永久拥有是 409 `already_owned`。范围不对是 422 `invalid_duration` |
| `POST /admin/shop/users/{user_id}/revoke` | `{ item_id, note, refund }` 收回（同时卸下）。收回的是当前的那一条记录；`refund` 为 true 时只退那一条的钱 |
| `GET /admin/shop/settings` | 官方作品、各项阈值，以及样式里能用的名字：`categories`、`tags`、气泡的 `animations` / `patterns` / `ornaments`、头像框的 `frame_animations` / `frame_ornaments` / `frame_positions`，还有 `price_max`、`unlock_level_max`、`duration_max`（只读） |

审计日志：赠送仍是 `shop.grant`（`details` 多了 `duration_days`：这一份给了多少天，永久为 null），改商品仍是 `shop.item.update`。限时商品到期是系统行为，不写审计日志。

### 5.1 限时商品的实现细节

- `shop_purchases` 每一行多了 `expires_at`、`duration_days`、`superseded_at`、`from_user_id`、`gift_record_id` 和两个「已通知」时间。「当前的那一行」= 没被收回、没被接替；唯一索引（`uq_shop_purchases_live`）只管它，所以一个人一件商品同时只有一行当前记录。「现在拥有」= 当前的那一行没到期。
- **续期**是把当前的那一行标成被接替、再写一行新的（带累加之后的到期时间），所以每次花的钱、得到的天数各有一行，换绑防刷的口径（按记录上的 CCW 账号求和）不用变。
- **并发续期不丢天数**：接替是一条带条件的 UPDATE（那一行必须还是当前记录），抢不到的事务整个回滚、按对方提交之后的到期时间重来；两个事务同时写第一行，后提交的撞唯一索引，同样重来。花币的路径上扣款那条 UPDATE 在前，事务从那里起就拿着数据库的写锁，后一个事务读到的一定是前一个提交之后的到期时间。
