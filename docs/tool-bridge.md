# 会話とHermesのツール実行をつないだ事例

当初、companionのAPIがtool_callsを返さず、会話中にHermesの検索などを使えませんでした。長い作業を別エージェントへ委託する経路はあっても、同じ会話の文脈で一手ずつツールを使う経路が不足していました。

会話処理を一時保留して外部ツールの呼び出しだけをHermesへ返し、結果が戻ったら呼び出しIDを照合して保留を解く構成を導入しました。

~~~mermaid
sequenceDiagram
  participant H as Hermes
  participant C as companion
  participant T as Hermesのツール
  H->>C: 会話とツール定義
  C->>C: 一時保留
  C-->>H: tool_calls
  H->>T: 実行
  T-->>H: 結果
  H->>C: role:toolの結果
  C->>C: ID照合と再開
  C-->>H: 次の操作または最終回答
~~~

人格・記憶・会話の管理をcompanionに残し、ツール実行をHermesへ渡します。過程と結果をCoreの記録へ戻します。

## 実装と検証

- [bridge.py](../companion/bridge.py): ツール定義の変換と保留処理。
- [server.py](../companion/server.py): 呼び出しの返却と継続リクエストの照合。
- [test_bridge.py](../tests/test_bridge.py): 保留、中断、内部・外部ツールの混在。
- [test_server_api.py](../tests/test_server_api.py): 応答形式と複数ラウンド。

開発元の2026-10-07の実装記録にはHermesのweb_search成功と操作記録の保存が残っています。別の検証では、稼働中のcompanionへHermes形式の模擬リクエストを送り、検索結果に依存したブラウザ操作と最終回答まで確認しています。これらは当時の記録で、公開環境で再実行した結果ではありません。

特定のAIモデルやClaude Codeの担当と断定する記録は、今回の公開準備では確認できていません。
