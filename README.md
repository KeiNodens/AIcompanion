# companion

**記憶を持ち、会話から作業へつなぐ日本語AIコンパニオン。**

[![CI](https://github.com/KeiNodens/AIcompanoion/actions/workflows/ci.yml/badge.svg)](https://github.com/KeiNodens/AIcompanoion/actions/workflows/ci.yml)

Discordやブラウザで話しかけ、過去の文脈を使った相談、リマインダー、外部エージェントへの作業依頼と成果の受け取りを扱う、個人開発のプロジェクトです。

会話の人格と記憶を担うCoreと、作業を実行するHermes・Codexとの接続を分けています。任せた作業の結果を、同じ文脈へ戻せる構成を目指しています。

> 個人運用のコードから、本番データと個人設定を除いて切り出したソースです。Windows向けの連携を含みます。第三者の環境での導入と実機操作は、接続先を設定して確認する必要があります。

## できること

| 用途 | 実装 |
| --- | --- |
| 前に話したことを使って相談する | 長期記憶、直近会話の圧縮、ユーザープロフィール |
| 予定や用事を管理する | タスク、リスト、リマインダー、定期通知 |
| 調べながら会話を進める | Hermesのツール実行と会話を接続する橋 |
| 長い作業を任せる | 非同期の作業依頼、進捗・承認・完了の管理 |
| 作業の文脈と結果を引き継ぐ | MCPの文脈・技能・成果報告ツール |
| 記憶を検証する | 記憶の受理検査、応答への供給記録、再構築・検証処理 |

## なぜ作ったか

毎回同じ前提を説明する手間を減らし、相談したことと実際の作業を一つの会話で扱える相棒を作りたいと考えています。開発者本人のローカル運用を起点に、会話と記憶、承認と実行結果をつなぐ仕組みを開発しています。

公開利用者数や時間短縮の数値は、測定していないため掲載していません。開発に使ったAIの担当範囲も、確認できる記録に基づいて説明します。

## 開発で解決した問題

Hermesを接続した当初、companionのAPIがツールの呼び出し指示を返さず、会話中にHermesの検索機能を使えない問題がありました。

そこで、companionの会話を一時保留し、Hermesへツール実行を渡し、結果を受け取って同じ会話を再開する仕組みを導入しました。人格・記憶・会話の管理をCoreに残しながら、外部ツールを使う経路を整えています。[実装と検証の入口](docs/tool-bridge.md)を参照してください。

## 構成

~~~mermaid
flowchart LR
  D["Discord"] --> H["Hermes gateway"]
  H --> A["会話API"]
  U["ブラウザ /ui"] --> A
  A --> C["会話・人格・ツール"]
  C <--> M["ローカルの記憶"]
  C <--> B["作業・承認・結果管理"]
  B <--> W["Hermes / Codex"]
  W --> P["MCP: 文脈・技能・成果報告"]
  P --> C
~~~

決定的な記憶処理はcompanion/kernel/、I/O・LLM・検索・スケジューリングはcompanion/adapters/に分けています。canonicalモードではdata/core.dbが記憶の正本です。旧mem0経路と移行処理も含まれます。

**新しい環境には開発者の記憶DBや状態ファイルを配布しません。** 公開用コードと設定例はcanonicalを既定にしています。mem0とdual_runは旧記憶の移行・復帰用の互換経路として残しています。既存記憶の移行は初期導入とは別に扱ってください。

## 導入

WindowsとPython 3.12以降を前提とします。開発元のPythonは3.14で、CIは3.12・3.14を対象にしています。会話にはローカルのOpenAI互換エンドポイントが必要です。現行クライアントは認証値としてunusedを送る実装なので、有料クラウドAPIへ直接つなぐ設定例は提供していません。

~~~powershell
git clone https://github.com/KeiNodens/AIcompanoion.git
cd AIcompanoion
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
~~~

.envのLLM_BASE_URLとLLM_MODELを、実際のローカルエンドポイントに合わせます。COMPANION_API_KEYには自分で作った鍵を設定します。鍵はコミットしません。人格と会話プロンプトには公開用サンプルを同梱しています。

~~~powershell
.venv\Scripts\python bot.py
~~~

http://127.0.0.1:8788/ui を開き、設定したAPI鍵を入力します。終了はターミナルでCtrl+Cを押します。個人環境の常駐・停止スクリプトは含めていません。

Discord・Hermes・Codex連携は、別途インストール・認証・設定と実機確認が必要です。[接続ガイド](docs/setup.md)を参照してください。

## 検証

本番データや外部サービスを切り離したテストがあります。CIでは構文、kernel manifest、記憶・承認・API・MCPなどを検証します。Discord、実モデル、Windowsの実画面の操作まで通ることを証明するものではありません。

~~~powershell
.venv\Scripts\python -m pip install -r requirements-test.txt
.venv\Scripts\python scripts/kernel_manifest.py check
.venv\Scripts\python -m unittest discover -s tests -p "test_*.py"
~~~

最新結果は[GitHub Actions](https://github.com/KeiNodens/AIcompanoion/actions)を参照してください。公開用コピーの準備時には、作業環境のPython起動が制限され、テストをローカルで再実行できていません。

## コードを読む

| 関心 | 入口 |
| --- | --- |
| 会話とツール | [engine.py](companion/engine.py)、[server.py](companion/server.py) |
| ツール実行の橋渡し | [bridge.py](companion/bridge.py) |
| 作業と承認 | [bodyruns.py](companion/bodyruns.py)、[runtime/body.py](companion/runtime/body.py) |
| 記憶の供給 | [adapters/supply.py](companion/adapters/supply.py) |
| 決定的な記憶処理 | [kernel/](companion/kernel/) |
| MCP | [mcp_server.py](companion/mcp_server.py)、[mcp_stdio.py](companion/mcp_stdio.py) |

## 公開範囲・ライセンス

本体、テスト、運用スクリプト、連携プラグイン、汎用の設定例と文書を含みます。会話・記憶・ログ・個人プロフィール・鍵・開発履歴・人格原文・調査用資料は含みません。[公開方針](docs/public-scope.md)を参照してください。

自作部分のMITライセンス案は公開前の確認待ちです。現在は非公開の検証用コピーです。依存ライブラリ、外部ツール、追加取得するモデルにはそれぞれの条件が適用されます。[依存とモデルの条件](THIRD_PARTY_NOTICES.md)を確認してください。モデル重みと第三者のソースは同梱していません。

脆弱性の報告は[SECURITY.md](SECURITY.md)、変更履歴は[CHANGELOG.md](CHANGELOG.md)を参照してください。
