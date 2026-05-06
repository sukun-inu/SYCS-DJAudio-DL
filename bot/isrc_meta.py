"""
Deezer API を使って MP3 のメタデータを補完するモジュール。
1. ISRC が取れている場合 → /track/isrc:{isrc} で完全一致検索
2. ISRC がない場合       → 媒体別クエリ最適化＋フォールバックリトライで検索
いずれも認証不要の公開エンドポイントを使用。
"""

import asyncio
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import aiohttp
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPOS, TPE1, TRCK
from mutagen.mp3 import MP3

from site_detection import detect_site

logger = logging.getLogger(__name__)

_DEEZER_ISRC   = "https://api.deezer.com/track/isrc:{}"
_DEEZER_SEARCH = "https://api.deezer.com/search"

# スコアリング閾値
_HIGH_SCORE = 0.80  # この閾値以上なら即採用
_MIN_SCORE  = 0.45  # この閾値未満は採用しない

# タイトルから除去するノイズパターン（括弧・鍵括弧・隅付き括弧）
_NOISE_BRACKET = re.compile(
    r"""
    [\(\[\【]                        # 開き括弧
    \s*
    (?:
        official\s+(?:music\s+)?(?:video|audio|mv|visualizer|lyric[s]?|clip)|
        (?:hd|4k|8k|hq)\s*(?:video)?|
        full\s+(?:song|album|version)|
        (?:audio|video)\s*(?:only)?|
        remaster(?:ed)?(?:\s+\d{4})?|
        (?:extended|original|radio|album|single)\s+(?:mix|edit|version)|
        from\s+[^\)\]\】]+|
        (?:feat|ft)\.?\s+[^\)\]\】]+|
        lyric[s]?|
        (?:m/?v|p/?v)|
        \d{4}(?:\s+version)?|
        live(?:\s+ver(?:sion)?)?|
        ver(?:sion)?\.?\s*\w*|
        short\s+ver(?:sion)?|
        music\s+video|
        visuali[sz]er|
        sub(?:title)?[s]?\s+(?:indo|english|日本語)|
        (?:日本語|英語|한국어|中文)\s*(?:字幕|ver)?
    )
    \s*
    [\)\]\】]                        # 閉じ括弧
    """,
    re.IGNORECASE | re.VERBOSE,
)

# 括弧の外にある feat./ft. 表現（末尾方向）
_FEAT_BARE = re.compile(r"\s+(?:feat|ft)\.?\s+.+$", re.IGNORECASE)

# 末尾の「- Topic」「/ Topic」形式（YouTubeの自動生成チャンネル名）
_TOPIC_SUFFIX = re.compile(r"\s*[-/]\s*Topic\s*$", re.IGNORECASE)

# 「Artist - Title」形式の区切り（全角ハイフン含む）
_DASH_SPLIT = re.compile(r"\s+[-－—–]\s+")


