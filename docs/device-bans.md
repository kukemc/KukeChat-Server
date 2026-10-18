# 防小号：设备信号、账号关联与设备封禁 接口契约

问题：被处罚的人换个 IP 注册新号，接着违规。站长能接受一个人有一两个小号，要管的是「一堆小号轮流用来躲封禁」的人。

做法分三层：

1. 客户端每个请求带上设备信号（服务端签发的设备令牌、机器码哈希、浏览器指纹），服务端按账号记下来（只存哈希）。
2. 后台看一个账号的设备和关联账号，按信号强弱判断是不是同一个人；可以封信号、一次封掉「这个人」。
3. 注册时数这台设备上已经注册过几个号，超过上限就不给注册；被封的设备不能注册、登录，也不能继续用接口。

代码：`app/core/device.py`（令牌、哈希、中间件）、`app/services/device_signals.py`（记录、关联）、
`app/services/device_bans.py`（封禁、注册限制、设置）、`app/api/v1/admin_devices.py`（后台接口）、`app/models/device.py`。

## 1. 客户端要带的头

每个 `/api/v1` 请求（`/api/v1/admin`、`/api/v1/bot-api` 除外）都可以带下面这些头，哪个缺了都照常处理：

| 头 | 内容 | 谁带 |
| --- | --- | --- |
| `X-Kuke-Device` | 服务端签发的设备令牌 `kd_<22 位>.<22 位>`，客户端当成不透明字符串存 | 所有客户端 |
| `X-Kuke-Machine` | `SHA-256("KukeChat-machine-v1\|" + 原始机器码)` 的 64 位小写十六进制。桌面端：Windows `MachineGuid` / macOS `IOPlatformUUID` / Linux `/etc/machine-id`；安卓：`ANDROID_ID` | 桌面端、安卓 |
| `X-Kuke-Fp` | 浏览器指纹（canvas、WebGL 渲染器、屏幕、时区、语言、CPU 核数等，不含 UA）的 SHA-256 | 网页端、桌面端 |
| `X-Kuke-Platform` | `web` / `desktop` / `android` / `ios` | 所有客户端 |

- **签发**：请求没带有效令牌（缺了、格式不对、签名对不上）时，响应头里带 `X-Kuke-Device-Issue: <新令牌>`（CORS 已暴露这个头）。
  客户端存下来，以后每次都带。令牌是随机 id + HMAC 签名（密钥由 `JWT_SECRET_KEY` 派生），客户端伪造不了别人的令牌，只能不带。
- **`GET /auth/device`** → `{ "device": "<令牌>", "issued": bool }`：带了有效令牌原样返回 `issued=false`；没带就发一个新的（响应头里也有）。
  客户端启动时手里没有令牌就调一次，登录 / 注册前最多等它 3 秒，保证第一次注册就带着令牌。不查库。
- **客户端怎么存**：桌面端在应用数据目录（Rust 写）和 localStorage 各存一份；网页端存 localStorage、IndexedDB、一个 400 天的 cookie 三份；
  安卓存 `filesDir` 文件和 SharedPreferences 两份。启动时从还在的那份恢复，缺的补写。

## 2. 服务端记什么

`device_signals` 表：一行 = (账号, 种类, 值)，记首次 / 最近使用时间、记下的次数、平台、是不是用它注册的。

- 设备令牌、机器码、指纹**只存 HMAC(服务端密钥, 值)**，后台只看得到前 8 位。IP 和 `users.last_login_ip` 一样存原文，另记网段（IPv4 /24、IPv6 /64）。
- **什么时候写**：注册、登录（包括被封的号来登录 —— 这样才知道它换到了哪台设备）当场写；已登录的请求只在进程内存里记一笔，
  同一账号的同一个信号每小时最多记一次，后台循环每 30 秒合并写一次库。请求上不额外查库。
- 上线前的老数据只有 IP（`users.registration_ip / last_login_ip / last_seen_ip`、`auth_sessions.client_ip`），关联时也会看这些。

## 3. 关联强度

