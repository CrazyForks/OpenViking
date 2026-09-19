<div align="center">

<a href="https://openviking.ai/"><img src="docs/images/ov-logo.png" alt="OpenViking" width="140"></a>

# OpenViking

**AI エージェントのコンテキストデータベース**

知識・記憶・スキルを、次のセッションにも、別のエージェントにも。

[クイックスタート](#クイックスタート) · [ドキュメント](https://docs.openviking.ai/en/getting-started/01-introduction) · [デモ](https://openviking.ai/studio) · [公式サイト](https://openviking.ai/) · [リリース](https://github.com/volcengine/OpenViking/releases)

<p>
  <a href="https://discord.com/invite/eHvx8E9XF3"><img src="docs/images/community/discord.svg" width="18" height="18" alt="">&nbsp;Discord</a> &nbsp;·&nbsp;
  <a href="https://docs.openviking.ai/en/about/01-about-us#lark-group"><img src="docs/images/community/lark.svg" width="18" height="18" alt="">&nbsp;Lark</a> &nbsp;·&nbsp;
  <a href="https://docs.openviking.ai/en/about/01-about-us#wechat-group"><img src="docs/images/community/wechat.svg" width="18" height="18" alt="">&nbsp;WeChat</a> &nbsp;·&nbsp;
  <a href="https://x.com/openvikingai"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/images/community/x-dark.svg"><img src="docs/images/community/x.svg" width="16" height="16" alt=""></picture>&nbsp;X</a>
</p>

[English](README.md) / [中文](README_CN.md) / 日本語

</div>

## エージェントを変えても、コンテキストは引き継ぐ

プロジェクトの資料、ユーザーの好み、これまでの作業。一つのエージェントや会話だけに閉じ込める必要はありません。OpenViking はコンテキストウィンドウの外に保存し、次のセッションや別のエージェントから使えるようにします。

OpenViking は、**知識・記憶・スキルをファイルとディレクトリで管理する**オープンソースのデータベースです。同じサービスとユーザー領域に接続すれば、MCP、プラグイン、CLI、SDK を通じてコンテキストを再利用できます。

<a href="https://openviking.ai/studio">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/studio-playground-dark.png">
    <img src="docs/images/studio-playground.png" alt="OpenViking Studio：ディレクトリの閲覧、内容の確認、意味検索">
  </picture>
</a>

[Studio でコンテキストを閲覧・検索](https://openviking.ai/studio)。インストールは不要です。

## ファイルとして整理し、必要な内容だけ読む

- **知識・記憶・スキルを一か所に。** 文書、コード、Web ページを取り込み、ユーザーの記憶や再利用可能なスキルと一緒に保存。`viking://` URI で参照できます。
- **プロジェクトの範囲に絞って検索。** ディレクトリを指定して意味検索し、`ls`、`tree`、`read`、`write`、`grep` で内容を確認・編集。検索結果を受け取るだけでなく、エージェント自身がコンテキストを整理できます。
- **原文の前に、要約を読む。** 自動生成されるディレクトリの要約（L0）と概要（L1）で、読むべき内容を判断。全文（L2）は必要なときに読み込み、すべてをプロンプトに詰め込む必要はありません。
- **会話ログから、次に使える記憶へ。** セッションのコミットでユーザーの記憶を抽出・更新。Agent Evolution を明示的に有効にすると、タスク事例から再利用可能な経験も蓄積できます。

```text
viking://
├── resources/                 # 文書、コード、Web ページ
│   └── project/
│       ├── .abstract.md       # L0：探している内容はあるか？
│       ├── .overview.md       # L1：何が入っているか？
│       └── ...                # L2：必要な内容を読む
└── user/
    └── {user_id}/
        ├── memories/          # 好み、出来事、経験
        └── skills/            # 再利用可能なタスク指示
```

資料をナレッジベースにまとめるには、[`ov compile`](https://docs.openviking.ai/en/context-compilation/01-overview)。指定したスキルに従い、ソースディレクトリから Wiki、知識グラフ、レポートを生成します。VikingBot が必要です。

[アーキテクチャ](https://docs.openviking.ai/en/concepts/01-architecture) · [ディレクトリ検索](https://docs.openviking.ai/en/concepts/07-retrieval) · [記憶の抽出](https://docs.openviking.ai/en/concepts/06-extraction) · [Agent Evolution の設定](https://docs.openviking.ai/en/guides/01-configuration#server-section)

## クイックスタート

**ローカルサーバーを起動。** Python 3.10+ と、埋め込みモデル・視覚言語モデル（VLM）へのアクセスが必要です。[uv](https://docs.astral.sh/uv/getting-started/installation/) で環境を分離してインストールします。

```bash
uv tool install openviking --upgrade
openviking-server init      # プロバイダーとモデルを選択
openviking-server doctor    # 設定と接続を確認
openviking-server
```

**取り込んで検索。** 別のターミナルで npm から CLI をインストールし、`ov config` を実行。**Custom** を選び、`http://127.0.0.1:1933` を設定します。デフォルトのローカルサーバーでは API キーを空欄にします。

```bash
npm install -g @openviking/cli
ov config
ov health

printf '# Project Atlas\nMaya owns the weekly backup. Keep backups for 30 days.\n' > quickstart.md
ov add-resource ./quickstart.md --to viking://resources/quickstart-demo --wait --timeout 120
ov overview viking://resources/quickstart-demo
ov find "Who owns the backup process?" --uri viking://resources/quickstart-demo
```

`find` は関連するコンテキストを返し、回答文は生成しません。`ov read "<返されたファイル URI>"` で内容を確認できます。再度取り込むときは、未使用の保存先ディレクトリを指定してください。

既存のサービスがある場合は、サーバーのインストールを省略し、URL とユーザー API キーで CLI を接続します。

[詳しい手順](https://docs.openviking.ai/en/getting-started/02-quickstart) · [モデル設定](https://docs.openviking.ai/en/guides/01-configuration) · [Docker](https://docs.openviking.ai/en/guides/03-deployment) · [Railway にデプロイ](https://railway.com/deploy/openviking)

## エージェントに接続する

ネイティブ連携では記憶の自動取得と会話の収集を、MCP ではエージェントから呼び出せるコンテキスト操作ツールを利用できます。ライフサイクルへの対応範囲は連携ごとに異なります。

| 連携方式 | エージェント |
| --- | --- |
| Hooks + MCP | [Claude Code](https://docs.openviking.ai/en/agent-integrations/02-claude-code) · [Codex](https://docs.openviking.ai/en/agent-integrations/04-codex) · [Cursor](https://docs.openviking.ai/en/agent-integrations/12-cursor) · [TRAE](https://docs.openviking.ai/en/agent-integrations/13-trae) |
| Plugin + MCP | [OpenCode](https://docs.openviking.ai/en/agent-integrations/10-opencode) · [DeerFlow](docs/images/agents/en/deerflow-memory-manager.md) · [DSH](https://docs.openviking.ai/en/agent-integrations/17-dsh) |
| Context engine | [OpenClaw](https://docs.openviking.ai/en/agent-integrations/03-openclaw) |
| 組み込み Provider | [Hermes](https://docs.openviking.ai/en/agent-integrations/05-hermes) |
| ネイティブ拡張 | [pi](https://docs.openviking.ai/en/agent-integrations/11-pi) |
| Connector | [Doubao Work](docs/images/agents/en/doubao-work.md) |
| Tools + store | [LangChain / LangGraph](https://docs.openviking.ai/en/agent-integrations/07-langchain-langgraph) |

[MCP クライアント](https://docs.openviking.ai/en/agent-integrations/06-mcp-clients) · [Agent Plugins 1.0](https://docs.openviking.ai/en/agent-integrations/15-agent-plugins) · [連携機能の比較](https://docs.openviking.ai/en/agent-integrations/16-capability-reference)

**GUI で設定するなら：** [OpenViking Helper](https://docs.openviking.ai/en/agent-integrations/14-openviking-helper) でローカルの連携を設定し、セッション・記憶・スキルを確認できます。macOS / Windows 対応（Beta）。

**アプリを開発するなら：** [Python](sdk/python/README.md)、[Go](sdk/go/README.md)、[TypeScript](sdk/typescript/README.md) SDK、または [HTTP API](https://docs.openviking.ai/en/api/01-overview) を利用できます。[VikingBot](https://docs.openviking.ai/en/guides/17-vikingbot) は同梱のエージェントフレームワークです。

## ベンチマーク

チームが 2026 年 5 月に公開した評価では、記憶の活用により長期会話の QA 精度と複数ターンのタスク成功率が向上しました。

| 評価 | OpenViking なし | OpenViking あり |
| --- | ---: | ---: |
| LoCoMo、OpenClaw | 24.20% | **82.08%** |
| LoCoMo、Hermes | 33.38% | **82.86%** |
| LoCoMo、Claude Code | 57.21% | **80.32%** |
| tau2-bench、Retail | 70.94% | **77.81%** |
| tau2-bench、Airline | 54.38% | **66.25%** |

LoCoMo は各エージェントの標準メモリと OpenViking 連携を比較し、入力トークンは 34–91% 減少しました。tau2-bench は同一 LLM で経験記憶の有無を比較しています。

結果は報告された実験条件に基づき、すべてのモデルや用途で保証されるものではありません。[評価レポート](https://blog.openviking.ai/post/openviking-benchmark-results/) · [評価コード](benchmark)

## デプロイ

- **オープンソース：** AGPLv3 のサーバーを自分の環境で運用。アクティベーションキーは不要です。[デプロイガイド](https://docs.openviking.ai/en/guides/03-deployment)。
- **マネージドサービス：** [Volcano Engine OpenViking](https://www.volcengine.com/product/openviking-service) を利用し、サーバー運用を委任できます。[プランと制限](https://docs.volcengine.com/docs/84313/2374478)。
- **商用セルフホスト：** 自社クラウド / VPC、またはオフライン環境での分散デプロイと公式サポート。[問い合わせ](https://docs.google.com/forms/d/e/1FAIpQLScQqwsm7fvKdjtNiW5rWNXJjoHPtedVzLsKSMJgObtsj2_udA/viewform)。

リモート接続を許可する前に[認証](https://docs.openviking.ai/en/guides/04-authentication)を設定してください。共有環境では[ユーザー分離](https://docs.openviking.ai/en/concepts/11-multi-tenant)と[リソース権限](https://docs.openviking.ai/en/concepts/15-acl)も確認してください。

## 研究

記憶管理、ディレクトリ検索、構造化文書 RAG を支える研究：

- [**VikingMem**](https://arxiv.org/abs/2605.29640)：長期記憶の抽出・更新・統合。
- [**Directory-Aware Query and Maintenance in Vector Databases**](https://arxiv.org/abs/2606.16903)：ディレクトリ単位の索引と検索。
- [**VikingRAG**](https://arxiv.org/abs/2609.11390)：少ないトークンで構造化文書から根拠を取得。

## コミュニティ・お問い合わせ

[Volcano Engine の Viking チーム](https://docs.openviking.ai/en/about/01-about-us)とオープンソースの貢献者が開発しています。使い方の相談や事例の共有はこちらへ：

<p>
  <a href="https://discord.com/invite/eHvx8E9XF3"><img src="docs/images/community/discord.svg" width="18" height="18" alt="">&nbsp;Discord</a> ·
  <a href="https://docs.openviking.ai/en/about/01-about-us#lark-group"><img src="docs/images/community/lark.svg" width="18" height="18" alt="">&nbsp;Lark</a> ·
  <a href="https://docs.openviking.ai/en/about/01-about-us#wechat-group"><img src="docs/images/community/wechat.svg" width="18" height="18" alt="">&nbsp;WeChat</a>
</p>

| 目的 | 窓口 |
| --- | --- |
| バグ報告・機能の提案 | [GitHub Issues](https://github.com/volcengine/OpenViking/issues) |
| リリース・技術記事 | <a href="https://x.com/openvikingai"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/images/community/x-dark.svg"><img src="docs/images/community/x.svg" width="16" height="16" alt=""></picture>&nbsp;@openvikingai</a> · [ブログ](https://blog.openviking.ai/) · [リリース](https://github.com/volcengine/OpenViking/releases) |
| 商用デプロイの相談 | [問い合わせフォーム](https://docs.google.com/forms/d/e/1FAIpQLScQqwsm7fvKdjtNiW5rWNXJjoHPtedVzLsKSMJgObtsj2_udA/viewform) |
| 脆弱性の非公開報告 | [セキュリティポリシー](SECURITY.md)。公開 Issue には投稿しないでください |

Lark のリンク先に参加用 QR コードがあります。WeChat は QR コードでアシスタントを追加し、「OpenViking」と伝えるとグループに招待されます。

**開発に参加する：** コード、ドキュメント、連携、再現可能なバグ報告を歓迎します。[コントリビューションガイド](CONTRIBUTING_JA.md)から始めてください。

**パートナープロジェクト：** [DeerFlow](https://github.com/bytedance/deer-flow) · [NoKV](https://github.com/NoKV-Lab/NoKV) · [loopx](https://github.com/huangruiteng/loopx) · [Hermes Agent](https://github.com/NousResearch/hermes-agent)

## ライセンス

サーバーは [AGPLv3](LICENSE)、[CLI](crates/LICENSE) と[サンプル](examples/LICENSE)は Apache 2.0 です。[Hermes プラグイン](examples/hermes-plugin/LICENSE)は MIT、サードパーティのコンポーネントは各自のライセンスを維持します。
