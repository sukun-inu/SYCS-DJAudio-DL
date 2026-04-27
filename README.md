# 🎵 Discord YT-DLP Bot

設定したチャンネルに URL を投稿するだけで、自動的に MP3 に変換し  
**ダウンロードリンクを返信**する Discord Bot です。

---

## 🏗️ アーキテクチャ

```
[Discord]
  ユーザーが URL を投稿
       ↓
[bot コンテナ]
  yt-dlp + ffmpeg で MP3 変換
  /app/cache/ に保存（TTL 後に自動削除）
       ↓ 共有 Volume (mp3_cache)
[flask コンテナ]
  /files/<token> で MP3 を配信
       ↓
[Discord]
  ダウンロードリンクを返信
```

---

## 📋 必要なもの

- Docker / Docker Compose がインストールされた PC またはサーバー
- Discord アカウント
- Git

---

## 🚀 セットアップ手順

### Step 1 — Discord Bot を作成する

1. [Discord Developer Portal](https://discord.com/developers/applications) を開く
2. **New Application** → 名前を入力して作成
3. 左メニュー **Bot** → **Reset Token** → トークンをコピーして保存
4. **Privileged Gateway Intents** で以下を **ON**：
   - `SERVER MEMBERS INTENT`
   - `MESSAGE CONTENT INTENT` ← **必須。これがないと URL を読めません**
5. 左メニュー **OAuth2 → URL Generator**
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`, `Add Reactions`
6. 生成された URL でサーバーに招待

---

### Step 2 — リポジトリをクローン

```bash
git clone <あなたのリポジトリURL>
cd discord-ytdlp-bot
```

---

### Step 3 — 環境変数を設定する

```bash
cp .env.example .env
```

`.env` を開いて編集：

```env
# 必須: Discord トークン
DISCORD_TOKEN=xxxxxxxxxxxxxxxxxxxx

# 必須: Flask サーバーの公開 URL（ドメイン決まったら変更）
BASE_URL=http://localhost:5000

# 任意: キャッシュ有効期間（秒）デフォルト 600 = 10分
CACHE_TTL_SECONDS=600
```

> ⚠️ `.env` は絶対に Git にコミットしないでください（`.gitignore` で除外済み）

---

### Step 4 — 起動

```bash
docker compose up -d
```

起動確認：
```bash
docker compose logs -f
```

`Bot 起動: ...` と `Flask 配信サーバー起動: ...` が表示されれば成功！

---

### Step 5 — 監視チャンネルを設定

Discord でチャンネルに移動して：

```
/setchannel #チャンネル名
```

> チャンネル管理権限が必要です

---

## 🎮 使い方

設定したチャンネルに URL を投稿するだけ：

```
https://www.youtube.com/watch?v=xxxx
```

| リアクション | 意味 |
|---|---|
| ⏳ | 変換中 |
| ✅ | 完了（リンクを返信） |
| ❓ | 対応していない URL |
| ❌ | エラー（詳細を返信） |

完了すると以下のような Embed が返信されます：

```
🎵 MP3 準備完了
⏱️ リンクは 10分後 に失効します

📥 曲のタイトル
[ダウンロード](http://your-domain.com/files/xxxxxxxxxxxx)
```

---

## 🌐 ドメインを設定するとき

`.env` の `BASE_URL` を変更して再起動するだけです：

```env
BASE_URL=https://music.example.com
```

```bash
docker compose restart bot
```

Nginx などのリバースプロキシを使う場合は `docker-compose.yml` の ports を  
`"127.0.0.1:5000:5000"` に変更してください（外部への直接公開を防ぐため）。

---

## ⚙️ 環境変数一覧

| 変数名 | 必須 | 説明 | デフォルト |
|---|---|---|---|
| `DISCORD_TOKEN` | ✅ | Discord Bot トークン | — |
| `BASE_URL` | ✅ | Flask サーバーの公開 URL | `http://localhost:5000` |
| `CACHE_TTL_SECONDS` | — | MP3 キャッシュ有効期間（秒） | `600`（10分）|
| `FLASK_PORT` | — | Flask サーバーのポート番号 | `5000` |
| `DEFAULT_CHANNEL_ID` | — | デフォルトの監視チャンネル ID | 未設定 |

---

## 🔧 管理コマンド

```bash
# ログを見る
docker compose logs -f

# Bot だけ再起動（BASE_URL 変更後など）
docker compose restart bot

# 全体を停止
docker compose down

# コードを更新して再ビルド
docker compose up -d --build
```

---

## 🛠️ Flask API エンドポイント

| パス | 説明 |
|---|---|
| `GET /files/<token>` | MP3 ファイルをダウンロード |
| `GET /info/<token>` | ファイル情報・残り時間を JSON で返す |
| `GET /health` | ヘルスチェック |

---

## ❓ トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| Bot がオフライン | `DISCORD_TOKEN` が間違い → `.env` を確認 |
| URL に反応しない | `MESSAGE CONTENT INTENT` が OFF → Developer Portal で ON に |
| リンクが開けない | `BASE_URL` が正しくない → `.env` を確認して `docker compose restart bot` |
| ❓ になる | yt-dlp 非対応のサービス or 非公開動画 |
| ❌ になる | yt-dlp のバージョンが古い → `docker compose up -d --build` |
