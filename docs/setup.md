# 接続と設定

[README](../README.md)の手順でローカルAPIとブラウザ画面を使える状態にします。

## 会話用LLM

現行クライアントはOpenAI互換APIへ接続しますが、認証値はunusedを送る実装です。ローカルのエンドポイントを用意し、LLM_BASE_URLとLLM_MODELを設定してください。モデルの接続先と会話データの扱いも確認します。

開発元ではChatMockを別プロセスで使っていました。公開版のrequirements.txtには含めていません。ChatMockのidna固定値とPageIndex経由の依存条件に競合があるため、同じ環境へ無条件にまとめて導入する手順は提供しません。使う場合は別の仮想環境を用意し、同ツールの公式手順と条件を確認してください。

## 記憶

.env.exampleはMEMORY_AUTHORITY=canonicalを指定し、新しいデータでcanonical記憶を使う構成の例です。開発者のdata/は含みません。既存mem0データの移行は別の作業として、scripts/migrate.pyの対象と手順、バックアップを確認します。

埋め込みモデルは初回使用時に取得されます。設定例は384次元の多言語MiniLMです。モデルを変えるときはEMBED_DIMSを合わせ、既存索引への影響を確認します。追加のリランキングは無効です。有効にする場合は[モデル固有の条件](../THIRD_PARTY_NOTICES.md)を確認します。

## HermesとDiscord

Discordの受信はHermes gateway、通知送信はcompanionのDiscord RESTが担当します。退役したcompanion.discord_botを同じbotトークンで同時起動しないでください。

Hermesを別途設定し、会話モデルの接続先をcompanionの/v1へ向けます。入口鍵とセッション識別ヘッダx-companion-sessionを渡します。会話中のツール連携には、toolsを送り、tool_callsを実行してrole:toolの結果を返す経路が必要です。

長い作業にはHermesのapi_serverを使い、HERMES_API_BASEとHERMES_API_KEYを合わせます。裏方モデルは実行を担当するモデルへ指定します。

~~~powershell
.venv\Scripts\python -m companion.hermes_check
.venv\Scripts\python -m companion.profile_check
~~~

COMPANION_API_KEYとCOMPANION_MCP_KEYは別の入口用です。MCPの専用鍵が空の場合、その面は利用できません。

## CodexとMCP

Codex backendは実験的なapp-serverのプロトコルを使います。Codexを導入・認証してから疎通を確認します。

~~~powershell
.venv\Scripts\python -m companion.codex_check
~~~

codex-marketplace/companion-body/.mcp.json.exampleを.mcp.jsonへコピーし、<COMPANION_ROOT>を自分の絶対パスへ置換します。実行ファイル、作業ディレクトリ、PYTHONPATHを合わせてからプラグインを利用してください。個人用.mcp.jsonはGitの対象外です。

## APIの境界

会話APIは127.0.0.1限定です。設定例は入口鍵を強制します。ヘッダのないローカル呼び出しはdesktopとして扱われ、インターネット公開を想定していません。ブラウザ画面はAPI鍵をlocalStorageに保持します。

PC操作、音楽アプリ、Obsidian、PDF索引は別途設定と実機確認が必要です。初期導入では会話APIから一つずつ確認します。
