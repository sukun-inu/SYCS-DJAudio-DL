"""
Deezer API を使って MP3 のメタデータを補完するモジュール。
1. ISRC が取れている場合 → /track/isrc:{isrc} で完全一致検索
2. ISRC がない場合       → /search?q=... でタイトル+アーティスト検索（先頭1件）
いずれも認証不要の公開エンドポイントを使用。
"""

import asyncio
import logging
from pathlib import Path

import aiohttp
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPOS, TPE1, TRCK
from mutagen.mp3 import MP3

logger = logging.getLogger(__name__)

_DEEZER_ISRC   = "https://api.deezer.com/track/isrc:{}"
_DEEZER_SEARCH = "https://api.deezer.com/search"


async def _fetch_by_isrc(isrc: str, session: aiohttp.ClientSession) -> dict | None:
    try:
        async with session.get(
            _DEEZER_ISRC.format(isrc),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            if "error" in data:
                logger.info(f"Deezer: ISRC={isrc} 未登録")
                return None
            return data
    except Exception as e:
        logger.warning(f"Deezer ISRC 検索エラー: {e}")
        return None


async def _fetch_by_search(title: str, artist: str | None, session: aiohttp.ClientSession) -> dict | None:
    q = f'artist:"{artist}" track:"{title}"' if artist else f'track:"{title}"'
    try:
        async with session.get(
            _DEEZER_SEARCH,
            params={"q": q, "limit": 1},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            items = data.get("data") or []
            if not items:
                return None
            logger.info(f"Deezer 検索ヒット: q={q!r} → {items[0].get('title')}")
            return items[0]
    except Exception as e:
        logger.warning(f"Deezer テキスト検索エラー: {e}")
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
    if cover and cover[:3] == b"\xff\xd8\xff":  # JPEG magic bytes
        tags.delall("APIC")
        tags.add(APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,
            desc="Cover",
            data=cover,
        ))

    tags.save(mp3_path)


def _cover_url(data: dict) -> str | None:
    album = data.get("album") or {}
    return album.get("cover_xl") or album.get("cover_big") or album.get("cover")


async def enrich_metadata(mp3_path: Path, info: dict) -> bool:
    """
    Deezer からメタデータを取得して MP3 タグを上書きする。
    ISRC があれば完全一致検索、なければタイトル+アーティストでテキスト検索。
    成功したら True、スキップ・失敗なら False を返す。
    """
    isrc   = info.get("isrc")
    title  = info.get("track") or info.get("title") or info.get("alt_title")
    artist = (
        info.get("artist")
        or info.get("album_artist")
        or info.get("creator")
        or info.get("uploader")
    )

    if not isrc and not title:
        return False

    async with aiohttp.ClientSession() as session:
        data = None

        if isrc:
            data = await _fetch_by_isrc(isrc, session)

        if data is None and title:
            data = await _fetch_by_search(title, artist, session)

        if not data:
            logger.info(f"Deezer: 該当なし ({mp3_path.name})")
            return False

        cover = await _download_bytes(_cover_url(data), session) if _cover_url(data) else None

    try:
        await asyncio.to_thread(_write_tags, mp3_path, data, cover)
        logger.info(f"Deezer メタデータ適用完了: {mp3_path.name}")
        return True
    except Exception as e:
        logger.warning(f"タグ書き込みエラー ({mp3_path.name}): {e}")
        return False
