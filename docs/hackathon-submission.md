# Safe Memory Platform — ハッカソン提出資料

> **AIエージェントのための、持ち運べる・安全な・再利用可能な「知識ファイル」基盤**
> プライベートなデータ（会計・FX・業務ナレッジ）を、ポリシー付きのポータブルなメモリパック（`.smp.json`）に変換し、URL経由でWeb・ChatGPT・Claudeから共有・再利用できるようにする。

- **本番URL**: https://smp.sdesigner.tokyo
- **稼働状況**: ライブ稼働中（Alibaba Cloud ECS、HTTPS、Qwen Cloud連携）
- **提出時点のステータス**: 全機能デプロイ済み・E2E検証済み / **171 passed, 3 skipped**

---

## 1. 課題（Problem）

現在のAIエージェント（ChatGPT / Claude 等）には、根本的な制約がある。

1. **記憶が持ち運べない** — チャットのコンテキストはセッションごとに消える。過去プロジェクトの知識を次に持ち越せない。
2. **ファイルを渡し合えない** — LLMのAction/MCPはファイルのバイト列を送受信できない（JSON/テキストのみ、かつ約45秒のタイムアウト）。「このフォルダの中身を全部使って」が物理的にできない。
3. **機密の扱いが雑** — 生データをそのままLLMに投げると、SECRET/機密情報まで外部モデルに流れてしまう。企業データでは致命的。
4. **再利用の単位がない** — 「1つのプロジェクトの知識」を、検証可能・改ざん検知可能な形で1ファイルにまとめて渡す標準が存在しない。

## 2. 解決策（Solution）

**Safe Memory Pack** という、ポータブルでポリシー付きの「知識ファイル」を中心に据えた交換ネットワークを構築した。

- 人／エージェントが、自分のデータを **1つのパックファイル（`.smp.json`）** に変換する。
- パックは **URLで共有** できる（LLMはバイト列を送れないが、URLは渡せる → **URLが知識流通の「通貨」**）。
- 受け手のGPT/Claudeは `importPackByRef(url)` でパックを取り込み、そのまま知識として利用・検索できる。
- **機密レベル（classification）とポリシー**が各エントリに埋め込まれ、SECRETは決して外部LLMに送られない。
- **改ざん検知**（sha256のハッシュチェーン台帳）付き。

### Safe Memory Pack とは

`.smp.json` は「ベクトルDBのクローン」ではなく、**自己完結した知識ファイル**。中身は：

| 要素 | 内容 |
|---|---|
| **entries** | テキスト本文（原本）＋ Qwen埋め込み（意味検索用ベクトル）＋ キーワード ＋ メタデータ |
| **classification** | `PUBLIC` / `SHAREABLE` / `INTERNAL` / `CONFIDENTIAL` / `SECRET` / `EPHEMERAL` の6段階 |
| **policy flags** | そのエントリを「検索に使ってよいか / LLMに送ってよいか / エクスポートしてよいか」 |
| **provenance** | 各エントリの出所（どのファイル由来か） |
| **ledger** | 追記専用のハッシュチェーン（各ブロックが前ブロックのsha256を封入 → 改ざん検知） |

**バックエンドが強制するポリシー**:
- `CONFIDENTIAL` / `SECRET` は、明示許可がない限りエクスポートから除外。
- `SECRET` は **絶対に** 外部LLMに送信しない。
- エクスポート時に機微テキストをリダクト可能。
- 全ファイルアクセスは `SAFE_MEMORY_ROOT` 配下に閉じ込め（サンドボックス）。

## 3. 3つのコアサービス

1. **Memory Forge（鍛造）** — 一時的なユーザーデータからSafe Memory Packを生成する。
2. **Memory Lens（検証）** — パックを検索・検証・監査する。
3. **Memory Workspace（活用）** — パックを使ってエージェントにプロジェクトタスクを実行させる。

## 4. アーキテクチャ / 技術スタック

