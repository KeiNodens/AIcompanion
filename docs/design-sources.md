# 設計参照元と実装の由来

companionの記憶機構は、複数の公開実装・仕様を調べて、companion自身の仕様から実装する方針で開発されています。開発元のライセンス台帳では、最終構成に原コードの移植を予定した行はなく、AGPL・GPL・非商用条件・ライセンス不明の参照元には独自実装のみの制約を記録しています。

この公開用コピーは、参照元のソース、SQL、設定、スキーマ、fixture、モデル重み、取得した文書を同梱していません。公開ファイルの列挙とハッシュはPUBLIC-SNAPSHOT.jsonで確認できます。

## 帰属と参考資料

| 参照元 | 設計で参考にした点 | 条件・出典 |
| --- | --- | --- |
| Open Engram Standard / plur-ai | 記憶パックの整合性・成熟度・削除記録などの語彙 | [仕様](https://github.com/plur-ai/engram-spec/blob/6e5916bfa8ca0c2c435dbb75ba740668ba2bb76d/ENGRAM-STANDARD-v1.md)、仕様本文は[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/)。概念・語彙をcompanionのpcore.pack.v1へ対応づけており、仕様本文を転載せず、元のJSON Schemaも同梱しません |
| dreamd / Botzr | 記録と投影、履歴・差分の扱い | [参照コミット](https://github.com/botzrDev/dreamd/tree/2be67916b876e04645fc4f2d31b7f5e7a4eb2a0b)。設計時の調査はApache-2.0とNOTICEを記録 |
| myco-brain | 証拠の独立性と来歴による評価 | [参照コミット](https://github.com/thegoodguysla/myco-brain/tree/d55c4f2f4c6fe3213adf5345d624c20c4c3d4866)。設計時の調査はApache-2.0とNOTICEを記録 |
| experience-hub | 決定的な処理、codec、索引と検証 | [参照コミット](https://github.com/xwj123456/experience-hub/tree/ffe726276fbf4eb6f073d1ac934756f7b4a49888)。設計時の調査はApache-2.0と帰属表示を記録 |
| MarsNMe | 起動時検査と成果の引き渡し | [参照コミット](https://github.com/Marsmanleo/MarsNMe/tree/cf6721229cd3affd4a207936637f3f4f8d882274)。設計時の調査はApache-2.0、NOTICE、商標の別条件を記録 |
| Hindsight | 時間・対象名・再採点などの検索手法 | [プロジェクト](https://github.com/vectorize-io/hindsight)。companionのcanonical記憶の内側に独自実装 |
| Mem++ | 非破壊的な記憶の扱い、最近の記憶の供給枠 | [プロジェクト](https://github.com/AIDAChip-Inc/mem-plus-plus)。companionの供給処理で独自実装 |

原コードや語彙表を実際に取り込む変更を今後行う場合、この表の記載だけで配布条件を満たしたとは扱わず、対象コミットのライセンス・著作権表示・NOTICE・文書の条件を再確認します。

## 制限のある参照元

開発時の調査にはvestige、memento-4242、OpenViking、basic-memory、Honcho、aukora-kernel、kage、MuninnDB、mcp-memory-graph、fireweed-mcp、somnigraphや、ライセンス状態が未確認だった候補も含まれます。これらは機構・設計の参照に留め、原コード・SQL・設定・fixtureの移植や同梱を行わない制約です。

MuninnDBについては、著作権のライセンスとは別に、仮特許出願の表示が開発時の記録にあります。companionは同プロジェクトの実装形を移植せず、減衰・想起閾値を一般的な規則として設計する方針です。これは特許の非侵害を保証する記録ではありません。

## この確認の限界

独自実装の方針、実装記録、公開対象、コード内の由来表示を照合しました。すべての参照元コードとの翻案・類似性を法的に鑑定したものではありません。MITはcompanionの自作部分に適用するライセンスで、参照元のコードやモデルの条件を変更するものではありません。
