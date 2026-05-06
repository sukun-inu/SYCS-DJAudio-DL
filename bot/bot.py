"""
Discord YT-DLP Bot（マルチサーバー対応版）
- 設定チャンネルに投稿された URL を自動検知
- yt-dlp + ffmpeg で MP3 変換・キャッシュ保存
- Flask 配信 URL は /files/<guild_id>/<token> 形式（サーバーごとに分離）
- コマンドは /setchannel のみ
"""

import json
import asyncio
import logging
import os
import re
import tempfile
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from cache import register_file, cache_cleanup_loop, update_discord_message
from config import (
    BASE_URL, CACHE_TTL, COOLDOWN_SECONDS, DL_CONCURRENCY,
    DL_TIMEOUT, FFMPEG_PATH, LOG_LEVEL, MAX_URLS_PER_MSG,
)
from isrc_meta import enrich_metadata
from site_detection import detect_site

# ──────────────────────────────────────────────
# ロギング
# ──────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 定数・設定
# ──────────────────────────────────────────────
CONFIG_PATH = Path("/app/data/config.json")
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

URL_PATTERN = re.compile(r"https?://[^\s]+")

# ──────────────────────────────────────────────
# 設定ファイルの読み書き
# ──────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"設定読み込み失敗: {e}")
    return {}