```
[ユーザー / GPT (Actions) / Claude (MCP) / Web]
        │  HTTPS（URL＝知識の通貨）
        ▼
[Caddy] ── Let's Encrypt自動TLS ── smp.sdesigner.tokyo
        │  reverse_proxy → localhost:8787
        ▼
[ASGIルートディスパッチ]
   ├─ /mcp  → ネイティブMCPサーバー（Streamable HTTP / Claude等が接続）
   └─ /*    → FastAPI backend（REST API / GPT Actions・Web）
        ▼
[FastAPI backend]  (Docker / Alibaba Cloud ECS, CentOS 7)
   ├─ 認証: APIキー middleware（X-Safe-Memory-Key）＋ OpenAPI securitySchemes宣言
   ├─ MCP認証: pure-ASGI（Bearer / X-Safe-Memory-Key 両対応・SSE安全）
   ├─ Memory Forge: chunk → embed → classify → pack化
   ├─ ポリシーエンジン: classification / export / redaction
   ├─ 台帳: sha256ハッシュチェーン（改ざん検知）
   ├─ ジョブ管理: retention（session / process_and_return / server_vault）＋ TTL
   └─ ワンタイムアップロードリンク（SAS的・キー入力不要）
        │
        ├─→ [Qwen Cloud]（OpenAI互換API）
        │      embeddings: text-embedding-v4 / chat: qwen-flash
        │      ※キー欠落・失敗時は決定論的フォールバックでデモが落ちない
        │
        └─→ [Alibaba OSS]  一時パックのホスティング
               署名なし安定トークンURL /api/packs/dl/{token}
               → ローカル失効後はOSS署名URLへ307リダイレクト
```

**技術要素**
- **言語/FW**: Python 3 / FastAPI / Uvicorn / Pydantic
- **AI**: Qwen Cloud（Alibaba DashScope, OpenAI互換エンドポイント）— 埋め込み・分類・要約
- **ストレージ**: ローカルファースト（DB／ベクトルDBなし）。パックは `.smp.json`。一時ホスティングにOSS。
- **インフラ**: Docker Compose / Alibaba Cloud ECS / Caddy（自動HTTPS）/ 独自ドメイン `sdesigner.tokyo`
- **連携**: ChatGPT Custom GPT Actions（OpenAPIスキーマ）/ **Claude ネイティブMCP（Streamable HTTP, `mcp` SDK）** / Web UI
- **規模**: バックエンド約5,500行 / テスト約2,900行・20ファイル・**182 passed**

## 5. 実装済みの主要機能（すべて本番稼働・検証済み）

### エージェント連携（GPT: 16の公開Action ／ Claude: 13のMCPツール）
**GPT Actions（OpenAPI）**: `buildMemoryPack` / `buildPackFromUrl` / `queryMemoryPack` / `exportMemoryPack` / `verifyMemoryPack` / `importPackByRef` / `getAgentCatalog` / `runProjectWithMemory` / `createUploadLink` / `getUploadLinkResult` / ジョブ系（`getJob` / `getJobDownload` / `cleanupJobs` / `deleteJob`）/ `append` / `health`

**Claude ネイティブMCP（`/mcp`, Streamable HTTP）**: `health` / `build_pack_from_url` / `get_job` / `query_memory_pack` / `import_pack_by_ref` / `export_memory_pack` / `verify_memory_pack` / `get_agent_catalog` / `create_upload_link` / `get_upload_link_result` / `build_memory_pack` / `append` / `run_project_with_memory`。各ツールは既存RESTハンドラを**in-processで再利用**（ロジック二重化なし）。

