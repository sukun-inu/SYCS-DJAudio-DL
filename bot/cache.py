"""
キャッシュ管理モジュール（マルチサーバー対応版）
- トークンに guild_id を紐づけて保存
- Flask 側で guild_id を照合してアクセス制御
- TTL 経過後に自動削除
"""

import os
import json
import re
import uuid
import shutil
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CACHE_DIR = Path(os.getenv("CACHE_DIR", "/app/cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "600"))


def _sanitize_filename(value: str, fallback: str = "file") -> str:
    value = str(value or "").strip()
    value = value.replace("/", " ").replace("\\", " ")
    value = re.sub(r'[<>:"|?*\\]', "", value)
    value = re.sub(r"\s+", " ", value).strip()
    safe = "".join(c for c in value if c.isalnum() or c in " ._-()[]{}")
    safe = safe.strip()[:120]
    if not safe:
        safe = fallback
    return safe


# ──────────────────────────────────────────────
# 登録
# ──────────────────────────────────────────────

def register_file(mp3_path: Path, source_url: str, title: str, guild_id: int) -> str:
    """
    MP3 をキャッシュに登録してトークンを返す。
    guild_id をメタデータに含めてサーバーごとのアクセス制御に使う。
    """
    token = uuid.uuid4().hex
    dest_mp3  = CACHE_DIR / f"{token}.mp3"
    meta_path = CACHE_DIR / f"{token}.json"

    shutil.move(str(mp3_path), dest_mp3)

    safe_title = _sanitize_filename(title, fallback=token)
    expires_at = datetime.now(timezone.utc).timestamp() + CACHE_TTL
    meta = {
        "token":      token,
        "guild_id":   str(guild_id),   # ← サーバー ID を記録
        "title":      title,
        "source_url": source_url,
        "filename":   f"{safe_title}.mp3",
        "expires_at": expires_at,
    }
    with meta_path.open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    logger.info(f"キャッシュ登録: {token} guild={guild_id} ({safe_title}) TTL={CACHE_TTL}s")
    return token


def update_discord_message(token: str, channel_id: int, message_id: int) -> None:
    """キャッシュエントリに Discord 返信メッセージ情報を記録する。"""
    meta_path = CACHE_DIR / f"{token}.json"
    try:
        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)
        meta["discord_channel_id"] = str(channel_id)
        meta["discord_message_id"] = str(message_id)
        with meta_path.open("w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Discord メッセージ情報の更新失敗 {token}: {e}")


# ──────────────────────────────────────────────
# 取得
# ──────────────────────────────────────────────

def get_meta(token: str) -> dict | None:
    """メタデータを返す。存在しない or 期限切れなら None。"""
    meta_path = CACHE_DIR / f"{token}.json"
    mp3_path  = CACHE_DIR / f"{token}.mp3"

    if not meta_path.exists() or not mp3_path.exists():
        return None

    try:
        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        return None

    if datetime.now(timezone.utc).timestamp() > meta["expires_at"]:
        _delete_entry(token)
        return None

    return meta


# ──────────────────────────────────────────────
# 削除
# ──────────────────────────────────────────────

def _delete_entry(token: str) -> None:
    for suffix in (".mp3", ".json"):
        p = CACHE_DIR / f"{token}{suffix}"
        try:
            p.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"削除失敗 {p}: {e}")
    logger.info(f"キャッシュ削除: {token}")


# ──────────────────────────────────────────────
# バックグラウンド掃除（asyncio）
# ──────────────────────────────────────────────

async def cache_cleanup_loop(bot=None, interval: int = 60) -> None:
    """interval 秒ごとに期限切れキャッシュを掃除するループ。"""
    logger.info(f"キャッシュ掃除ループ開始（{interval}秒間隔）")
    while True:
        await asyncio.sleep(interval)
        await _cleanup_expired(bot)


async def _cleanup_expired(bot=None) -> None:
    now = datetime.now(timezone.utc).timestamp()
    deleted = 0
    for meta_path in CACHE_DIR.glob("*.json"):
        try:
            with meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            if now > meta.get("expires_at", 0):
                if bot is not None:
                    channel_id = meta.get("discord_channel_id")
                    message_id = meta.get("discord_message_id")
                    if channel_id and message_id:
                        try:
                            channel = bot.get_channel(int(channel_id))
                            if channel:
                                msg = await channel.fetch_message(int(message_id))
                                await msg.delete()
                                logger.info(f"Discord メッセージ削除: {message_id}")
                        except Exception as e:
                            logger.warning(f"メッセージ削除失敗 {message_id}: {e}")
                _delete_entry(meta["token"])
                deleted += 1
        except Exception as e:
            logger.warning(f"掃除中にエラー {meta_path}: {e}")
    if deleted:
        logger.info(f"期限切れキャッシュ {deleted} 件を削除")
