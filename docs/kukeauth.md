# KukeAuth：统一账号

KukeChat 的账号升级成的统一账号系统。其他服务通过标准 OAuth 2 / OIDC（授权码 + PKCE）登录，用户在账号站注册、登录、改资料。

代码：`app/api/kukeauth.py`（接口）、`app/services/kukeauth.py`（业务）、`app/models/kukeauth.py`（表）、`app/api/v1/admin_kukeauth.py`（后台）、`account-web/`（页面）。

## 1. 配置

| 环境变量 | 说明 |
| --- | --- |
| `KUKEAUTH_ISSUER` | 账号站地址，比如 `https://auth.example.com`，不带结尾斜杠 |
| `KUKEAUTH_SIGNING_KEY` | ES256（P-256）私钥 PEM，换行写成 `\n`。和 `JWT_SECRET_KEY` 无关 |
| `KUKEAUTH_RETIRED_PUBLIC_KEYS` | 换密钥时留着的旧公钥（JSON 数组），已发出的令牌过期前还能验证 |
| `KUKEAUTH_SESSION_DAYS` | 账号站会话天数，默认 30 |

生成密钥：`openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt`

前两项没配时，`/oauth/*` 和 `/account/api/*` 一律 503。接口只在 issuer 的域名上响应，其他 Host 返回 404。

### 部署账号站

`account-web/` 是纯静态页面：`npm run build` 后放到服务器。nginx 的要求：

- 未知路径回退到 `index.html`（页面路径有 `/login`、`/register`、`/forgot`、`/verify`、`/continue`、`/account`）；
- `/oauth/`、`/account/api/`、`/.well-known/` 反代到后端，保留 `Host`，用 `proxy_set_header X-Real-IP $remote_addr` 覆盖客户端自带的值。限速和「一个 IP 一个号」靠它；
- 页面响应加 `Content-Security-Policy: default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'sha256-<index.html 内联脚本的哈希>'; frame-ancestors 'none'; base-uri 'none'`。浏览器把 CRLF 当 LF 算哈希，算之前先把换行统一成 LF。

## 2. 接入一个服务

1. 在后台创建 client（`POST /api/v1/admin/kukeauth/clients`），`client_secret` 只显示这一次，库里只有哈希；
2. 读 `{issuer}/.well-known/openid-configuration` 拿到各接口地址；
3. 走授权码流程：

```text
GET {issuer}/oauth/authorize
    ?response_type=code&client_id=...&redirect_uri=...
    &scope=openid profile&state=...&nonce=...
    &code_challenge=...&code_challenge_method=S256

用户确认后跳回 redirect_uri?code=...&state=...&iss=...

POST {issuer}/oauth/token   grant_type=authorization_code, code, redirect_uri, code_verifier, client_id, client_secret
GET  {issuer}/oauth/userinfo   Authorization: Bearer <access_token>
```

接入方必须校验 `state`，用 `sub` 作为用户的唯一标识。用户名可能变更，不要拿它当主键。令牌留在接入方后端，不要交给浏览器。

### 规则

| 项目 | 规则 |
| --- | --- |
| 流程 | 只支持授权码 + PKCE（S256），`state` 必填 |
| 回调地址 | 完全相等匹配。公开 client 的 `http://127.0.0.1` 和 `http://[::1]` 可以换端口，`localhost` 不行 |
| `prompt` | `none`（不能静默时返回 `login_required` / `consent_required` / `interaction_required`）、`login`（要求重新登录）、`consent`、`select_account` |
| 授权码 | 60 秒、一次性。重放时吊销它换出来的 refresh token |
| access token | 1 小时，ES256，`aud` 是 client_id。吊销不了，用户被封后 userinfo 直接 401 |
| refresh token | 30 天，要 `offline_access`，每次刷新都轮换。旧令牌重用时吊销整组 |
| 登出 | `GET /oauth/logout?id_token_hint=...&post_logout_redirect_uri=...`，回跳地址要登记过 |

### scope 和 userinfo

| scope | 字段 |
| --- | --- |
| `openid` | `sub`（`acc_` 开头，不暴露内部 ID） |
| `profile` | `preferred_username`、`name`、`picture`、`bio` |
| `email` | `email`、`email_verified`，只在邮箱验证过时给 |
| `ccw` | `ccw_verified`，绑定时还有 `ccw_student_oid`、`ccw_name` |
| `offline_access` | 发 refresh token |

## 3. 和聊天账号的关系

- 同一张 `users` 表，老用户用原密码直接登录。
- 令牌不互通：OAuth 令牌用 ES256，聊天接口只认 HS256；userinfo 只认 ES256。
- 账号站有自己的会话（`account_sessions`），cookie 是 `__Host-kuke_auth`，只属于账号站的域名，`SameSite=Lax`。不复用 `kukechat_session`。
- 账号站接口不走 `/api/v1`，所以不需要客户端证明头。写接口校验 `Origin` 必须等于 issuer。

