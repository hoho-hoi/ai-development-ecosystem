# 引き継ぎメモ（harness-broker v0.1）

Claude アプリのセッションで作った v0.1 を、Claude Code で続けるためのメモです。

## 目的

ハーネス（Claude Code、Codex など）を抽象化し、次のことを MCP 経由でできるようにする。

- プロファイルからセッションを起動する
- セッション同士で送受信する

プロファイルから生まれるセッションは隔離コンテナで動かし、ホストやプロジェクトの設定・skill を読ませない（再現性のため）。実行履歴は送信原文まで残す。

仕様書（ドラフト）：https://claude.ai/code/artifact/8941d9d5-a033-44a9-8457-579be8a8a514

## 現状

- 実装は `src/hbroker/` にあり、構成と使い方は README.md のとおり。
- テストは `cd tests && python3 -m unittest test_units test_e2e` で、35 件すべて通る。
- Docker の通しテスト（`tests/test_docker.py`、手順は README）も通る。確認できたのは次の点。
  - エージェントが uid 10001・ケーパビリティなし・マウントなしで動く
  - ワークスペースから設定ファイルが除去されている
  - コンテナからブリッジ経由でブローカーに接続できる
  - 終了時に記録を回収し、コンテナを削除する
- テストはすべて偽の ACP エージェント（`tests/fake_acp_agent.py`）で行った。本物の Claude Code・Codex ではまだ動かしていない。

## 決めたこと

- **言語は Python**：作成環境から npm と PyPI に接続できなかったため。依存は pyyaml、pydantic、starlette、uvicorn。
- **ブローカーは自前の薄い実装**：repowire・clawtool などは土台にしていない。ACP と MCP の上に作っている。
- **MCP ブリッジ（`bridge.py`）は標準ライブラリだけで自作**：Claude Code Channels の通知（`notifications/claude/channel`）を任意のタイミングで送るため。公式 MCP SDK のクライアントで動作確認済み。
- **ワークスペースはホスト側で作る**：固定コミットから clone し、設定ファイルを除去してから tar でコンテナに `docker cp` する。ホストのディレクトリはマウントしない。
- **配送の方式**
  - 管理下セッションへの送信はキューに積み、ACP の `session/prompt` で投入する。割り込みはしない。
  - 最終応答は送信元に自動で返す。
  - 応答する前にセッションが終わった場合は、送信元にその旨を通知する。
- **履歴の閲覧権限**：操作者はすべて読める。セッションが読めるのは、自分、自分が起動したセッション、自分が参加したスレッドだけ。
- **待ち受け先**：docker ランナーでは、ブローカーは 127.0.0.1 と `hb-net` のゲートウェイ IP の両方で待ち受ける。

## 次にやること（優先順）

1. **実機での確認**：`images/claude-code/Dockerfile` をビルドし、`profiles/examples/reviewer.yaml` の image を書き換えて `hb call spawn` から応答まで通す。トークンは `CLAUDE_CODE_OAUTH_TOKEN` で渡す。あわせて `claude-agent-acp` の版と挙動を確認する。
2. **外部の Claude Code**：`hb mcp --channel` で、Channels 経由で受信できるか確認する。
3. **Codex**：`codex-acp` イメージを確認し、新しい `CODEX_HOME` での認証の渡し方を決める。
4. **リポジトリへの組み込み**：このリポジトリの `Makefile` と CI に harness-broker のテストを入れる。CONTRIBUTING により `make test` が必須。
5. **残りの未実装**：MCP Tasks、外向き通信の制限（egress プロキシ）、Gemini への組み込み、起動後の設定検証、実行中ターンへの割り込み（README の「未実装・未確認」も参照）。

このリポジトリの開発ルールは `.cursor/rules/*.mdc` と CONTRIBUTING.md にある（役割分担と TDD）。
