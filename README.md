# Burnote

自動消滅する暗号化ノート共有サービス。E2EE（エンドツーエンド暗号化）により、サーバー側でもノート内容を閲覧できません。

## セットアップ

```bash
pip install -r requirements.txt
python app.py
```

## 本番デプロイ

```bash
gunicorn -c gunicorn.ctl app:app
```

### 環境変数

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `RATE_LIMIT_STORAGE_URI` | `memory://` | レートリミットのストレージバックエンド |

### ストレージ構成

- **ノートテキスト**: SQLite (`kemuri.db`)
- **添付ファイル**: ファイルシステム (`attachments/` ディレクトリ)
  - 1ファイル最大 3MB
  - 全体で最大 500MB
- 期限切れノートはバックグラウンドスレッド（60秒間隔）で自動削除されます


## Docker でのセルフホスト

```bash
# ビルド＆起動
docker compose up -d

# ログ確認
docker compose logs -f

# 停止
docker compose down
```

### 環境変数（Docker）

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `PORT` | `8000` | 公開ポート |
| `RATE_LIMIT_STORAGE_URI` | `memory://` | レートリミットのストレージ |

### Redis によるレートリミット共有（推奨）

デフォルトではインメモリストレージを使用します。gunicorn で複数ワーカーを動かす場合、ワーカー間でレートリミット状態が共有されず、プロセス再起動時にリセットされます。

本番環境では Redis の使用を推奨します：

```bash
# Redis インストール
sudo apt install redis-server

# 環境変数で Redis を指定
export RATE_LIMIT_STORAGE_URI=redis://localhost:6379
```

パスワード認証付きの場合：
```bash
export RATE_LIMIT_STORAGE_URI=redis://:yourpassword@localhost:6379/0
```

> **注意:** Redis が停止した場合、レートリミットの適用でエラーが発生します。Redis の可用性を確保してください。Redis なしで運用する場合は、デフォルトの `memory://` のまま使用できます。