### 主要ワークフロー
- **フォルダ丸ごとパック化** — Web `/upload` にフォルダ/複数ファイルをドラッグ&ドロップ → ブラウザ側でzip化 → サーバーが1パックに統合（ファイルごとのprovenance付き）→ OSS共有URL発行。
- **ワンタイム・アップロードリンク（SAS的）** — `createUploadLink` で発行した `/u/{token}` は **APIキー入力欄なし**・単回使用・TTL付き・スコープ限定（アップロードのみ、catalog/query等は自動401）。「ユーザーにキーを入力させない」正しい設計。
- **URL経由のパック取り込み** — `importPackByRef(url)` で任意のHTTPS URLからパックを取得・再利用。
- **リテンション管理** — `session` / `process_and_return`（処理後に生データ削除）/ `server_vault` の3モード＋TTL自動クリーンアップ。
- **マルチプラットフォーム連携** — 同一バックエンドを **GPT（Actions/OpenAPI）・Claude（ネイティブMCP）・Web** の3面から利用可能。GPTとClaudeで機能差を出さない。
- **安全なフォールバック** — Qwenキー欠落・呼び出し失敗時は決定論的ハッシュ埋め込み＆ヒューリスティック分類で**デモが決して落ちない**。

## 6. 技術的に解いた難問（Engineering Highlights）

このプロジェクトの価値は「動くデモ」だけでなく、**本番でしか出会えない実問題を根本原因まで特定して外科的に修正した**点にある。

### (1) LLMはファイルを送れない → 「URL＝通貨」設計
GPT ActionsもリモートMCPもバイト列を送受信できない。この制約を逆手に取り、**パックをサーバーにホストしURLで参照**する設計に統一。`importPackByRef(url)` が任意HTTPSから取り込む。

### (2) 匿名でのSharePoint/OneDriveフォルダ読み取りは不可能（MS仕様）
`:f:` フォルダ共有リンクを徹底検証（`&as=zip` / `guestaccess.aspx` / 匿名Graph API 全滅、返るのはHTMLシェルのみ）。**Microsoftの仕様上、匿名フォルダ読み取りは不可**と結論し、明確なFAILEDメッセージ＋Web/ワンタイムリンクのアップロード導線に振り替えた。

### (3) FXパックが「延々PROCESSING」（15〜20分）→ 2.1秒
クラッシュではなかった。根本原因は **`source_language=ja` が、純ASCII構造データ（272行の取引ID・数値・USDJPY）まで全部翻訳対象にしていた**こと。数値レコードが番号付き翻訳プロトコルのアライメントを破壊 → 1件ずつフォールバック（30コール/バッチ）→ 150〜200回超の逐次呼び出し。
- **修正**: 翻訳判定を「実際に日本語文字を含む行だけ」に変更（content-gate）。さらに翻訳を**既定オフ**にし、分類も明示指定時はper-entry LLM分類をスキップ（明白なSECRETだけヒューリスティックで昇格）。
- **結果**: 15〜20分 → **2.1秒**（LLM chat呼び出しゼロ、embeddingsのみ）。

### (4) GPTの403エラー = サーバー起因ではなかった
本番を実測し、我々のサーバーは認証失敗時に**必ず401**（403は一切返さない）ことを証明。403は**ChatGPT Actionsゲートウェイがサーバー到達前にブロック**していた＝GPT側のAPIキー未設定（クローン/スキーマ再取り込みでキーが消える）。
- **恒久対策**: OpenAPIに `securitySchemes.SafeMemoryApiKey` を宣言し、GPTがスキーマ取り込み時に認証を自動認識するようにした。

### (5) ダウンロードURLが間欠的に壊れる（SignatureDoesNotMatch）
OSS署名URLをGPTに直接渡すと、base64署名中の `+` `/` `=` がChatGPTの描画/転送層で壊れ、間欠的に `SignatureDoesNotMatch`（署名が1文字欠落）になっていた。
- **修正**: GPT/人間には**署名なしの安定トークンURL** `/api/packs/dl/{token}`（URL-safe文字のみ）を渡す。ローカルパック存在時は200ストリーム、TTL失効後は**サーバー裏でOSS署名URLへ307リダイレクト**（署名はブラウザだけが見るLocationヘッダに載り、GPTには露出しない）。
- **結果**: 転送で壊れる署名が一切露出しなくなり、ダウンロード破損が根絶。

