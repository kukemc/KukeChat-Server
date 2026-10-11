from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "KukeChat API"
    # Opt in for bounded, anonymized HTTP ASGI byte/latency logs per worker.
    transport_metrics_enabled: bool = False
    database_url: str = "sqlite+aiosqlite:///./kukechat.db"
    # 没有默认值：缺失或仍是占位值时启动即失败，见下方 validator。
    # 一个可预测的签名密钥等于任何人都能伪造任意用户的令牌。
    jwt_secret_key: str = Field(default="")
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7
    cookie_session_days: int = 30
    cookie_session_name: str = "kukechat_session"
    # 默认 None = host-only cookie，只对当前域生效。
    # 只有在需要跨子域共享会话时才设置（例如 ".example.com"）。
    cookie_session_domain: str | None = None
    cookie_secure: bool = True
    redis_url: str | None = None
    realtime_redis_channel: str = "kukechat:realtime"
    upload_dir: str = "uploads"
    storage_backend: Literal["local", "s3"] = "local"
    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_region: str | None = None
    s3_public_url: str | None = None
    image_quality: int = Field(default=82, ge=1, le=95)
    image_max_dimension: int = Field(default=2048, ge=128, le=8192)
    image_thumbnail_max_dimension: int = Field(default=360, ge=64, le=2048)
    # 内容自动审核依赖一个外部审核服务，本项目不提供该服务。
    # 默认关闭；启用时必须同时提供 CONTENT_MODERATION_URL。
    content_moderation_enabled: bool = False
    content_moderation_url: str | None = None
    # 审核服务的共享密钥，随请求放在 X-Moderation-Token 头里。留空则不带。
    content_moderation_token: str | None = None
    content_moderation_timeout_seconds: int = Field(default=30, ge=1, le=120)
    content_moderation_retry_interval_seconds: int = Field(default=300, ge=10, le=86400)
    content_moderation_retry_min_age_seconds: int = Field(default=120, ge=0, le=86400)
    content_moderation_retry_batch_size: int = Field(default=20, ge=1, le=200)
    # 审核日志里 allow / error 记录的保留天数。这两类占全表 98%，是「这条没问题」
    # 和「这次调用失败了」，除了排查当下问题没别的用途，却带着正文和原始响应
    # 永久堆积 —— 一度撑到 50 万行、占掉整个数据库的 64%。设 0 表示不清理。
    content_moderation_log_retention_days: int = Field(default=7, ge=0, le=3650)
    content_moderation_log_cleanup_batch_size: int = Field(default=5000, ge=100, le=100000)
    # 审核服务对文本「拿不准」（review）时要不要先撤回。默认不撤，只留记录等人工；
    # 图片的 review 无论如何都不撤回。两种情况都不会记违规。
    content_moderation_text_review_recall: bool = False
    moderation_alert_email: str | None = None
    moderation_alert_cooldown_minutes: int = Field(default=30, ge=1, le=1440)
    # CCW 账号绑定：用户在指定作品下留言，服务端轮询评论完成校验。
    # 这些值与具体运营方绑定，未配置时 CCW 绑定功能不可用。
    ccw_bind_subject_oid: str | None = None
    ccw_bind_subject_type: str = "CREATION"
    # 访问 CCW 接口时携带的访客标识。请填入你自己的值，不要复用他人的。
    ccw_bind_guest_id: str | None = None
    ccw_bind_poll_interval_seconds: int = Field(default=20, ge=5, le=300)
    ccw_bind_challenge_minutes: int = Field(default=20, ge=3, le=120)
    # 邀请链接的落地页。用户打开它即可加入群聊 / 添加好友，
    # 通常指向你部署了 KukeChat 客户端的页面。
    invite_base_url: str = "http://localhost:5173"
    task_reminder_check_interval_seconds: int = Field(default=60, ge=10, le=3600)
    task_reminder_advance_hours: int = Field(default=24, ge=1, le=168)
    smtp_enabled: bool = False
    smtp_host: str | None = None
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_sender_name: str = "KukeChat Server"
    smtp_use_ssl: bool = True
    # 默认只允许本地开发来源。部署时必须在 .env 里加上你自己的前端域名，
    # 否则浏览器端会被 CORS 拦下。
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ]
    cors_origin_regex: str | None = r"https?://(localhost|127\.0\.0\.1|tauri\.localhost)(:\d+)?"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [origin.strip() for origin in stripped.split(",") if origin.strip()]
        return value

    @field_validator("jwt_secret_key")
    @classmethod
    def validate_jwt_secret(cls, value: str) -> str:
        """拒绝空值和历史占位值。

        令牌签名密钥一旦可预测，任何人都能伪造任意用户（包括管理员）的
        访问令牌，因此这里选择启动即失败，而不是带着弱密钥跑起来。
        """
        secret = (value or "").strip()
        placeholders = {"", "replace-with-a-long-random-secret", "changeme", "secret"}
        if secret in placeholders:
            raise ValueError(
                "JWT_SECRET_KEY 未设置。请在 .env 中填入一个足够长的随机值，例如：\n"
                "    python -c \"import secrets; print(secrets.token_urlsafe(64))\""
            )
        if len(secret) < 32:
            raise ValueError("JWT_SECRET_KEY 太短，至少需要 32 个字符。")
        return secret

    @model_validator(mode="after")
    def validate_moderation(self) -> "Settings":
        if self.content_moderation_enabled and not (self.content_moderation_url or "").strip():
            raise ValueError(
                "CONTENT_MODERATION_ENABLED=true 时必须同时提供 CONTENT_MODERATION_URL。"
                "本项目不自带内容审核服务，需要你自行接入。"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
