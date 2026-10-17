# 发版服务（应用版本、安装包、更新日志、自动更新）

KukeChat 自己的发版服务：安装包放在我们的对象存储里（或者手动填一个外部链接），按「平台 + 渠道 + 版本」发版，带更新日志；
客户端（桌面端、安卓原生、旧的 Capacitor 安卓）检查更新都走这里。

路径前缀 `/api/v1`。和其它 `/api/v1/*` 一样要带客户端证明头（`X-Kuke-Client` / `X-Kuke-Timestamp` / `X-Kuke-Nonce` / `X-Kuke-Proof`），
但**公开接口不需要登录**：没登录也要能检查更新。错误的 `detail` 是 `{ code, message }`，`message` 是可以直接展示的中文。时间一律 ISO（UTC）。

本文件是后台 / 客户端 / 安卓 / 发版脚本共同的依据，改接口要同步改这里。

## 1. 概念

| 字段 | 说明 |
| --- | --- |
| `platform` | `android`、`windows`、`macos`、`linux` |
| `channel` | `stable`（正式版）、`beta`（测试版）。`beta` 用户检查更新时 `stable` 和 `beta` 一起比，谁新给谁 |
| `version` | 语义化版本 `x.y.z`，可带预发布后缀 `x.y.z-beta.1`（预发布排在同号正式版之前）。不带 `v` 前缀 |
| `build` | 整数构建号（安卓的 `versionCode`），版本号相同时用它比大小。必须 ≥ 0 |
| `changelog` | 更新日志（Markdown 纯文本）。惯例是「x.y.z 更新内容包括：」加短横线列表，每条一句话 |
| `min_supported_version` | 强制更新的下限：客户端当前版本 **低于** 它时 `forced = true`，客户端要挡住使用直到更新。可空 |
| `published` | 草稿 `false` / 已发布 `true`。只有已发布的版本对外可见 |
| artifacts | 一个版本可以有多个安装包（比如 Windows x64 / arm64）。每个包要么是**上传到我们存储的**（`source = "upload"`），要么是**手填的外部链接**（`source = "external"`） |

- 同一个 `(platform, channel, version)` 只能有一条。
- 「最新版本」= 已发布的版本里 `(version, build)` 最大的那条，不是最近发布的那条。
- 发布时要求：至少有一个可用（`status = "ready"`）的安装包；版本号必须比同平台同渠道已发布的都新（否则 409 `version_not_newer`，带 `force: true` 可以跳过这条检查）。

### 1.1 安装包存储

- 上传的包放在**主桶**（公开读），key 是 `releases/<platform>/<channel>/<version>/<随机 10 位>/<文件名>`。随机段让草稿期的包没法被猜到地址，也让同名文件重传不会互相覆盖。
- 下载直接走桶 / CDN（`S3_PUBLIC_URL` 拼出来的地址），**不经过应用服务器**。
- 每个包都存 `size` 和 `sha256`：
  - 上传的包：上传前由上传方算好 `sha256` 和 `size` 交给服务器；直传完成后服务器用 `head_object` 核对大小（不一致删掉对象并 422 `size_mismatch`）。走服务器中转上传时服务器自己算 sha256，和填的不一样 422 `sha256_mismatch`。
  - 外部链接：`size`、`sha256` 选填（填了客户端才能校验）。
  - 客户端下载完**必须**校验 `sha256`（有值时），不一致删掉文件、提示重新下载。
- `signature`：给以后接 `tauri-plugin-updater` 预留，存 Tauri 构建生成的 `.sig` 文件内容。现在桌面端用的是自研更新器（`src-tauri/src/updater.rs`），不校验这个字段。服务器**从不生成、也不保存**任何签名私钥。
- 单个包大小上限 `RELEASE_MAX_ARTIFACT_BYTES`（默认 1GB）。

### 1.2 直传和中转

- `STORAGE_BACKEND=s3` 时开直传：后台 / 发版脚本先要一张预签名 PUT，自己把文件 PUT 到桶，再调 `complete`。
- 本地存储（开发环境）没有直传，`uploads` 接口 409 `direct_upload_unavailable`，改走 multipart 中转接口（`POST .../artifacts`）。中转受 nginx `client_max_body_size` 限制，线上大包请用直传。

### 1.3 和旧的「下载配置」的关系

后台原来的「下载配置」页（`/admin/download-settings`，手填 PC / 安卓安装包链接和版本号）继续保留。
`/desktop-update.json`（旧版桌面端、旧版安卓检查更新用的元数据）和 `/download` 下载页的取值规则：