### 封禁

| 类型 | 字段 | 影响 |
| --- | --- | --- |
| 聊天处罚 | `status`、`banned_until` | 只影响聊天，照样能登录账号站和其他服务 |
| 账号级封禁 | `account_banned_at`、`account_banned_until`、`account_ban_reason` | 不能登录账号站和其他服务，会话和 refresh token 全部作废，userinfo 返回 401。不影响聊天 |
| 接入方自己的封禁 | 接入方的库 | 比如反馈站封瞎反馈的账号，只影响那个服务 |

账号级封禁走 `PUT /api/v1/admin/kukeauth/users/{id}/ban`，记在 `admin_audit_logs`，不进处罚台账 `user_violations`。

### 注册

- 和客户端注册共用 `create_user_account`：同一 IP 一个号、设备上限、网段上限都生效。
- 账号站注册至少 8 位密码，注册时不收邮箱，邮箱靠验证码绑定。
- 账号站注册的号第一次登录 KukeChat 时才进官方群（`official_group_pending`）。
- 邮箱和 CCW 至少验证一个才能授权给其他服务。老账号同样如此，没验证的第一次授权时会被带去验证。

### 邮箱

`users.email_verified_at` 为空的邮箱不算数，不会给接入方，不能用来找回密码。有人验证了别的账号未经验证占用的邮箱时，已验证方优先，原账号的邮箱被清空并记审计日志，它还能用用户名登录。

## 4. 账号站接口

都在 `/account/api` 下。错误统一是 `{"detail": {"code": "...", "message": "给人看的话"}}`，沿用 KukeChat 校验的地方 `detail` 可能是字符串。

| 接口 | 说明 |
| --- | --- |
| `GET /device` | 领设备令牌（`docs/device-bans.md`） |
| `POST /register`、`POST /login`、`POST /logout` | 注册、登录、退出 |
| `GET /me`、`PATCH /me` | 读资料；改 `nickname`、`bio`、`avatar_url`，走 `services/users.py::update_profile` |
| `POST /me/avatar` | 上传头像，返回 `url`，再 `PATCH /me` 保存 |
| `POST /me/password` | 改密码。其他会话、refresh token、聊天 cookie 会话全部作废 |
| `GET /me/grants`、`DELETE /me/grants/{client_id}` | 已授权应用、取消授权 |
| `POST /email/send`、`POST /email/verify` | 验证邮箱 |
| `POST /ccw/challenge`、`POST /ccw/check` | 绑定 CCW，复用 `services/ccw.py` |
| `GET /connections`、`POST /connections/{provider}/unbind` | 账号绑定页。解绑要当前密码；没有验证过的邮箱时不能解绑（409 `last_method`） |
| `POST /password-reset/email/send`、`.../confirm` | 邮箱找回密码。不管邮箱是否存在都返回一样的结果 |
| `POST /password-reset/ccw/challenge`、`.../confirm` | CCW 找回密码 |
| `GET /requests/{id}`、`POST .../approve`、`POST .../deny` | 授权确认页用 |

### 账号绑定

页面按 `services/kukeauth.py::connections` 返回的列表画，目前只有 CCW。接入新的外部账号（比如 GitHub）要做四件事：在 `connections` 加一项，在 `is_verified` 和解绑判断里算上它，后端加绑定接口并在 `unbind_connection` 分发解绑，页面 `BINDERS` 加绑定组件。

KukeChat 客户端原有的 CCW 解绑接口（`app/api/v1/ccw.py`）不要求密码，也不检查是否还有别的验证方式，所以账号可能回到未验证状态，那时再授权其他服务会被带去验证。金币防重复花的规则见 `docs/gifts.md`。

### 限速

`app/core/rate_limit.py`，配了 Redis 就跨 worker 共享。

| 操作 | 上限 |
| --- | --- |
| 登录 | 同一账号 + IP 10 次 / 10 分钟；同一 IP 60 次 / 10 分钟 |
| 注册 | 同一 IP 20 次 / 小时 |
| 发邮件 | 同一邮箱 1 封 / 分钟；同一 IP 10 封 / 小时 |
| 邮箱验证码 | 每个码错 5 次作废 |
| 换令牌 | 同一 IP 120 次 / 分钟 |

## 5. 后台接口

都要管理员权限，在 `/api/v1/admin/kukeauth` 下：`GET/POST /clients`、`PATCH /clients/{client_id}`、`POST /clients/{client_id}/rotate-secret`、`PUT/DELETE /users/{id}/ban`。