def save_config(config: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_watch_channel_id(guild_id: int) -> int | None:
    config = load_config()
    val = config.get(str(guild_id))
    if val is not None:
        return int(val)
    default = os.getenv("DEFAULT_CHANNEL_ID")
    return int(default) if default else None


# ──────────────────────────────────────────────
# yt-dlp ユーティリティ
# ──────────────────────────────────────────────

async def can_download(url: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "--simulate", "--quiet", "--no-warnings",
        "--ffmpeg-location", FFMPEG_PATH,
        url,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    return proc.returncode == 0


def _normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _format_title_from_metadata(meta: dict) -> str:
    title  = _normalize_text(meta.get("title"))
    artist = _normalize_text(
        meta.get("artist") or meta.get("album_artist")
        or meta.get("creator") or meta.get("uploader")
    )
    track = _normalize_text(
        meta.get("track") or meta.get("alt_title") or meta.get("release_title")
    )
    site = detect_site(meta)

    def _with_artist(first: str, second: str) -> str:
        if not first:
            return second
        if not second:
            return first
        if second.lower().startswith(first.lower()):
            return second
        return f"{first} - {second}"

    if not title:
        return artist or track or "unknown"

    if site == "youtube":
        if artist and track:
            return _with_artist(artist, track)
        if artist:
            return _with_artist(artist, title)
        if meta.get("uploader"):
            return _with_artist(_normalize_text(meta["uploader"]), title)
        return title

    if site == "soundcloud":
        return _with_artist(artist, title) if artist else title

    if site == "bandcamp":
        if artist:
            return _with_artist(artist, title)
        return _with_artist(track, title) if track else title

    if site == "nicovideo":
        return title

    if site == "tiktok":
        uploader = _normalize_text(meta.get("uploader"))
        return _with_artist(uploader, title) if uploader else title

    if site == "spotify":
        if artist and track:
            return _with_artist(artist, track)
        return _with_artist(artist, title) if artist else title

    if artist:
        return _with_artist(artist, title)
    return _with_artist(track, title) if track else title


def _load_info_json(mp3_path: Path) -> dict | None:
    info_path = mp3_path.with_suffix(".info.json")
    if not info_path.exists():
        return None
    try:
        return json.loads(info_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"info.json の読み込みに失敗しました: {e}")
        return None


async def download_as_mp3(url: str, output_dir: str) -> list[Path]:
    template = str(Path(output_dir) / "%(title).80s.%(ext)s")
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "-f", "bestaudio/best",
        "--format-sort", "asr,abr,acodec:opus",
        "--postprocessor-args", "ffmpeg:-q:a 0",
        "--write-info-json",
        "--embed-thumbnail",
        "--convert-thumbnails", "jpg",
        "--embed-metadata",
        "--concurrent-fragments", "4",
        "--buffer-size", "1M",
        "--http-chunk-size", "10M",
        "--retries", "5",
        "--socket-timeout", "30",
        "--no-playlist",
        "-o", template,
        "--ffmpeg-location", FFMPEG_PATH,
        "--no-warnings",
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")
        logger.error(f"yt-dlp エラー [{url}]: {err[-400:]}")
        raise RuntimeError(err[-400:])

    return sorted(Path(output_dir).glob("*.mp3"))


# ──────────────────────────────────────────────
# Bot 本体
# ──────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

_dl_semaphore: asyncio.Semaphore | None = None
_user_cooldown: dict[int, float] = {}
processing: set[tuple] = set()


def _get_semaphore() -> asyncio.Semaphore:
    global _dl_semaphore
    if _dl_semaphore is None:
        _dl_semaphore = asyncio.Semaphore(DL_CONCURRENCY)
    return _dl_semaphore


@bot.event
async def on_ready():
    logger.info(f"Bot 起動: {bot.user} (ID: {bot.user.id})")
    logger.info(f"BASE_URL: {BASE_URL} / CACHE_TTL: {CACHE_TTL}s")
    asyncio.create_task(cache_cleanup_loop(bot=bot, interval=60))
    try:
        synced = await bot.tree.sync()
        logger.info(f"スラッシュコマンド同期: {len(synced)} 件")
    except Exception as e:
        logger.error(f"コマンド同期失敗: {e}")


# ──────────────────────────────────────────────
# /setchannel
# ──────────────────────────────────────────────

@bot.tree.command(
    name="setchannel",
    description="URL を監視して MP3 リンクを送信するチャンネルを設定します",
)
@app_commands.describe(channel="設定するチャンネル")
@app_commands.checks.has_permissions(manage_channels=True)
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    config = load_config()
    config[str(interaction.guild_id)] = channel.id
    save_config(config)

    embed = discord.Embed(title="✅ チャンネル設定完了", color=discord.Color.green())
    embed.description = (
        f"{channel.mention} を監視チャンネルに設定しました。\n"
        "このチャンネルに URL を投稿すると自動で MP3 リンクを返信します。\n\n"
        f"🔗 配信 URL: `{BASE_URL}`\n"
        f"⏱️ キャッシュ有効期間: `{CACHE_TTL // 60}分`"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    logger.info(f"Guild {interaction.guild_id}: 監視チャンネルを #{channel.name} に設定")


@setchannel.error
async def setchannel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ チャンネル管理権限が必要です。", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ エラー: {error}", ephemeral=True)


# ──────────────────────────────────────────────
# on_message — URL 自動検知
# ──────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not message.guild:
        return

    watch_ch_id = get_watch_channel_id(message.guild.id)
    if watch_ch_id is None or message.channel.id != watch_ch_id:
        return

    urls = URL_PATTERN.findall(message.content)[:MAX_URLS_PER_MSG]
    if not urls:
        return

    now = time.monotonic()
    last = _user_cooldown.get(message.author.id, 0)
    if now - last < COOLDOWN_SECONDS:
        remaining = int(COOLDOWN_SECONDS - (now - last))
        await message.reply(f"⏱️ {remaining}秒後に再試行してください。", mention_author=False)
        return
    _user_cooldown[message.author.id] = now

    await asyncio.gather(*[process_url(message, url) for url in urls])
    await bot.process_commands(message)


# ──────────────────────────────────────────────
# ダウンロード処理
# ──────────────────────────────────────────────

async def _download_and_register(url: str, guild_id: int, tmpdir: str) -> list[tuple[str, str]]:
    """MP3 をダウンロードしてメタデータを補完・キャッシュ登録し、(title, token) のリストを返す。"""
    mp3_files = await download_as_mp3(url, tmpdir)
    if not mp3_files:
        raise RuntimeError("MP3 ファイルが生成されませんでした")

    results: list[tuple[str, str]] = []
    for mp3 in mp3_files:
        info_meta = _load_info_json(mp3)
        if info_meta:
            await enrich_metadata(mp3, info_meta)
        title_text = _format_title_from_metadata(info_meta) if info_meta else mp3.stem
        token = register_file(mp3, source_url=url, title=title_text, guild_id=guild_id)
        results.append((title_text, token))
    return results


def _build_result_embed(results: list[tuple[str, str]], guild_id: int) -> discord.Embed:
    """ダウンロード結果の Discord Embed を構築する。"""
    ttl_min = CACHE_TTL // 60
    embed = discord.Embed(
        title="🎵 MP3 準備完了",
        color=discord.Color.blurple(),
        description=f"⏱️ リンクは **{ttl_min}分後** に失効します",
    )
    for title, token in results:
        link = f"{BASE_URL}/files/{guild_id}/{token}"
        embed.add_field(
            name=f"📥 {title[:50]}",
            value=f"[ダウンロード]({link})\n`{link}`",
            inline=False,
        )
    return embed


async def process_url(message: discord.Message, url: str) -> None:
    """URL を処理して Flask 配信リンクを返信する。"""
    key = (message.guild.id, message.id, url)
    if key in processing:
        return
    processing.add(key)

    try:
        await message.add_reaction("⏳")
        logger.info(f"URL検知 guild={message.guild.id} [{message.author}]: {url}")

        if not await can_download(url):
            await message.remove_reaction("⏳", bot.user)
            await message.add_reaction("❓")
            return

        async with _get_semaphore():
            with tempfile.TemporaryDirectory() as tmpdir:
                results = await asyncio.wait_for(
                    _download_and_register(url, message.guild.id, tmpdir),
                    timeout=DL_TIMEOUT,
                )

        await message.remove_reaction("⏳", bot.user)
        await message.add_reaction("✅")

        embed = _build_result_embed(results, message.guild.id)
        embed.set_footer(text=f"リクエスト: {message.author.display_name}")
        reply_msg = await message.reply(embed=embed, mention_author=False)
        for _, token in results:
            update_discord_message(token, reply_msg.channel.id, reply_msg.id)
        logger.info(f"完了 guild={message.guild.id} [{message.author}]: {len(results)} ファイル")

    except asyncio.TimeoutError:
        logger.error(f"タイムアウト [{url}]")
        try:
            await message.remove_reaction("⏳", bot.user)
            await message.add_reaction("❌")
            await message.reply("⚠️ タイムアウトしました。時間をおいて再試行してください。", mention_author=False)
        except discord.HTTPException:
            pass
    except Exception as e:
        logger.exception(f"処理失敗 [{url}]: {e}")
        try:
            await message.remove_reaction("⏳", bot.user)
            await message.add_reaction("❌")
            await message.reply(
                f"⚠️ ダウンロードに失敗しました\n```\n{str(e)[:300]}\n```",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
    finally:
        processing.discard(key)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("環境変数 DISCORD_TOKEN が設定されていません。")
    bot.run(token)
