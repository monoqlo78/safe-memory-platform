# Safe Memory Platform — アーキテクチャ構成と説明

> **AIエージェントのための、持ち運べる・安全な・再利用可能な「知識ファイル」基盤**
> プライベートなデータ（会計・FX・業務ナレッジ・各種ドキュメント）を、ポリシー付きの
> ポータブルなメモリパック（`.smp.json`）に変換し、**URL経由で** Web・ChatGPT・Claude から
> 共有・再利用できるようにする。

- **本番URL**: https://smp.sdesigner.tokyo （ライブ稼働中）
- **インフラ**: Alibaba Cloud ECS / Docker / Caddy 自動HTTPS / 独自ドメイン
- **AIエンジン**: Qwen Cloud（Alibaba DashScope, OpenAI互換）— embedding / classification / reasoning

---

## 1. システム全体アーキテクチャ

```mermaid
graph TB
    subgraph clients["① エージェント・クライアント層（利用面）"]
        GPT["ChatGPT<br/>Custom GPT Actions<br/>(OpenAPI)"]
        CLAUDE["Claude<br/>ネイティブMCP<br/>(Streamable HTTP)"]
        WEB["Web UI<br/>/upload<br/>(ドラッグ&ドロップ)"]
        LINK["ワンタイム<br/>アップロードリンク<br/>/u/{token}"]
    end

    subgraph edge["② エッジ層（Alibaba Cloud ECS）"]
        CADDY["Caddy リバースプロキシ<br/>Let's Encrypt 自動TLS<br/>smp.sdesigner.tokyo → :8787"]
        DISPATCH["pure-ASGI ルートディスパッチ"]
    end

    subgraph app["③ アプリケーション層（FastAPI / Docker）"]
        MCP["MCPサーバー /mcp<br/>13ツール<br/>pure-ASGI認証(SSE安全)"]
        REST["REST API /api/*<br/>16 Action<br/>APIキー middleware"]
        subgraph core["コアエンジン（3サービス）"]
            FORGE["Memory Forge（鍛造）<br/>chunk → embed → classify → pack化"]
            LENS["Memory Lens（検証）<br/>query / verify / audit"]
            WORK["Memory Workspace（活用）<br/>runProjectWithMemory"]
        end
        POLICY["ポリシーエンジン<br/>classification / export / redaction"]
        LEDGER["ハッシュチェーン台帳<br/>sha256 改ざん検知"]
        JOBS["ジョブ / リテンション管理<br/>session / process_and_return / server_vault"]
    end

    subgraph ext["④ 外部サービス（Qwen Cloud / Alibaba OSS）"]
        QWEN["Qwen Cloud<br/>text-embedding-v4 / qwen-flash<br/>(OpenAI互換API)"]
        OSS["Alibaba OSS<br/>一時パックのホスティング"]
    end

    subgraph store["⑤ ストレージ（ローカルファースト）"]
        PACKS["Safe Memory Packs<br/>.smp.json<br/>SAFE_MEMORY_ROOT配下"]
        DL["署名なし安定URL<br/>/api/packs/dl/{token}"]
    end

    GPT -->|HTTPS| CADDY
    CLAUDE -->|HTTPS| CADDY
    WEB -->|HTTPS| CADDY
    LINK -->|HTTPS| CADDY
    CADDY --> DISPATCH
    DISPATCH -->|/mcp| MCP
    DISPATCH -->|/*| REST
    MCP -.->|in-process 再利用| REST
    REST --> FORGE
    REST --> LENS
    REST --> WORK
    FORGE --> POLICY
    FORGE --> LEDGER
    FORGE --> JOBS
    FORGE -->|embedding / classify| QWEN
    FORGE --> PACKS
    JOBS -->|一時ホスト| OSS
    PACKS --> DL
    DL -.->|TTL失効後 307| OSS
    LENS --> PACKS
    WORK --> PACKS

    classDef client fill:#e3f2fd,stroke:#1976d2,color:#0d47a1
    classDef edgecls fill:#fff3e0,stroke:#f57c00,color:#e65100
    classDef appcls fill:#e8f5e9,stroke:#388e3c,color:#1b5e20
    classDef extcls fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
    classDef storecls fill:#fce4ec,stroke:#c2185b,color:#880e4f
    class GPT,CLAUDE,WEB,LINK client
    class CADDY,DISPATCH edgecls
    class MCP,REST,FORGE,LENS,WORK,POLICY,LEDGER,JOBS appcls
    class QWEN,OSS extcls
    class PACKS,DL storecls
```

