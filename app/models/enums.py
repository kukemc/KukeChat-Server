from __future__ import annotations

from enum import Enum


class ConversationType(str, Enum):
    DIRECT = "direct"
    GROUP = "group"


class FriendRequestStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class MemberRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class GroupJoinRequestStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class MessageType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"
    FORWARD_BUNDLE = "forward_bundle"
    SYSTEM = "system"


class PostVisibility(str, Enum):
    PUBLIC = "public"
    FRIENDS = "friends"
    PRIVATE = "private"


class PostNotificationType(str, Enum):
    LIKE = "like"
    COMMENT = "comment"
    REPLY = "reply"
    REPOST = "repost"
    MENTION = "mention"


class ModerationStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PlatformRole(str, Enum):
    USER = "user"
    ADMIN = "admin"


class UserStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class PresenceStatus(str, Enum):
    ONLINE = "online"
    AWAY = "away"
    BUSY = "busy"
    DND = "dnd"
    CREATING = "creating"
    GAMING = "gaming"
    CUSTOM = "custom"


class ReportTargetType(str, Enum):
    MESSAGE = "message"
    USER = "user"
    CONVERSATION = "conversation"
    POST = "post"


class ReportStatus(str, Enum):
    PENDING = "pending"
    REVIEWED = "reviewed"
    REJECTED = "rejected"


class PasswordResetStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ModerationSource(str, Enum):
    MESSAGE_AUTO = "message_auto"
    REPORT_REVIEW = "report_review"


class ModerationAction(str, Enum):
    NONE = "none"
    RECALLED = "recalled"
    USER_BANNED = "user_banned"
    PERMANENT_BAN = "permanent_ban"
    ERROR = "error"


class InviteTargetType(str, Enum):
    GROUP = "group"
    USER = "user"


class TeamupProfileStatus(str, Enum):
    RECRUITING = "recruiting"
    CLOSED = "closed"


class TeamupSkill(str, Enum):
    PROGRAM = "program"
    ART = "art"
    DESIGN = "design"
    MUSIC = "music"
    WRITING = "writing"
    TEST = "test"
    OTHER = "other"


class TeamupSkillLevel(str, Enum):
    BEGINNER = "beginner"
    SKILLED = "skilled"
    EXPERT = "expert"
