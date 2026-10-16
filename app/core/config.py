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
    # 群文件（group-files/ 前缀）单独放的私有桶，和主桶共用 endpoint / 凭据。配了它（且 storage_backend = s3）群文件就走
    # 预签名 URL 直传直下，不经过后端（docs/group-plus.md 第 5 节）；不配就和以前一样存主桶、由后端转发。
    s3_private_bucket: str | None = None
    # 私有桶单独的访问凭据（雨云等服务商的桶级密钥只能访问自己的桶）；不配时私有桶也用上面那对
    s3_private_access_key_id: str | None = None
    s3_private_secret_access_key: str | None = None
    # 发版服务（docs/releases.md）单个安装包的大小上限
    release_max_artifact_bytes: int = Field(default=1024 * 1024 * 1024, ge=1024 * 1024, le=8 * 1024 * 1024 * 1024)
    image_quality: int = Field(default=82, ge=1, le=95)
    image_max_dimension: int = Field(default=2048, ge=128, le=8192)
    image_thumbnail_max_dimension: int = Field(default=360, ge=64, le=2048)
    # 公告长图（信息图、更新日志整图）单独放宽：其它上传路径（头像、聊天图、
    # 作品图、组队图、资料背景、表情包）仍然走上面三个值。放宽的只有
    # announcements/ 一个目录，见 services/storage.py 的 _IMAGE_UPLOAD_FOLDER_LIMITS。
    image_announcement_max_bytes: int = Field(default=20 * 1024 * 1024, ge=3 * 1024 * 1024, le=64 * 1024 * 1024)
    image_announcement_max_dimension: int = Field(default=8192, ge=2048, le=8192)
    image_announcement_max_pixels: int = Field(default=40_000_000, ge=12_000_000, le=100_000_000)
    image_announcement_quality: int = Field(default=90, ge=1, le=95)
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
    # 这些值与具体运营方绑定，未配置时 CCW 绑定功能不可用。真正向 CCW 取数的实现不在本仓库里（见 README「集成点」），
    # 没有接入时这些值填了也不生效。
    ccw_bind_subject_oid: str | None = None
    ccw_bind_subject_type: str = "CREATION"
    # 访问 CCW 时使用的访客标识（由 CCW 网关的实现使用）。请填入你自己的值，不要复用他人的。
    ccw_bind_guest_id: str | None = None
    ccw_bind_poll_interval_seconds: int = Field(default=20, ge=5, le=300)
    ccw_bind_challenge_minutes: int = Field(default=20, ge=3, le=120)
    # —— 商店（docs/shop.md）——
    # 金币余额 = 用户给这个作品累计投的币 − 已花。留空时退回 ccw_bind_subject_oid；两个都没配，
    # 或者没配 ccw_bind_guest_id，或者没有接入余额来源（见 README「集成点」）时，余额功能不可用：能逛、能领等级解锁的商品，不能买。
    ccw_shop_creation_oid: str | None = None
    # 每人多少秒内最多向 CCW 核实一次余额；购买时在这段时间内核实成功过就直接用
    shop_refresh_interval_seconds: int = Field(default=20, ge=1, le=3600)
    # 给余额来源的实现用的两个参数：核实时先看「最近投过币的前 N 人」，不在里面再取全量；全量结果在进程内缓存多少秒
    shop_ranking_recent_limit: int = Field(default=200, ge=1, le=1000)
    shop_ranking_cache_seconds: int = Field(default=60, ge=0, le=3600)
    # shop.changed 同一进程多少秒内最多发一次
    shop_changed_interval_seconds: int = Field(default=30, ge=0, le=3600)
    # user.cosmetics.updated 同一个用户多少秒内最多发一次（间隔内的变化并成一次，发最后的状态）
    shop_cosmetics_event_interval_seconds: int = Field(default=5, ge=0, le=3600)
    # —— 红包（docs/red-packets.md）——
    # 开关和各项限制存在数据库里（后台可改），这里只有清扫的间隔：多久把过期没领完的红包退回一次。
    # 领取时会现场判断过期，所以这个间隔只影响「钱多久退回到账」，不影响对错
    red_packet_sweep_interval_seconds: int = Field(default=60, ge=5, le=3600)
    # —— 限时商品（docs/shop.md 2.5）——
    # 多久清扫一次到期的限时商品（卸下、发 shop.item.expired）和快到期的（发 shop.item.expiring）。
    # 「是否拥有 / 是否装备着」读的时候现场判断到期，这个间隔只影响事件晚多久发，不影响对错
    shop_expiry_sweep_interval_seconds: int = Field(default=60, ge=5, le=3600)
    # —— 礼物（docs/gifts.md）——
    # 开关和限制存在数据库里（后台可改）。心意榜的结果在进程内缓存多少秒
    gift_leaderboard_cache_seconds: int = Field(default=60, ge=0, le=3600)
    # 邀请链接的落地页。用户打开它即可加入群聊 / 添加好友，
    # 通常指向你部署了 KukeChat 客户端的页面。
    invite_base_url: str = "http://localhost:5173"
    # 风纪委员（docs/jury-v2.md）。客户端通过 GET /jury/config 读这些数字，不要在别处写死。
    jury_min_account_age_days: int = Field(default=30, ge=0, le=3650)
    jury_min_recent_messages: int = Field(default=30, ge=0, le=100000)
    jury_recent_activity_days: int = Field(default=30, ge=1, le=365)
    jury_term_days: int = Field(default=30, ge=1, le=3650)
    jury_daily_vote_limit: int = Field(default=30, ge=1, le=1000)
    jury_seats_per_case: int = Field(default=5, ge=3, le=15)
    jury_assignment_ttl_hours: int = Field(default=24, ge=1, le=720)
    # 每 N 个案件里混入一道已有结论的质控题
    jury_golden_every: int = Field(default=5, ge=2, le=1000)
    jury_min_settled_for_review: int = Field(default=20, ge=1, le=10000)
    jury_min_golden_for_review: int = Field(default=5, ge=1, le=10000)
    jury_min_accuracy: float = Field(default=0.6, ge=0, le=1)
    # 进入委员队列后多久没有结论就转给管理员
    jury_timeout_hours: int = Field(default=72, ge=1, le=24 * 60)
    jury_sweep_interval_seconds: int = Field(default=600, ge=30, le=86400)
    # 公开群（join_mode=open）成员数达到这个值后不再按群成员回避，只回避快照里的发言人
    jury_large_group_members: int = Field(default=200, ge=2, le=1000000)
    jury_context_near: int = Field(default=3, ge=0, le=10)
    # 定案后多久内委员还能回看案件内容 / 多久之后进公示栏
    jury_content_retention_days: int = Field(default=30, ge=1, le=3650)
    jury_publish_delay_hours: int = Field(default=72, ge=0, le=24 * 365)
    jury_overview_window_days: int = Field(default=30, ge=1, le=365)
    jury_overview_min_sample: int = Field(default=5, ge=1, le=10000)
    jury_opinion_max_length: int = Field(default=500, ge=1, le=5000)
    jury_opinions_per_case: int = Field(default=20, ge=1, le=1000)
    jury_opinions_per_minute: int = Field(default=5, ge=1, le=1000)
    jury_opinions_per_day: int = Field(default=100, ge=1, le=100000)
    jury_opinion_auto_hide_reports: int = Field(default=2, ge=1, le=100)
    # —— 二期（docs/jury-v2.md 第 2 节）——
    # 定为严重违规（l3）至少要几张有效票；同一案件累计几张「上下文不足」后转出
    jury_severe_min_votes: int = Field(default=5, ge=3, le=15)
    jury_need_context_votes: int = Field(default=2, ge=1, le=15)
    # 质控题的位置：距上一道至少隔几张票才可能再出（之后每次取件按 1 / jury_golden_every 的概率出），隔了多少张之后强制出
    jury_quality_min_gap: int = Field(default=2, ge=0, le=1000)
    jury_quality_max_gap: int = Field(default=9, ge=1, le=10000)
    jury_recuse_daily_limit: int = Field(default=5, ge=0, le=1000)
    # 在质控题上回避过的，过这么多天之后同一道题可以再出给他（回避不算「见过」，否则连着回避就能把计分题耗光）
    jury_quality_recuse_retry_days: int = Field(default=7, ge=1, le=3650)
    # 质控题 / 考试题是早就结案的旧案件：展示时把内容的时间整体平移，让被举报的那条看起来发于「分到手之前的这么多小时」
    #（范围和真实待评审案件的年龄相仿），每条分配记录稳定地取一个值
    jury_quality_shift_min_hours: int = Field(default=2, ge=0, le=24 * 30)
    jury_quality_shift_max_hours: int = Field(default=40, ge=1, le=24 * 30)
    # 弃权过多：近 window 张票里「拿不准 + 上下文不足」的比例，不足 min_votes 张不判断
    jury_abstain_window: int = Field(default=50, ge=1, le=10000)
    jury_abstain_min_votes: int = Field(default=20, ge=1, le=10000)
    jury_abstain_warn_rate: float = Field(default=0.3, ge=0, le=1)
    jury_abstain_cooldown_rate: float = Field(default=0.5, ge=0, le=1)
    jury_abstain_cooldown_hours: int = Field(default=24, ge=1, le=24 * 30)
    # /jury/me.warnings 里「快要被降级 / 暂停」的提醒：比率离那条线不到 margin 时开始提醒；
    # 暂停线的样本数达到要求的 sample_ratio 倍（默认一半）就开始看
    jury_warning_margin: float = Field(default=0.1, ge=0, le=1)
    jury_warning_sample_ratio: float = Field(default=0.5, ge=0, le=1)
    # 风纪值：事件 -> 分值
    jury_points_vote_agreed: int = Field(default=10, ge=-1000, le=1000)
    jury_points_vote_disagreed: int = Field(default=3, ge=-1000, le=1000)
    jury_points_quality_correct: int = Field(default=5, ge=-1000, le=1000)
    jury_points_quality_wrong: int = Field(default=-5, ge=-1000, le=1000)
    jury_points_daily_first: int = Field(default=2, ge=-1000, le=1000)
    jury_points_streak_7: int = Field(default=10, ge=-1000, le=1000)
    jury_points_opinion_liked: int = Field(default=5, ge=-1000, le=1000)
    jury_points_exam_passed: int = Field(default=20, ge=-1000, le=1000)
    jury_points_overturn_adjust: int = Field(default=7, ge=0, le=1000)
    # 一条观点的赞数第一次达到多少时给作者加分
    jury_opinion_like_reward_threshold: int = Field(default=3, ge=1, le=1000)
    # 层级：晋升到正式 / 资深的条件，以及各自的降级线
    jury_regular_votes_settled: int = Field(default=15, ge=0, le=100000)
    jury_regular_agreement_rate: float = Field(default=0.7, ge=0, le=1)
    jury_regular_quality_rate: float = Field(default=0.7, ge=0, le=1)
    jury_regular_quality_min: int = Field(default=3, ge=0, le=100000)
    jury_regular_days_served: int = Field(default=14, ge=0, le=3650)
    jury_regular_demote_rate: float = Field(default=0.6, ge=0, le=1)
    jury_senior_votes_settled: int = Field(default=50, ge=0, le=100000)
    jury_senior_agreement_rate: float = Field(default=0.8, ge=0, le=1)
    jury_senior_quality_rate: float = Field(default=0.8, ge=0, le=1)
    jury_senior_quality_min: int = Field(default=8, ge=0, le=100000)
    jury_senior_days_served: int = Field(default=60, ge=0, le=3650)
    jury_senior_demote_rate: float = Field(default=0.7, ge=0, le=1)
    # 降级看最近多少张已结的票，不足 min_votes 张不判断
    jury_demote_window: int = Field(default=30, ge=1, le=10000)
    jury_demote_min_votes: int = Field(default=15, ge=1, le=10000)
    # 入门考试
    jury_exam_total: int = Field(default=10, ge=1, le=100)
    # 机器人开放接口（/bot-api）每把密钥的频率限制：每个 worker 一个令牌桶，平均每秒 rate 次、最多攒 burst 次。
    # 2026-10-16 有个机器人每秒 20 次轮询 35 个群的成员列表，把数据库连接池占满、整个服务挂了一次
    bot_api_rate_per_second: float = Field(default=5.0, gt=0, le=1000)
    bot_api_burst: int = Field(default=30, ge=1, le=10000)
    jury_exam_pass_score: int = Field(default=8, ge=1, le=100)
    jury_exam_time_limit_minutes: int = Field(default=60, ge=1, le=24 * 60)
    # 浏览器会把后台标签页的定时器降到每分钟一次，宽限太短的话自动交卷会错过
    jury_exam_grace_seconds: int = Field(default=90, ge=0, le=3600)
    jury_exam_attempts_max: int = Field(default=3, ge=1, le=100)
    jury_exam_window_days: int = Field(default=30, ge=1, le=3650)
    jury_exam_retry_hours: int = Field(default=24, ge=0, le=24 * 365)
    jury_exam_valid_days: int = Field(default=90, ge=1, le=3650)
    jury_practice_daily_limit: int = Field(default=20, ge=1, le=10000)
    # 已核对的题里，案件编号能被它整除的那一份只给练习场，其余的给考试和评审质控（练习场会公布答案，两边不能是同一批题）
    jury_practice_every: int = Field(default=3, ge=2, le=1000)
    # —— 三期：资深合议、申诉复审、机器案源、复议（docs/jury-v2.md 3.1–3.3、3.7）——
    # 合议 / 复审都是 3 个席位、两票一致即出结果（写在代码里）；这里只配多久没结果转管理员
    jury_panel_timeout_hours: int = Field(default=48, ge=1, le=24 * 60)
    jury_appeal_timeout_hours: int = Field(default=72, ge=1, le=24 * 60)
    jury_points_panel_agreed: int = Field(default=15, ge=-1000, le=1000)
    jury_points_panel_disagreed: int = Field(default=5, ge=-1000, le=1000)
    jury_points_review_vote: int = Field(default=8, ge=-1000, le=1000)
    # 机器拿不准的内容每天（最近 24 小时）最多进多少件 = max(下限, 在任委员数 × 系数)
    jury_machine_daily_per_juror: float = Field(default=2, ge=0, le=1000)
    jury_machine_daily_min: int = Field(default=5, ge=0, le=100000)
    # 一条消息 / 动态被认定不违规后多少天内不能再举报
    jury_cleared_block_days: int = Field(default=30, ge=0, le=3650)
    # 举报人复议：定案后多少天内可以申请、理由至少多少个字
    jury_reconsider_window_days: int = Field(default=7, ge=1, le=365)
    jury_reconsider_text_min: int = Field(default=10, ge=1, le=2000)
    # 自动入库：委员全票一致且有效票不少于这么多、定案满这么久
    jury_golden_consensus_votes: int = Field(default=3, ge=3, le=15)
    jury_golden_delay_hours: int = Field(default=72, ge=0, le=24 * 365)
    # —— 三期：判例与议事厅（docs/jury-v2.md 3.4、3.5）——
    jury_precedent_title_max: int = Field(default=60, ge=1, le=200)
    jury_precedent_summary_max: int = Field(default=1000, ge=1, le=10000)
    jury_hall_title_max: int = Field(default=60, ge=1, le=200)
    jury_hall_content_max: int = Field(default=5000, ge=1, le=50000)
    jury_hall_comment_max: int = Field(default=1000, ge=1, le=10000)
    # 列表里的正文只给前这么多字
    jury_hall_list_excerpt: int = Field(default=200, ge=1, le=5000)
    # 每人最近 24 小时最多发多少帖 / 多少条评论
    jury_hall_posts_per_day: int = Field(default=3, ge=1, le=1000)
    jury_hall_comments_per_day: int = Field(default=50, ge=1, le=100000)
    # 提案表决几天；通过要求表决人数 >= max(quorum_min, 在任委员数 × quorum_ratio 向上取整)，
    # 且 yes × pass_den >= (yes + no) × pass_num（整数比较，恰好等于也算通过）
    jury_hall_vote_days: int = Field(default=7, ge=1, le=90)
    jury_hall_quorum_ratio: float = Field(default=0.3, ge=0, le=1)
    jury_hall_quorum_min: int = Field(default=3, ge=1, le=10000)
    jury_hall_pass_num: int = Field(default=2, ge=1, le=1000)
    jury_hall_pass_den: int = Field(default=3, ge=1, le=1000)
    # 帖子或评论被这么多名在任委员举报后自动折叠，等管理员处理（其他用户的举报只记录，不计入）
    jury_hall_auto_hide_reports: int = Field(default=3, ge=1, le=1000)
    # 每人最近 24 小时最多举报多少次（帖子和评论合计）
    jury_hall_reports_per_day: int = Field(default=20, ge=1, le=100000)
    # —— 账号等级（docs/account-level.md）。客户端通过 GET /levels/rules 读这些值，不要在别处写死 ——
    # 活跃天的数值都精确到 0.1 天（库里存的是十分之一天的整数）
    account_level_max: int = Field(default=256, ge=1, le=256)
    # 每天（UTC+8 自然日）最多能攒多少活跃天
    account_level_daily_cap: float = Field(default=2.0, ge=0, le=100)
    # 基础：当天在线满多少分钟拿多少活跃天（两档）
    account_level_online_full_minutes: int = Field(default=120, ge=1, le=1440)
    account_level_online_full_days: float = Field(default=1.0, ge=0, le=100)
    account_level_online_half_minutes: int = Field(default=30, ge=1, le=1440)
    account_level_online_half_days: float = Field(default=0.5, ge=0, le=100)
    # 加成，都要先拿到基础才生效
    account_level_bonus_messages_target: int = Field(default=20, ge=1, le=100000)
    account_level_bonus_messages_days: float = Field(default=0.3, ge=0, le=100)
    account_level_bonus_checkin_days: float = Field(default=0.1, ge=0, le=100)
    account_level_bonus_ccw_bound_days: float = Field(default=0.1, ge=0, le=100)
    account_level_bonus_jury_days: float = Field(default=0.2, ge=0, le=100)
    # 连续活跃（连续拿到基础活跃天）满多少天给多少；长的那档替代短的那档。对外的 key 固定是 streak_7 / streak_30
    account_level_streak_short_target: int = Field(default=7, ge=1, le=3650)
    account_level_streak_short_days: float = Field(default=0.1, ge=0, le=100)
    account_level_streak_long_target: int = Field(default=30, ge=1, le=3650)
    account_level_streak_long_days: float = Field(default=0.3, ge=0, le=100)
    # 在线时长多久记一次、每次记多少（时间片长度）。多个 worker 都跑这个循环，靠时间片编号去重
    account_level_tick_seconds: int = Field(default=300, ge=60, le=3600)
    account_level_leaderboard_cache_seconds: int = Field(default=60, ge=0, le=3600)
    # 每升一级都发实时事件；通知中心只在升到这个数的倍数时写一条（4 = 多一个月亮）
    account_level_notify_every: int = Field(default=4, ge=1, le=256)
    # 公告回应（docs/chat-features.md 第 5 节）。客户端通过 GET /announcements/reaction-options 读这些值，不要在别处写死。
    # 快捷回应列表在环境变量里写成 JSON 数组
    announcement_reaction_quick: list[str] = ["👍", "❤️", "😂", "😮", "😢", "🎉", "👀", "🔥"]
    # 每人每条公告最多用几种 emoji / 每条公告最多出现几种 emoji / 每人每分钟最多操作几次
    announcement_reaction_per_user: int = Field(default=5, ge=1, le=100)
    announcement_reaction_per_announcement: int = Field(default=30, ge=1, le=1000)
    announcement_reaction_per_minute: int = Field(default=20, ge=1, le=10000)
    # 资料点赞（docs/chat-features.md 第 6 节）。客户端通过 GET /users/likes/options 读每日上限，不要在别处写死。
    # 每人每天（UTC+8 自然日）最多给多少个不同的人点赞
    profile_like_targets_per_day: int = Field(default=50, ge=1, le=100000)
    # 实时事件 user.profile.liked 的合并窗口：同一个被赞者在这段时间里收到的赞只推一次
    profile_like_event_merge_seconds: float = Field(default=3.0, ge=0, le=60)
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