### レイヤー解説

| 層 | 役割 | ポイント |
|---|---|---|
| **① クライアント** | GPT / Claude / Web / ワンタイムリンクの4つの入口 | **同一バックエンドを3面から同一機能で利用**。GPTとClaudeで機能差なし |
| **② エッジ** | Caddyが自動HTTPS＋リバースプロキシ。pure-ASGIで`/mcp`とREST APIを分岐 | `/mcp`を通常のミドルウェアから完全分離し**SSEを壊さない** |
| **③ アプリ** | FastAPI上のコアエンジン（Forge/Lens/Workspace）＋ポリシー＋台帳＋ジョブ | MCPツールは既存RESTハンドラを**in-process再利用**（ロジック二重化なし） |
| **④ 外部** | Qwen Cloudが埋め込み・分類・推論。OSSが一時パックのホスト | **Qwen Cloudが中核AIエンジン**。キー失敗時は決定論的フォールバックで落ちない |
| **⑤ ストレージ** | パックは`.smp.json`としてローカル永続。DB・ベクトルDBなし | 署名なし安定URLで配布、TTL失効後はOSS署名URLへ307 |

---

## 2. データフロー：知識を「パック」にして流通させる

```mermaid
sequenceDiagram
    participant U as ユーザー / エージェント
    participant API as Safe Memory Platform
    participant Q as Qwen Cloud
    participant S as Storage (.smp.json)

    Note over U,S: ── ① パック生成（Memory Forge）──
    U->>API: buildPackFromUrl / upload（Word/Excel/PDF/画像…）
    API->>API: 抽出・チャンク化（表は「見出し: 値」）
    API->>Q: embedding（text-embedding-v4）
    Q-->>API: 意味ベクトル
    API->>API: classification（6段階）＋ポリシー付与
    API->>API: sha256ハッシュチェーン台帳に追記
    API->>S: .smp.json を server_vault に永続
    API-->>U: message＋download_url（署名なし安定URL）

    Note over U,S: ── ② 検索・再利用（Memory Lens / Workspace）──
    U->>API: queryMemoryPack（自然文の質問）
    API->>Q: 質問文をembedding
    API->>API: hybrid検索（意味＋キーワード, top_k=12）
    API->>API: ポリシー適用（SECRETはLLM非送信）
    API->>Q: 上位エントリで回答生成（qwen-flash）
    Q-->>API: answer
    API-->>U: answer＋根拠エントリ（機密は保護）
```

---

## 3. 「URL＝知識の通貨」— なぜこの設計か

```mermaid
graph LR
    A["LLMの制約<br/>ファイル(バイト列)を<br/>送受信できない<br/>JSON/テキスト＋45秒制限"] --> B["逆転の発想<br/>パックをサーバーにホストし<br/>URLで参照する"]
    B --> C["importPackByRef(url)<br/>任意のHTTPS URLから<br/>知識を取り込み・再利用"]
    C --> D["エージェント間で<br/>知識を受け渡し<br/>GPT ⇄ Claude ⇄ Web"]

    classDef box fill:#e8f5e9,stroke:#388e3c,color:#1b5e20
    class A,B,C,D box
```

LLM（GPT Actions / リモートMCP）は**バイト列を送れない**。この制約を逆手に取り、
**パックをサーバーにホストしURLで参照**する設計に統一した。URLは渡せるので、
**URLが知識流通の「通貨」**になる。プロジェクトが終わっても知識は`.smp.json`として残り、
次のエージェントが `importPackByRef(url)` でそのまま呼び出せる。