### (6) 本番運用の地雷を踏んで学んだこと
- `docker compose restart` は `.env` を再読込しない → 環境変数変更時は `up -d`（recreate）必須。
- OSSの二重プレフィックス（`<bucket>.<bucket>.oss-...`）によるDNS失敗をエンドポイント構築ロジックのガードで修正。
- Qwen課金はモデル別の無料枠 → 枯渇したqwen-plusから別モデルへ切替。

### (7) Claudeネイティブ対応：MCPを同一アプリに同居させつつSSEを壊さない
「GPTはActions、ClaudeはMCP」を実現するため、公式MCP SDKのStreamable HTTPサーバーを**同一FastAPIプロセスの `/mcp` に同居**させた。ここでの技術的ポイント：
- **既存の16 Actionを再実装せず**、MCPツールから既存RESTハンドラを in-process 呼び出し（HTTP自打ちなし）。
- Starletteの `BaseHTTPMiddleware` は**SSE/ストリーミングを壊す**既知問題があるため、`/mcp` の認証を通常のミドルウェアではなく **pure-ASGIミドルウェア**（`__call__(scope, receive, send)`）で実装。ルート段の pure-ASGIディスパッチャで `/mcp` をFastAPIのミドルウェアスタックから完全に分離。
- `stateless_http=True` + `json_response=True` で tools/call を単発JSON応答にし、長時間SSE保持を回避。認証は **Bearer / `X-Safe-Memory-Key` 両対応**（Claudeコネクタはトークン欄→Bearer）。
- `/mcp` はGPTのOpenAPIスキーマに**出さない**（回帰テストで固定）ため、GPT側の挙動・operationIdは一切変化しない。

## 7. デモ手順（Demo）

### A. Web UI（キー不要・最も確実）
1. ブラウザで **https://smp.sdesigner.tokyo/upload** を開く。
2. CSV/Excelの入ったフォルダ（または複数ファイル）をドラッグ&ドロップ。
3. 数秒でパック化完了 → 表示される **共有リンク（署名なし安定URL）** をコピー。
4. そのURLをGPT/Claudeの `importPackByRef` に渡せば、知識として再利用可能。

### B. ChatGPT Custom GPT
1. GPTビルダー → Actions → スキーマを `https://smp.sdesigner.tokyo/openapi.json` からインポート。
2. 認証（⚙）で API Key / Custom / ヘッダ名 `X-Safe-Memory-Key` を設定。
3. チャットで「このデータをパック化して」→ `buildPackFromUrl` / `createUploadLink` が動作。

### B'. Claude（ネイティブMCPコネクタ）
1. Claude（claude.ai / Desktop）→ **Settings → Connectors → Add custom connector**。
2. **URL**: `https://smp.sdesigner.tokyo/mcp`
3. **認証**: Bearer トークン欄に `SAFE_MEMORY_API_KEY` の実値（`X-Safe-Memory-Key` ヘッダでも可）。
4. 接続すると13個のMCPツールが出現。チャットで「このパックを検索して」「このURLをパック化して」等が自然言語で動く。
   - 実装は Streamable HTTP（`stateless_http` + `json_response`）＋ pure-ASGI認証で **SSEを壊さない**設計。GPTのOpenAPIスキーマには `/mcp` を露出させない（Claude専用面）。

### C. ワンタイムアップロードリンク
1. `createUploadLink` を呼ぶ → `https://smp.sdesigner.tokyo/u/{token}`（キー欄なし）が返る。
2. そのページにファイル投入 → パック化 → 結果URL取得。単回使用・TTL付き。

### 実測デモ結果（本番）
- 会計/FXデータ 272行 → **2.1秒でパック化完了**、entry_count=272、全INTERNAL、LLM chat呼び出し0。
- 生成された `.smp.json`（約1.7MB）に、本文（原本テキスト）＋128次元embedding＋分類＋provenance＋台帳を完全内包。
- 署名なしトークンURLで no-auth HTTP 200 ダウンロード成功。TTL失効後は307でOSS署名URLへ自動fallback。
- **Claude MCP（本番 `/mcp`）**: `initialize` → `tools/list`（13ツール列挙）→ `tools/call`（`health` / `get_agent_catalog` が `isError:false`）を実測成功。ヘッダ認証・Bearer認証の両方で200、無認証は401。