| 共用的信号 | 强度 | 说明 |
| --- | --- | --- |
| 设备令牌、机器码 | 强 | 基本就是同一台机器 |
| 机器人和它的创建者 | 强 | 同一个人管着的 |
| 浏览器指纹 | 中 | 同型号手机、同一批机房电脑可能一样 |
| IP | **弱** | 学校、家庭、移动网络的运营商 NAT 都共用 IP。前后 30 分钟内都用过同一个 IP 会在说明里标出来，但仍然是弱 |

- 只靠 IP 连起来的账号永远是「弱」：不进关联圈、不会被「封禁此人」带上、不会触发任何自动处理。IP 只作旁证，以及短期的注册节流。
- **关联圈**：从一个账号出发，只沿强 / 中关联往外走（最多 3 层、50 个账号），强度取路径上最弱的那一段。

## 4. 注册限制

`POST /auth/register` 在原有检查（用户名、同一 IP 只能注册一个号）之后依次检查：

| 情况 | 返回 |
| --- | --- |
| 这次带的任一信号被封（包括 `scope=register` 的 IP / 网段封禁） | 403 `device_banned` |
| 打开了 `require_device_for_register`，却没带有效设备令牌 | 403 `device_required`（「请更新到最新版 KukeChat 后再注册」） |
| 同一设备令牌 / 机器码已经注册过 `max_accounts_per_device` 个号（默认 3） | 403 `too_many_accounts_on_device`，`detail.limit` |
| 同一浏览器指纹已经注册过 `max_accounts_per_fingerprint` 个号（默认 5） | 403 `too_many_accounts_on_device` |
| 同一 IP 网段最近 24 小时注册了 `ip_registrations_per_day` 个号（默认 5） | 429 `registration_ip_throttled` |

只数「用这个信号注册的」账号，在这台设备上登录过别的号不算。上限填 0 = 不限。

## 5. 封禁

`device_bans` 表：封一个信号（`kind` + 值），带理由、期限、操作人。

- **期限**：设备令牌 / 机器码 / 指纹可以永久；**IP 必须有期限**（默认 7 天，最长 30 天），可以封单个 IP 或整个网段（`112.39.99.0/24`）。
- **范围** `scope`：`all` = 登录、注册、已登录的接口都拦；`register` = 只拦注册（IP 默认用这个，免得误伤同一网络下的其他人）。
- **生效**：每个进程把生效中的封禁整张读进内存，30 秒重读一次；本进程改了立刻生效，另一个 worker 最多晚 30 秒。
  新封禁生效时，最近 30 天用过这些信号的在线用户会被断开实时连接（`/ws` 收到 `device.banned`）。
- **管理员不受设备封禁影响**（免得把后台锁在外面），机器人账号也不参与。

命中封禁时（登录、注册、任何需要登录的接口、`GET /auth/session`）：

```jsonc
{ "detail": { "code": "device_banned", "message": "这台设备（或当前网络）已被限制使用 KukeChat",
              "reason": "...", "expires_at": "2026-10-26T08:00:00+00:00", "permanent": false } }
```

登录时先判断账号本身是否被封（`account_suspended`，带申诉令牌），再判断设备；申诉接口（`get_appeal_user`）不看设备封禁。

**随账号封禁自动带上**（设置 `propagate_enabled`，**默认关**）：账号被封（任何来源：管理员、举报、风纪、机器）且封禁不短于
`propagate_min_ban_days` 天（默认 30；永久封禁一定算）时，把它最近 90 天用过的设备令牌和机器码一起封，到期时间和账号一致（`source=propagated`）。
被封的号之后用新设备来登录，新设备也会被带上。账号解封时，这些自动带上的封禁一起解除。指纹和 IP 不会被自动带上。

## 6. 后台接口（`/admin`）

