from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.system import SystemSetting
from app.schemas.report import DownloadSettingsRead
from app.schemas.user import UserPublic
from app.services.reports import log_admin_action


DOWNLOAD_URL_KEY = "download.url"
DOWNLOAD_PC_URL_KEY = "download.pc_url"
DOWNLOAD_ANDROID_URL_KEY = "download.android_url"
DOWNLOAD_SCRATCH_URL_KEY = "download.scratch_url"
DOWNLOAD_VERSION_KEY = "download.version"
DOWNLOAD_PC_VERSION_KEY = "download.pc_version"
DOWNLOAD_MOBILE_VERSION_KEY = "download.mobile_version"
DEFAULT_SCRATCH_DOWNLOAD_URL = "https://assets.ccw.site/extension/kukechat"


async def get_download_url(db: AsyncSession) -> str | None:
    item = await _get_setting(db, DOWNLOAD_PC_URL_KEY) or await _get_setting(db, DOWNLOAD_URL_KEY)
    value = item.value.strip() if item and item.value else ""
    return value or None


async def get_public_download_settings(db: AsyncSession) -> DownloadSettingsRead:
    return await get_download_settings(db)


async def get_download_settings(db: AsyncSession) -> DownloadSettingsRead:
    pc_item = await _get_setting(db, DOWNLOAD_PC_URL_KEY)
    legacy_item = await _get_setting(db, DOWNLOAD_URL_KEY)
    android_item = await _get_setting(db, DOWNLOAD_ANDROID_URL_KEY)
    scratch_item = await _get_setting(db, DOWNLOAD_SCRATCH_URL_KEY)
    version_item = await _get_setting(db, DOWNLOAD_VERSION_KEY)
    pc_version_item = await _get_setting(db, DOWNLOAD_PC_VERSION_KEY)
    mobile_version_item = await _get_setting(db, DOWNLOAD_MOBILE_VERSION_KEY)
    latest_item = max(
        [item for item in (pc_item, legacy_item, android_item, scratch_item, version_item, pc_version_item, mobile_version_item) if item is not None],
        key=lambda item: item.updated_at,
        default=None,
    )
    if latest_item is None:
        return DownloadSettingsRead(scratch_url=DEFAULT_SCRATCH_DOWNLOAD_URL)
    return DownloadSettingsRead(
        url=(pc_item or legacy_item).value if (pc_item or legacy_item) else None,
        pc_url=(pc_item or legacy_item).value if (pc_item or legacy_item) else None,
        android_url=android_item.value if android_item else None,
        scratch_url=(scratch_item.value if scratch_item and scratch_item.value else DEFAULT_SCRATCH_DOWNLOAD_URL),
        version=(pc_version_item or version_item).value if (pc_version_item or version_item) else None,
        pc_version=(pc_version_item or version_item).value if (pc_version_item or version_item) else None,
        mobile_version=mobile_version_item.value if mobile_version_item else None,
        updated_at=latest_item.updated_at,
        updated_by=UserPublic.model_validate(latest_item.updated_by) if latest_item.updated_by else None,
    )


async def update_download_settings(db: AsyncSession, admin_id: int, pc_url: str | None, android_url: str | None, scratch_url: str | None, pc_version: str | None, mobile_version: str | None) -> DownloadSettingsRead:
    before = await get_download_settings(db)
    await _upsert_setting(db, DOWNLOAD_PC_URL_KEY, pc_url, admin_id)
    await _upsert_setting(db, DOWNLOAD_URL_KEY, pc_url, admin_id)
    await _upsert_setting(db, DOWNLOAD_ANDROID_URL_KEY, android_url, admin_id)
    await _upsert_setting(db, DOWNLOAD_SCRATCH_URL_KEY, scratch_url, admin_id)
    await _upsert_setting(db, DOWNLOAD_VERSION_KEY, pc_version, admin_id)
    await _upsert_setting(db, DOWNLOAD_PC_VERSION_KEY, pc_version, admin_id)
    await _upsert_setting(db, DOWNLOAD_MOBILE_VERSION_KEY, mobile_version, admin_id)

    await log_admin_action(
        db,
        admin_id,
        "download.settings.update",
        "system_setting",
        None,
        {
            "old_pc_url": before.pc_url,
            "old_android_url": before.android_url,
            "old_scratch_url": before.scratch_url,
            "old_pc_version": before.pc_version,
            "old_mobile_version": before.mobile_version,
            "new_pc_url": pc_url,
            "new_android_url": android_url,
            "new_scratch_url": scratch_url,
            "new_pc_version": pc_version,
            "new_mobile_version": mobile_version,
        },
    )
    await db.commit()
    return await get_download_settings(db)


async def export_public_download_metadata(db: AsyncSession) -> dict[str, str | bool | None]:
    settings = await get_download_settings(db)
    url = settings.pc_url
    filename = url.rstrip("/").split("/")[-1] if url else None
    return {
        "configured": bool(settings.pc_url or settings.android_url or settings.scratch_url),
        "url": url,
        "filename": filename,
        "pc_url": settings.pc_url,
        "android_url": settings.android_url,
        "scratch_url": settings.scratch_url,
        "version": settings.pc_version,
        "pc_version": settings.pc_version,
        "mobile_version": settings.mobile_version,
    }


async def _get_setting(db: AsyncSession, key: str) -> SystemSetting | None:
    result = await db.execute(select(SystemSetting).options(joinedload(SystemSetting.updated_by)).where(SystemSetting.key == key))
    return result.scalar_one_or_none()


async def _upsert_setting(db: AsyncSession, key: str, value: str | None, admin_id: int) -> None:
    item = await _get_setting(db, key)
    if item is None:
        db.add(SystemSetting(key=key, value=value, updated_by_id=admin_id))
        return
    item.value = value
    item.updated_by_id = admin_id
