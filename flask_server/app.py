"""
Flask MP3 配信サーバー（マルチサーバー対応版）
- /files/<guild_id>/<token>  → guild_id を照合してから MP3 配信
- /info/<guild_id>/<token>   → ファイル情報 JSON
- /health                    → ヘルスチェック

アクセス制御:
  URL の guild_id と メタデータの guild_id が一致しない → 403 Forbidden
  期限切れ → 410 Gone
  存在しない → 404 Not Found
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, send_file, jsonify, abort

_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

CACHE_DIR  = Path(os.getenv("CACHE_DIR", "/app/cache"))
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")

CACHE_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)


# ──────────────────────────────────────────────
# ヘルパー
# ──────────────────────────────────────────────

def _validate_token(token: str) -> bool:
    """トークンが 32文字の英数字かチェック（パストラバーサル対策）"""
    return token.isalnum() and len(token) == 32


def _validate_guild_id(guild_id: str) -> bool:
    """guild_id が数字のみかチェック"""
    return guild_id.isdigit()


def _read_meta(token: str) -> dict | None:
    """JSON ファイルを読み込んで返す。ファイル不在・パース失敗なら None。"""
    meta_path = CACHE_DIR / f"{token}.json"
    mp3_path  = CACHE_DIR / f"{token}.mp3"

    if not meta_path.exists() or not mp3_path.exists():
        return None

    try:
        with meta_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None


def _is_expired(meta: dict) -> bool:
    """メタデータの期限切れを判定する。"""
    return datetime.now(timezone.utc).timestamp() > meta.get("expires_at", 0)


def _delete_entry(token: str) -> None:
    """キャッシュファイルを削除する。"""
    for suffix in (".mp3", ".json"):
        try:
            (CACHE_DIR / f"{token}{suffix}").unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"削除失敗 {token}{suffix}: {e}")


def _load_meta(token: str) -> dict | None:
    """メタデータを読み込む。存在しない or 期限切れなら None。"""
    if not _validate_token(token):
        return None

    meta = _read_meta(token)
    if meta is None:
        return None

    if _is_expired(meta):
        _delete_entry(token)
        logger.info(f"期限切れ: {token}")
        return None

    return meta


# ──────────────────────────────────────────────
# ルーティング
# ──────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/files/<guild_id>/<token>")
def serve_file(guild_id: str, token: str):
    """
    MP3 ファイルを配信する。
    URL の guild_id とメタデータの guild_id が一致しないと 403。
    """
    if not _validate_guild_id(guild_id) or not _validate_token(token):
        abort(404)

    meta = _load_meta(token)

    if meta is None:
        abort(410)

    if meta.get("guild_id") != guild_id:
        logger.warning(
            f"guild_id 不一致: URL={guild_id} meta={meta.get('guild_id')} token={token}"
        )
        abort(403)

    mp3_path = CACHE_DIR / f"{token}.mp3"
    raw_name = meta.get("filename", f"{token}.mp3")
    safe_name = "".join(
        c for c in raw_name if c.isalnum() or c in " ._-"
    ).strip() or f"{token}.mp3"
    if not safe_name.endswith(".mp3"):
        safe_name += ".mp3"

    logger.info(f"配信: guild={guild_id} token={token} → {safe_name}")
    return send_file(
        str(mp3_path),
        as_attachment=True,
        download_name=safe_name,
        mimetype="audio/mpeg",
    )


@app.route("/info/<guild_id>/<token>")
def file_info(guild_id: str, token: str):
    """ファイル情報と残り時間を JSON で返す。"""
    if not _validate_guild_id(guild_id) or not _validate_token(token):
        abort(404)

    meta = _load_meta(token)
    if meta is None:
        abort(410)

    if meta.get("guild_id") != guild_id:
        abort(403)

    now = datetime.now(timezone.utc).timestamp()
    remaining = max(0, int(meta["expires_at"] - now))

    return jsonify({
        "token":             token,
        "title":             meta.get("title", ""),
        "filename":          meta.get("filename", ""),
        "expires_at":        meta.get("expires_at"),
        "remaining_seconds": remaining,
        "remaining_minutes": remaining // 60,
    })


# ──────────────────────────────────────────────
# エラーハンドラ
# ──────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return jsonify({"error": "アクセスが拒否されました", "code": 403}), 403

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "ファイルが見つかりません", "code": 404}), 404

@app.errorhandler(410)
def gone(e):
    return jsonify({"error": "リンクの有効期限が切れています", "code": 410}), 410


# ──────────────────────────────────────────────
# 起動
# ──────────────────────────────────────────────

if __name__ == "__main__":
    logger.info(f"Flask 配信サーバー起動: {FLASK_HOST}:{FLASK_PORT}")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)