## 8. プロジェクトの歩み（Milestones）

| # | マイルストーン |
|---|---|
| 1 | Alibaba Cloud ECSへデプロイ（認証・prod Docker・アップロードAPI・リテンション管理） |
| 2 | 独自ドメイン＋Caddy自動HTTPS化、GPT-safeなアップロードフロー確立 |
| 3 | OSSハンドオフ統合（URL＝通貨の実装、`importPackByRef`） |
| 4 | Webフォルダ・ドラッグ&ドロップでのパック統合取り込み |
| 5 | ワンタイムアップロードリンク、GPT認証問題の解決、パック生成の大幅高速化 |
| 6 | ダウンロードURL破損バグの根本修正（署名なし安定URL＋OSS 307 fallback） |
| 7 | ネイティブMCPサーバー（`/mcp`）追加 — Claudeからツールとして直接利用可能に |

## 9. セキュリティ / プライバシー設計

- **APIキー認証**（`X-Safe-Memory-Key`）。未設定時はdev modeで警告ログ、キーは決してログ出力しない。
- **サンドボックス**: 全ファイルIOは `SAFE_MEMORY_ROOT` 配下に限定。
- **ポリシー強制**: SECRETは外部LLM送信禁止、CONFIDENTIAL/SECRETはエクスポート既定除外、リダクション対応。
- **改ざん検知**: 追記専用のsha256ハッシュチェーン台帳。
- **生データ最小化**: `process_and_return` で処理後に生アップロードを削除。
- **スコープ限定トークン**: ワンタイムリンクはアップロード＋自ジョブ参照のみ、他操作は自動401。

## 10. 現状の制約と今後（Limitations & Roadmap）

**制約**
- 匿名のSharePoint/OneDrive **フォルダ**直読みは不可（MS仕様）→ Web/ワンタイムリンクで代替。
- 大容量ファイルはLLM Action経由だと約45秒タイムアウトの影響を受けうる（Web/ステージドアップロードで回避）。
- embeddingは一方向変換で単体からは原文復元不可（ただし本文原本がパックに含まれるため復元は不要）。

**今後**
- 実GPT/Claudeからのフルエンドツーエンド運用テスト（Claudeは本番MCPコネクタで接続確認済み）。
- 大規模プロジェクトフォルダ（数千行）でのスケール検証。
- パック間の系譜（どのパックがどのパックから派生したか）の可視化。
- ローカルMCP（Claude Desktop / stdio）でのローカルファイル直読み連携（現状はリモートMCP over HTTPを提供済み）。

## 11. まとめ（Why it matters）

Safe Memory Platform は、**「AIの知識を、安全に・検証可能に・持ち運べる1ファイルにして、URLで流通させる」** という新しいプリミティブを提供する。プロジェクトが終わってもナレッジが `.smp.json` として残り、次のエージェントがそのまま呼び出せる。機密は機密のまま守られ、改ざんは検知できる。そして同じ知識基盤を **GPT（Actions）・Claude（ネイティブMCP）・Web** のどこからでも同一機能で使える。これは、断片的なチャット履歴に閉じ込められていたAIの記憶を、**再利用可能な資産（IQファイル）** に変える試みである。

そして重要なのは、これが**スライドの中の構想ではなく、独自ドメイン上で実際に動き、本番で発生した実バグを根本原因まで潰し切り、GPTとClaudeの両方から実接続できる、稼働中のシステム**であるということだ。

---

*本番: https://smp.sdesigner.tokyo ・ GPTスキーマ: /openapi.json ・ Claude MCP: /mcp ・ ヘルスチェック: /health（auth/oss/qwen すべて有効）*
