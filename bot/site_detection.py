"""yt-dlp の info.json からソース媒体を判定するユーティリティ。"""


def detect_site(info: dict) -> str:
    """extractor フィールドと URL から媒体識別子を返す。"""
    extractor = str(info.get("extractor") or info.get("extractor_key") or "").lower()
    if "youtube" in extractor:
        return "youtube"
    if "soundcloud" in extractor:
        return "soundcloud"
    if "bandcamp" in extractor:
        return "bandcamp"
    if "nicovideo" in extractor or "nico" in extractor:
        return "nicovideo"
    if "tiktok" in extractor:
        return "tiktok"
    if "spotify" in extractor:
        return "spotify"

    url = str(info.get("webpage_url") or info.get("url") or "").lower()
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "soundcloud.com" in url:
        return "soundcloud"
    if "bandcamp.com" in url:
        return "bandcamp"
    if "nicovideo.jp" in url:
        return "nicovideo"
    if "tiktok.com" in url:
        return "tiktok"
    if "spotify.com" in url:
        return "spotify"

    return "generic"
