"""
ISRC を使って Deezer API からメタデータを取得し MP3 タグを上書きするモジュール。
Deezer の公開エンドポイント（認証不要）を使用。
"""

import asyncio
import logging
from pathlib import Path

import aiohttp
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPOS, TPE1, TRCK
from mutagen.mp3 import MP3

logger = logging.getLogger(__name__)

_DEEZER_URL = "https://api.deezer.com/track/isrc:{}"


async def _get_deezer(isrc: str, session: aiohttp.ClientSession) -> dict | None:
    url = _DEEZER_URL.format(isrc)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            if "error" in data:
                logger.info(f"Deezer: ISRC={isrc} 未登録 ({data['error'].get('message', '')})")
                return None
            return data
    except Exception as e:
        logger.warning(f"Deezer API エラー (ISRC={isrc}): {e}")
        return None


async def _download_bytes(url: str, session: aiohttp.ClientSession) -> bytes | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception as e:
        logger.warning(f"カバー画像取得エラー: {e}")
    return None


def _write_tags(mp3_path: Path, data: dict, cover: bytes | None) -> None:
    audio = MP3(mp3_path, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    title        = data.get("title")
    artist_name  = (data.get("artist") or {}).get("name")
    album_data   = data.get("album") or {}
    album_title  = album_data.get("title")
    release_date = data.get("release_date")  # "YYYY-MM-DD"
    track_pos    = data.get("track_position")
    disk_num     = data.get("disk_number")

    if title:
        tags["TIT2"] = TIT2(encoding=3, text=title)
    if artist_name:
        tags["TPE1"] = TPE1(encoding=3, text=artist_name)
    if album_title:
        tags["TALB"] = TALB(encoding=3, text=album_title)
    if release_date:
        tags["TDRC"] = TDRC(encoding=3, text=release_date[:4])
    if track_pos:
        tags["TRCK"] = TRCK(encoding=3, text=str(track_pos))
    if disk_num:
        tags["TPOS"] = TPOS(encoding=3, text=str(disk_num))
    if cover:
        tags.delall("APIC")
        tags["APIC"] = APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,
            desc="Cover",
            data=cover,
        )

    tags.save(mp3_path)


async def enrich_with_isrc(mp3_path: Path, info: dict) -> bool:
    """
    info.json の isrc フィールドを使って Deezer からメタデータを取得し MP3 に書き込む。
    成功したら True、スキップ・失敗なら False を返す。
    """
    isrc = info.get("isrc")
    if not isrc:
        return False

    async with aiohttp.ClientSession() as session:
        data = await _get_deezer(isrc, session)
        if not data:
            return False

        album_data = data.get("album") or {}
        cover_url = album_data.get("cover_xl") or album_data.get("cover_big") or album_data.get("cover")
        cover = await _download_bytes(cover_url, session) if cover_url else None

    try:
        await asyncio.to_thread(_write_tags, mp3_path, data, cover)
        logger.info(f"ISRC={isrc} のメタデータを Deezer から適用: {mp3_path.name}")
        return True
    except Exception as e:
        logger.warning(f"タグ書き込みエラー ({mp3_path.name}): {e}")
        return False