| 接口 | 说明 |
| --- | --- |
| `GET /admin/users/{id}/devices` | `{ user_id, signals: SignalRead[], linked: LinkedAccount[] }`：账号用过的信号（脱敏）和直接关联的账号，强的在前 |
| `GET /admin/users/{id}/cluster` | `{ root_id, accounts: LinkedAccount[], truncated }`：关联圈（只走强 / 中） |
| `GET /admin/device-bans?status=active\|expired\|lifted\|all&kind&q&limit&offset` | `{ items: DeviceBanRead[], total }`；`q` 可以是账号 id（`#123`）、IP / 网段、哈希前缀 |
| `POST /admin/device-bans` | `{ signal_id? \| (kind, value), reason?, duration_hours?, permanent?, scope? }` → `DeviceBanRead`。`value` 是 IP / 网段，或完整的 64 位哈希。不合法 422 `invalid_device_ban` |
| `POST /admin/device-bans/{id}/lift` | `{ note? }` → `DeviceBanRead` |
| `POST /admin/device-bans/person/preview` | `{ user_id, include_linked, include_fingerprint, include_ip }` → 会封哪些账号、哪些信号（不写任何东西） |
| `POST /admin/device-bans/person` | 预览的参数 + `{ reason, duration_hours \| permanent, ban_accounts, ip_duration_hours? }` → `{ bans, banned_user_ids, skipped }` |
| `GET / PUT /admin/device-bans/settings` | 见下；PUT 可以只传要改的，范围不对 422 `invalid_settings` |

```ts
SignalRead = { id, kind: 'device'|'machine'|'fp'|'ip', masked, ip, prefix, location /* 暂为 null */, platform,
               first_seen_at, last_seen_at, seen_count, registered_here, shared_accounts, ban: { id, expires_at, permanent, scope } | null }
LinkedAccount = { user: { id, username, nickname, avatar_url, status, is_bot, created_at, banned_until, ban_reason },
                  strength: 'strong'|'medium'|'weak', score, reasons: { kind: 'device'|'machine'|'fp'|'ip'|'bot_owner', masked, detail }[] }
DeviceBanRead = { id, kind, masked, value /* 只有 ip 给原文 */, scope, reason, status: 'active'|'expired'|'lifted',
                  source: 'admin'|'person'|'propagated', created_at, created_by, expires_at, permanent, lifted_at, lifted_by, source_user, affected_accounts }
```

**封禁此人**：本人（`include_linked` 时连同关联圈里的账号）的设备令牌、机器码，可选指纹（`include_fingerprint`，默认带）和
最近 14 天用过的 IP（`include_ip`，每个账号最多 5 个，期限 `ip_duration_hours`，默认 7 天）一次封掉；`ban_accounts`（默认是）时这些账号也按同一期限封号，
记进违规台账（`admin_action`）。平台管理员、已经被封得更久的账号会跳过（`skip_reason`）。只靠 IP 关联的账号不会被带上。

**设置**（存在 `system_settings`，键前缀 `device_ban.`，改动写审计日志）：

| 键 | 默认 | 范围 |
| --- | --- | --- |
| `max_accounts_per_device` | 3 | 0–100（0 = 不限） |
| `max_accounts_per_fingerprint` | 5 | 0–100 |
| `ip_registrations_per_day` | 5 | 0–1000（按 /24、/64 网段） |
| `require_device_for_register` | false | 新客户端普及之后再打开，否则老版本注册不了 |
| `propagate_enabled` | false | 账号被封时自动带上设备令牌、机器码 |
| `propagate_min_ban_days` | 30 | 0–3650（0 = 任何封禁都带） |

后台通道 `/admin/ws` 新增事件 `admin.device_ban.changed`：`{ ban_id?, user_id?, action: created | lifted | person }`。
审计日志动作：`device_ban.create`、`device_ban.lift`、`device_ban.person`、`device_ban.settings.update`。

## 7. 已知的局限

- 清掉应用数据 / 用无痕窗口 / 换一台设备，就拿不到原来的设备令牌；桌面端和安卓还有机器码兜底，网页端只剩指纹（中）和 IP（弱）。
- 指纹会撞：同型号手机、同一批机房电脑可能一模一样，所以指纹的注册上限更宽，也不会被自动带上。
- 机房电脑如果是同一个系统镜像克隆的，`MachineGuid` 可能一样 —— 这时会被当成同一台机器。遇到误伤可以单独解封。
- 老版本客户端不带任何设备信号，只能靠 IP；`require_device_for_register` 打开之前，老客户端照样能注册。
- WebSocket 握手（浏览器）不带自定义头，实时连接本身不检查设备封禁；接口一律拦，新封禁生效时会主动断开在线连接。
- 没有接入 IP 归属地库，`location` 暂时都是 null。
