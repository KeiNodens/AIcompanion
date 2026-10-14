# 依存ライブラリ・外部ツール・モデルの条件

このリポジトリのMIT Licenseは自作部分に適用します。第三者のライブラリ、ツール、モデルをMITへ変更するものではありません。依存のソース・バイナリ・モデル重みは同梱していません。

## 直接依存

以下は開発環境にインストールされた配布物のMETADATAで確認した版と表記です。推移的な依存や各配布物のNOTICE・追加条件は、導入したパッケージの文書とライセンスを参照してください。これは依存の脆弱性監査の結果ではありません。

| 配布物 | 版 | ライセンス表記 | 参照 |
| --- | --- | --- | --- |
| anyio | 4.15.1 | MIT | [公式配布物・ソース](https://github.com/agronholm/anyio) |
| discord.py | 2.7.1 | The MIT License (MIT) | [公式配布物・ソース](https://pypi.org/project/discord.py/) |
| fastapi | 0.142.1 | MIT | [公式配布物・ソース](https://github.com/fastapi/fastapi) |
| fastembed | 0.8.1 | Apache-2.0 | [公式配布物・ソース](https://github.com/qdrant/fastembed) |
| httpx | 0.28.1 | BSD-3-Clause | [公式配布物・ソース](https://github.com/encode/httpx) |
| mcp | 1.30.0 | MIT | [公式配布物・ソース](https://github.com/modelcontextprotocol/python-sdk) |
| mem0ai | 2.2.1 | Apache-2.0 | [公式配布物・ソース](https://pypi.org/project/mem0ai/) |
| openai | 2.54.0 | Apache-2.0 | [公式配布物・ソース](https://github.com/openai/openai-python) |
| pageindex | 0.2.20 | MIT | [公式配布物・ソース](https://github.com/VectifyAI/PageIndex) |
| pillow | 12.3.0 | MIT-CMU | [公式配布物・ソース](https://python-pillow.github.io) |
| psutil | 7.2.2 | BSD-3-Clause | [公式配布物・ソース](https://github.com/giampaolo/psutil) |
| pycaw | 20260927 | MIT | [公式配布物・ソース](https://github.com/AndreMiras/pycaw.git) |
| pypdfium2 | 5.13.0 | BSD-3-Clause, Apache-2.0, dependency licenses | [公式配布物・ソース](https://github.com/pypdfium2-team/pypdfium2) |
| python-dotenv | 1.2.3 | BSD-3-Clause | [公式配布物・ソース](https://github.com/theskumar/python-dotenv) |
| pywin32 | 312 | PSF | [公式配布物・ソース](https://github.com/mhammond/pywin32) |
| pywinauto | 0.6.9 | BSD 3-clause | [公式配布物・ソース](https://github.com/pywinauto/pywinauto) |
| PyYAML | 6.0.3 | MIT | [公式配布物・ソース](https://github.com/yaml/pyyaml) |
| qdrant-client | 1.19.1 | Apache-2.0 | [公式配布物・ソース](https://github.com/qdrant/qdrant-client) |
| requests | 2.32.5 | Apache-2.0 | [公式配布物・ソース](https://github.com/psf/requests) |
| sqlite-vec | 0.1.9 | MIT License, Apache License, Version 2.0 | [公式配布物・ソース](https://pypi.org/project/sqlite-vec/) |
| uvicorn | 0.54.0 | BSD-3-Clause | [公式配布物・ソース](https://github.com/Kludex/uvicorn) |
| winrt-runtime | 3.2.1 | MIT | [公式配布物・ソース](https://github.com/pywinrt/pywinrt) |
| winrt-Windows.Foundation | 3.2.1 | MIT | [公式配布物・ソース](https://github.com/pywinrt/pywinrt) |
| winrt-Windows.Foundation.Collections | 3.2.1 | MIT | [公式配布物・ソース](https://github.com/pywinrt/pywinrt) |
| winrt-Windows.Media.Control | 3.2.1 | MIT | [公式配布物・ソース](https://github.com/pywinrt/pywinrt) |

## モデル

- 設定例の[多言語MiniLM](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2)はモデルカードの条件を確認して取得します。コード側では384次元を設定します。
- 任意の[Jina Reranker v2](https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual)はCC-BY-NC-4.0のモデルです。設定例では無効にしています。有効化や商用利用にはモデル提供者の条件・案内を確認してください。MITのコードであることを理由にモデルの制限がなくなるとは扱いません。
- 別の埋め込み・生成モデルへ変更した場合、そのモデルの条件を確認してください。

## 外部ツール

Hermes、Codex、ChatMock、Discord、音楽アプリは別途導入する連携先です。各ツール・サービスのライセンスや利用条件が適用されます。ChatMockは同じ仮想環境の依存に含めず、認証や利用権を配布しません。

## 調査資料

個人運用のresearch/、取得したREADME群、第三者リポジトリのチェックアウトは含めません。公開文書は実装の説明として書き直しています。コード内に残る設計文書名は、開発時の内部参照です。