- 某个平台在发版服务里有**已发布的 stable 版本**（且有可用安装包）→ 用发版服务的版本号和安装包地址（`windows` → `pc_version` / `pc_url`，`android` → `mobile_version` / `android_url`）；`macos` 只给下载页用（stable 渠道里文件名是 `.dmg` 的安装包，没有手填值，也不进 `/desktop-update.json`）；
- 没有 → 还用「下载配置」里手填的值。

所以已经装在用户手上的旧客户端不用升级也能从发版服务收到更新；新客户端改调下面的 `/releases/latest`，拿到更新日志、强制更新和 sha256。

## 2. 公开接口（不需要登录）

### 2.1 `GET /releases/latest`

查询参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `platform` | 是 | `android` / `windows` / `macos` / `linux` |
| `channel` | 否 | 默认 `stable` |
| `current` | 否 | 客户端当前版本号。不传时 `update_available` 只表示「有已发布版本」，`forced` 恒为 `false` |
| `arch` | 否 | 例如 `x64`、`arm64`，用来挑 `artifact` |

```json
{
  "platform": "android",
  "channel": "stable",
  "current": "2.0.0",
  "update_available": true,
  "forced": false,
  "latest": {
    "id": 12,
    "platform": "android",
    "channel": "stable",
    "version": "2.0.1",
    "build": 201,
    "changelog": "2.0.1 更新内容包括：\n- 修好了……",
    "min_supported_version": "1.3.0",
    "published_at": "2026-10-16T08:00:00Z",
    "artifacts": [
      { "id": 3, "source": "upload", "file_name": "KukeChat-2.0.1.apk", "url": "https://cdn.example/releases/android/stable/2.0.1/x1y2z3a4b5/KukeChat-2.0.1.apk",
        "size": 31457280, "sha256": "64 位小写十六进制", "arch": null, "signature": null }
    ],
    "artifact": { "...": "按 arch 挑出来的那个包（arch 相同 > arch 为空 > 第一个），没有包时为 null" }
  }
}
```

- 没有任何已发布版本：`latest = null`，`update_available = false`，`forced = false`。
- `update_available = current < latest.version`（`current` 等于或高于最新版本时为 `false`）。
- `forced = update_available && latest.min_supported_version && current < latest.min_supported_version`。
- `current` 不是合法版本号：422 `invalid_version`。
- 响应在服务器内存里缓存最多 30 秒（每个 worker 各自缓存）；发布 / 撤回会清掉本进程的缓存。

### 2.2 `GET /releases`

更新日志页用。参数 `platform`（必填）、`channel`（默认 `stable`，`beta` 同样包含 stable）、`limit`（1–100，默认 20）、`offset`。

```json
{ "items": [ { "...": "同 latest 对象" } ], "total": 7 }
```

按版本号从新到旧排。

## 3. 后台接口（`/admin/releases/*`）

鉴权二选一：

1. 普通管理员登录令牌：`Authorization: Bearer <JWT>`（后台网页用）；
2. **发版令牌**：`X-Kuke-Release-Token: kcr_…`（发版脚本用）。发版令牌只能调 `/admin/releases/*` 里除令牌管理以外的接口，签发它的管理员被撤销管理员身份后令牌自动失效。

所有写操作都写后台审计日志（`admin_audit_logs`，action 以 `release.` 开头，用令牌时 details 里带 `via_token`）。

### 3.1 配置

`GET /admin/releases/config` →

```json
{ "direct_upload": true, "max_artifact_bytes": 1073741824, "platforms": ["android", "windows", "macos", "linux"], "channels": ["stable", "beta"] }
```