---

## 4. Safe Memory Pack（`.smp.json`）の構造

`.smp.json` は「ベクトルDBのクローン」ではなく、**自己完結した知識ファイル**。

| 要素 | 内容 |
|---|---|
| **entries** | 本文（原本）＋ Qwen埋め込み（意味検索用ベクトル）＋ キーワード ＋ メタデータ |
| **classification** | `PUBLIC` / `SHAREABLE` / `INTERNAL` / `CONFIDENTIAL` / `SECRET` / `EPHEMERAL` の6段階 |
| **policy flags** | 「検索に使ってよいか / LLMに送ってよいか / エクスポートしてよいか」 |
| **provenance** | 各エントリの出所（どのファイル由来か） |
| **ledger** | 追記専用のハッシュチェーン（各ブロックが前ブロックのsha256を封入 → 改ざん検知） |

**バックエンドが強制するポリシー**
- `SECRET` は **絶対に** 外部LLMに送信しない
- `CONFIDENTIAL` / `SECRET` は明示許可がない限りエクスポートから除外
- 全ファイルアクセスは `SAFE_MEMORY_ROOT` 配下に閉じ込め（サンドボックス）

---

## 5. 技術スタック

| 分類 | 採用技術 |
|---|---|
| **言語 / FW** | Python 3 / FastAPI / Uvicorn / Pydantic |
| **AIエンジン** | **Qwen Cloud**（Alibaba DashScope, OpenAI互換）— embedding: `text-embedding-v4` / chat: `qwen-flash` |
| **エージェント連携** | ChatGPT Custom GPT Actions（OpenAPI）/ **Claude ネイティブMCP**（Streamable HTTP, `mcp` SDK）/ Web UI |
| **ドキュメント取込** | docx / pptx / pdf（テキスト）/ 画像・スキャンPDF（Tesseract OCR: jpn+eng）/ xlsx / xls / csv |
| **インフラ** | Docker Compose / Alibaba Cloud ECS / Caddy（自動HTTPS）/ 独自ドメイン `sdesigner.tokyo` |
| **ストレージ** | ローカルファースト（DB／ベクトルDBなし）。パックは`.smp.json`。一時ホスティングにAlibaba OSS |
| **セキュリティ** | APIキー認証 / ポリシーエンジン / sha256ハッシュチェーン / サンドボックス / スコープ限定トークン |

---

## 6. ハッカソンでの位置づけ（Qwen Cloud が中核）

```mermaid
graph TB
    subgraph engine["AI処理エンジン（中核）"]
        Q["Qwen Cloud<br/>embedding / classification / reasoning"]
        ECS["Alibaba Cloud ECS<br/>本番バックエンド"]
    end
    subgraph ui["エージェント・インターフェース（入口）"]
        G["GPT Actions<br/>OpenAPI経由"]
        C["Claude MCP<br/>MCPネイティブ"]
        W["Web UI<br/>非エンジニア向け"]
    end
    G --> Q
    C --> Q
    W --> Q
    Q --- ECS

    classDef eng fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
    classDef uicls fill:#e3f2fd,stroke:#1976d2,color:#0d47a1
    class Q,ECS eng
    class G,C,W uicls
```

> **GPT and Claude are used as agent clients, not as the core AI runtime.**
> The core memory processing pipeline runs on **Alibaba Cloud** and uses **Qwen Cloud**
> for embeddings, classification, reasoning, and safe memory decisions. GPT connects
> through OpenAPI Custom Actions, and Claude connects through the native Remote MCP
> endpoint. This demonstrates that Safe Memory Packs are portable memory assets reusable
> across multiple agent interfaces while still relying on Qwen Cloud as the AI engine.
>
> **操作画面はGPTやClaudeですが、メモリー処理の中核は Alibaba Cloud 上のバックエンドと Qwen Cloud です。**

---

*本番: https://smp.sdesigner.tokyo ・ GPTスキーマ: /openapi.json ・ Claude MCP: /mcp ・ ヘルス: /health*