def _clean_title(title: str) -> str:
    """タイトルからノイズ表現を除去して検索に最適化する。"""
    t = _FEAT_BARE.sub("", title)   # 括弧外の feat. を先に除去
    t = _NOISE_BRACKET.sub("", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    t = re.sub(r"[\s\-_|/\\]+$", "", t).strip()
    return t or title


def _normalize_for_compare(text: str) -> str:
    """比較専用の正規化：Unicode正規化 → アクセント除去 → 小文字 → 記号スペース化。"""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _similarity(a: str, b: str) -> float:
    """正規化済み文字列の類似度を 0.0〜1.0 で返す。"""
    na, nb = _normalize_for_compare(a), _normalize_for_compare(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _score_result(item: dict, ref_title: str, ref_artist: str | None) -> float:
    """
    Deezer 候補と入力情報の一致度を 0.0〜1.0 で返す。
    タイトル類似度 65% ＋ アーティスト類似度 35%。
    """
    dz_title  = item.get("title") or ""
    dz_artist = (item.get("artist") or {}).get("name") or ""

    title_sim = _similarity(ref_title, dz_title)

    if ref_artist:
        artist_sim = _similarity(ref_artist, dz_artist)
        return title_sim * 0.65 + artist_sim * 0.35

    return title_sim


def _build_search_queries(title: str, artist: str | None, site: str, info: dict) -> list[str]:
    """
    媒体ごとに最適化した Deezer 検索クエリを優先度順で返す。
    先頭ほど精度が高く、後ろはフォールバック用の広いクエリ。
    """
    clean = _clean_title(title)
    queries: list[str] = []

    if site == "youtube":
        # YouTube は「アーティスト - タイトル」形式が多いので分解を試みる
        # uploader が「XXX - Topic」形式の場合はアーティスト名として使える
        uploader = _TOPIC_SUFFIX.sub("", str(info.get("uploader") or "")).strip()

        if _DASH_SPLIT.search(clean):
            parts = _DASH_SPLIT.split(clean, maxsplit=1)
            inferred_artist = parts[0].strip()
            inferred_track  = parts[1].strip()
            queries.append(f'artist:"{inferred_artist}" track:"{inferred_track}"')
            # アーティスト側が "feat." を含む場合に備えてトラックのみも追加
            queries.append(f'track:"{inferred_track}"')

        if artist:
            queries.append(f'artist:"{artist}" track:"{clean}"')
        elif uploader:
            queries.append(f'artist:"{uploader}" track:"{clean}"')

        queries.append(f'track:"{clean}"')
        # ノイズ除去前の生タイトルでも試みる（clean が短くなりすぎた場合の救済）
        if clean != title:
            raw_clean = _clean_title(_DASH_SPLIT.split(title, 1)[-1]) if _DASH_SPLIT.search(title) else clean
            if raw_clean and raw_clean not in queries:
                queries.append(f'track:"{raw_clean}"')

    elif site == "soundcloud":
        # SoundCloud はアーティスト情報が比較的正確
        if artist:
            queries.append(f'artist:"{artist}" track:"{clean}"')
        queries.append(f'track:"{clean}"')
        if artist:
            queries.append(f'"{artist}" "{clean}"')

    elif site == "bandcamp":
        # Bandcamp はアーティスト名とアルバム名が明確
        album = str(info.get("album") or "").strip()
        if artist and clean:
            queries.append(f'artist:"{artist}" track:"{clean}"')
        if artist and album:
            queries.append(f'artist:"{artist}" album:"{album}"')
        queries.append(f'track:"{clean}"')

    elif site == "tiktok":
        # TikTok は音楽情報が断片的なためシンプルに
        music_title = str(info.get("music_title") or "").strip()
        music_artist = str(info.get("music_author") or "").strip()
        if music_artist and music_title:
            queries.append(f'artist:"{music_artist}" track:"{music_title}"')
        if music_title:
            queries.append(f'track:"{music_title}"')
        if artist and clean:
            queries.append(f'artist:"{artist}" track:"{clean}"')
        queries.append(f'track:"{clean}"')

    elif site == "spotify":
        # Spotify はメタデータが最も整っている
        if artist:
            queries.append(f'artist:"{artist}" track:"{clean}"')
        queries.append(f'track:"{clean}"')

    elif site == "nicovideo":
        # ニコ動はタイトルに情報が集中（アーティスト情報は薄い）
        queries.append(f'track:"{clean}"')
        if clean != title:
            queries.append(f'track:"{title}"')

    else:
        # 汎用フォールバック
        if artist:
            queries.append(f'artist:"{artist}" track:"{clean}"')
        queries.append(f'track:"{clean}"')

    # 全ケース共通の最終フォールバック：クリーニング済みテキストをそのまま投げる
    bare = clean if clean else title
    if bare not in queries:
        queries.append(bare)

    # 重複を保ちながら順序を維持して返す
    seen: set[str] = set()
    deduped: list[str] = []
    for q in queries:
        if q and q not in seen:
            seen.add(q)
            deduped.append(q)
    return deduped


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
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"Deezer ISRC 検索エラー: {e}")
        return None


async def _fetch_by_search(
    title: str,
    artist: str | None,
    session: aiohttp.ClientSession,
    site: str = "generic",
    info: dict | None = None,
) -> dict | None:
    """
    媒体別クエリリストを順に試し、スコアリングでベストマッチを返す。
    - 各クエリで最大 5 候補を取得してタイトル・アーティスト類似度を計算
    - スコアが _HIGH_SCORE 以上なら即採用
    - 全クエリ消化後、_MIN_SCORE 以上の最高スコア候補を返す
    """
    clean = _clean_title(title)

    # 「Artist - Title」形式を分解してスコアリングの基準を設定
    ref_title  = clean
    ref_artist = artist
    if _DASH_SPLIT.search(clean):
        parts = _DASH_SPLIT.split(clean, maxsplit=1)
        ref_artist = ref_artist or parts[0].strip()
        ref_title  = parts[1].strip()

    queries = _build_search_queries(title, artist, site, info or {})
    logger.debug(f"Deezer 検索クエリ候補 ({site}): {queries}")

    best_item:  dict | None = None
    best_score: float       = 0.0

    for q in queries:
        try:
            async with session.get(
                _DEEZER_SEARCH,
                params={"q": q, "limit": 5},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    continue
                data  = await resp.json(content_type=None)
                items = data.get("data") or []

            for item in items:
                score = _score_result(item, ref_title, ref_artist)
                if score > best_score:
                    best_score = score
                    best_item  = item
                if score >= _HIGH_SCORE:
                    logger.info(
                        f"Deezer 高精度ヒット (score={score:.2f}): q={q!r} → {item.get('title')!r}"
                    )
                    return item

            if not items:
                logger.debug(f"Deezer 検索ミス: q={q!r}")

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"Deezer テキスト検索エラー (q={q!r}): {e}")

    if best_item and best_score >= _MIN_SCORE:
        logger.info(
            f"Deezer ベストマッチ採用 (score={best_score:.2f}): {best_item.get('title')!r}"
        )
        return best_item

    if best_item:
        logger.info(f"Deezer: スコア不足 (best={best_score:.2f}) → スキップ")
    return None


async def _download_bytes(url: str, session: aiohttp.ClientSession) -> bytes | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
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
    ISRC があれば完全一致検索、なければ媒体別クエリ最適化でテキスト検索。
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
    site = detect_site(info)

    if not isrc and not title:
        return False

    async with aiohttp.ClientSession() as session:
        data = None

        if isrc:
            data = await _fetch_by_isrc(isrc, session)

        if data is None and title:
            data = await _fetch_by_search(title, artist, session, site=site, info=info)

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