### 3.2 版本

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/admin/releases?platform=&channel=&status=draft\|published&limit=&offset=` | 列表，`{ items, total }`，按版本号从新到旧 |
| POST | `/admin/releases` | 建草稿。`{ platform, channel?, version, build?, changelog?, min_supported_version? }` → 201 |
| GET | `/admin/releases/{id}` | 详情 |
| PATCH | `/admin/releases/{id}` | 改字段（同 POST 的字段，都可选）。已发布的版本只能改 `changelog`、`min_supported_version`，改别的 409 `release_published` |
| DELETE | `/admin/releases/{id}` | 只能删草稿（已发布先撤回），连带删掉存储里上传的包。204 |
| POST | `/admin/releases/{id}/publish` | 发布。`{ force?: bool }`。409 `no_artifacts` / `version_not_newer` |
| POST | `/admin/releases/{id}/unpublish` | 撤回成草稿 |

后台版本对象：

```json
{
  "id": 12, "platform": "android", "channel": "stable", "version": "2.0.1", "build": 201,
  "changelog": "…", "min_supported_version": "1.3.0",
  "published": false, "published_at": null,
  "created_at": "…", "updated_at": "…",
  "created_by": { "id": 1, "username": "kukemc", "nickname": "…" },
  "artifacts": [
    { "id": 3, "source": "upload", "status": "ready", "file_name": "KukeChat-2.0.1.apk", "url": "https://…", "storage_key": "releases/…",
      "size": 31457280, "sha256": "…", "arch": null, "signature": null, "created_at": "…" }
  ]
}
```

`status`：`pending`（直传单开了、还没 complete）/ `ready`。只有 `ready` 的包对外可见。超过 24 小时还没 complete 的 `pending` 包在下次操作这个版本时会被清掉。

### 3.3 安装包

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/releases/{id}/artifacts/uploads` | 开直传单。`{ file_name, size, sha256, arch?, signature? }` → 201 `{ artifact, upload: { method: "PUT", url, headers, expires_at } }`。客户端用 `upload.method` + `upload.headers` 把文件原样 PUT 到 `upload.url` |
| POST | `/admin/releases/{id}/artifacts/{artifact_id}/complete` | 直传完成，服务器 head 核对大小。→ 后台版本对象。409 `upload_incomplete`（桶里还没有）/ 422 `size_mismatch` |
| POST | `/admin/releases/{id}/artifacts` | 中转上传（multipart）：`file`，可选 `sha256`、`arch`、`signature` 表单字段 → 201 后台版本对象 |
| POST | `/admin/releases/{id}/artifacts/external` | 外部链接：`{ url, file_name?, size?, sha256?, arch?, signature? }` → 201 后台版本对象。`url` 必须 `http(s)://` |
| PATCH | `/admin/releases/{id}/artifacts/{artifact_id}` | 改 `arch` / `signature` / （外部链接的）`url`、`size`、`sha256` |
| DELETE | `/admin/releases/{id}/artifacts/{artifact_id}` | 删包（上传的连对象一起删）。已发布版本删到一个可用包都不剩时 409 `last_artifact` |

- 同一个版本里 `file_name` 不能重复（`pending` 的会被新的直传单替换），重复 409 `artifact_exists`。
- `sha256` 是 64 位十六进制（大小写都收，存小写），不合法 422 `invalid_sha256`。
- 文件名会被清洗（去掉路径、控制字符，最长 120 字符）。

### 3.4 发版令牌

只能用管理员登录令牌调（发版令牌自己不能签发令牌）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/admin/releases/tokens` | `{ items: [{ id, label, prefix, created_at, last_used_at, expires_at, revoked_at, created_by }] }` |
| POST | `/admin/releases/tokens` | `{ label, expires_in_days? }` → 201 `{ token: "kcr_…", item }`。**明文只在这里返回一次**，服务器只存 sha256 |
| DELETE | `/admin/releases/tokens/{id}` | 撤销。204 |

## 4. 错误码

| HTTP | code | 场景 |
| --- | --- | --- |
| 404 | `release_not_found` / `artifact_not_found` / `token_not_found` | |
| 409 | `release_exists` | 同平台同渠道同版本号已存在 |
| 409 | `release_published` | 改已发布版本的锁定字段、删已发布版本 |
| 409 | `artifact_exists` | 同版本里文件名重复 |
| 409 | `direct_upload_unavailable` | 没开直传 |
| 409 | `upload_incomplete` | complete 时桶里还没有对象 |
| 409 | `no_artifacts` | 发布时没有可用安装包 |
| 409 | `version_not_newer` | 发布时版本不比已发布的新（`force: true` 可跳过） |
| 409 | `last_artifact` | 删掉已发布版本的最后一个可用包 |
| 413 | `file_too_large` | 超过 `RELEASE_MAX_ARTIFACT_BYTES` |
| 422 | `invalid_version` / `invalid_platform` / `invalid_channel` / `invalid_sha256` / `invalid_url` / `size_mismatch` / `sha256_mismatch` / `empty_file` | |
| 401 | `invalid_release_token` | 发版令牌无效、过期、被撤销或签发人已不是管理员 |
| 403 | `token_not_allowed` | 用发版令牌调令牌管理接口 |

## 5. 发版流程（运维）

日常用私有仓库的 `ops/publish-release.ps1`（默认只做预检，加 `-Publish` 才真正上传和发布），或者后台「发版」页手动操作。细节见私有仓库 `docs/publish-release.md`。
