# harness-broker

プロファイルからコーディングエージェント（Claude Code、Codex など）のセッションを隔離コンテナで起動し、セッション同士を MCP 経由で送受信させるブローカーです。

- **ハーネスの抽象化**：管理下セッションは ACP（Agent Client Protocol）で操作するので、ハーネスの違いはアダプタが吸収します。
- **プロファイル**：A2A の Agent Card に実行環境（イメージ、モデル、指示、skill、権限、ワークスペース）を足した YAML です。
- **再現性**：イメージは digest、ワークスペースはコミット SHA で固定します。CLAUDE.md・AGENTS.md・`.claude/` などプロジェクト側の設定は全階層から除去し、ホストの `~/.claude` や `~/.codex` も読ませません。
- **実行履歴**：送信原文、セッションに実際に投入した文面、ACP の生のやりとりまで、追記専用で記録します。

状態は v0.1（最小構成）です。末尾の「未実装・未確認」も読んでください。

## 構成

```
外部セッション（人が起動）          ブローカー（hb daemon）              管理下セッション（コンテナ）
┌───────────────────────┐         ┌─────────────────────────┐         ┌──────────────────────────┐
│ Claude Code / Codex   │  MCP    │ HTTP API                │  ACP    │ ACP アダプタ + ハーネス   │
│  └ hb mcp（ブリッジ）  │────────▶│ SessionManager          │────────▶│ /workspace（消毒済み）    │
│                       │◀────────│ 履歴（JSONL + SQLite）   │◀────────│  └ hb ブリッジ（MCP）     │
└───────────────────────┘ push /  │ Runner（docker / local）│  MCP    └──────────────────────────┘
                          pull    └─────────────────────────┘
```

- セッションは全員、stdio の MCP サーバー `hb mcp`（ブリッジ）を通してブローカーのツールを使います。ブリッジは標準ライブラリだけで書かれた単一ファイル（`src/hbroker/bridge.py`）で、イメージにもそのままコピーされます。
- 管理下セッションへの配送は、ブローカーが ACP の `session/prompt` で直接投入します。実行中のターンがあればキューに積み、終わり次第、到着順に投入します。
- 外部セッションへの配送は、Claude Code なら Channels で push、それ以外は次のツール呼び出しの結果に未読を添えるか、`inbox` で受け取ります。

## 必要なもの

- Python 3.11 以上、git、Docker
- 依存パッケージ：pyyaml、pydantic、starlette、uvicorn（テストでは公式の `mcp` SDK も使います）

## セットアップ

```sh
pip install -e .

# セッション用イメージを作る（リポジトリのルートで）
docker build -f images/claude-code/Dockerfile -t harness-broker/claude-code .
docker inspect --format '{{.Id}}' harness-broker/claude-code   # → sha256:... をプロファイルの runtime.image に書く

cp hb.example.yaml hb.yaml        # profiles_dir などを調整
hb profiles --config hb.yaml      # プロファイルの検証と fingerprint の表示
hb daemon --config hb.yaml
```

docker ランナーでは、ブローカーは `127.0.0.1` に加えてセッション用ネットワーク（既定 `hb-net`）のゲートウェイアドレスでも待ち受けます。コンテナはそこへ接続するので、API を全インターフェイスに公開する必要はありません。

認証情報は、プロファイルの `runtime.secrets` に**環境変数名だけ**を書きます。ブローカーを起動したシェルの環境から値を取り、`docker create --env NAME` で渡します。値は argv にも履歴にも残りません。

```sh
export CLAUDE_CODE_OAUTH_TOKEN=...   # claude-agent-acp は OAuth トークンを使う
hb daemon --config hb.yaml
```

## プロファイル

例：[profiles/examples/reviewer.yaml](profiles/examples/reviewer.yaml)、[profiles/examples/implementer.yaml](profiles/examples/implementer.yaml)

| フィールド | 内容 |
| --- | --- |
| `id` / `version` / `name` / `description` / `skills` | Agent Card 互換。`list_profiles` で返す |
| `spawnableBy` | 起動を許す相手。`external`、`*`、またはプロファイル id |
| `runtime.harness` | `claude-code` / `codex` / `gemini` / `custom` |
| `runtime.adapter` | コンテナ内で起動する ACP アダプタのコマンド |
| `runtime.model` | モデル ID（固定値） |
| `runtime.image` | `sha256:<image id>` または `<name>@sha256:<digest>`。タグは拒否 |
| `runtime.instructions` / `bundledSkills` | ハーネス専用の設定ディレクトリ（`CLAUDE_CONFIG_DIR` / `CODEX_HOME`）に配置 |
| `runtime.permissions` | ACP の権限要求への答え方（`mode`、`allowKinds`）と、ハーネス側の許可リスト（`allowedTools`） |
| `runtime.secrets` / `env` | 渡す秘密情報の環境変数名 / 秘密でない環境変数 |
| `workspace.repo` / `ref` | clone するリポジトリとコミット SHA（ブランチ名は拒否） |
| `workspace.strip` / `stripExtra` | 除去するファイル名のパターン（既定値は下記）/ その追加分 |
| `network.mode` / `proxy` | `bridge` か `none` / 外向き通信を絞るためのプロキシ |
| `limits` | 最大実行時間、spawn の深さ、同時起動数、メモリ、CPU、プロセス数 |

既定で除去するもの：`CLAUDE.md`、`CLAUDE.local.md`、`.claude`、`.mcp.json`、`AGENTS.md`、`AGENTS.override.md`、`.codex`、`GEMINI.md`、`.gemini`

`limits.maxSpawnDepth` は、そのプロファイルのセッションがさらに何段下まで spawn できるかです（0 なら spawn 不可）。親の残りの深さとブローカー全体の `max_spawn_depth` も超えられません。

## 外部セッションをつなぐ

外部セッションのブリッジは、ブローカーの鍵（`<state_dir>/broker.key`、権限 0600）で自分を登録します。

**Claude Code（Channels で即時に受信）**

```sh
claude mcp add --scope user hb -- hb mcp --harness claude-code --channel
claude --dangerously-load-development-channels server:hb
```

Channels は claude.ai ログインが必要で、セッションを開いている間だけ届きます。`--channel` を付けなければ、Codex と同じ方式で受け取ります。

**Codex（次のツール呼び出しで受信）**

```sh
codex mcp add hb -- hb mcp --harness codex
```

## ツール

| ツール | 内容 |
| --- | --- |
| `list_profiles` | プロファイル一覧（Agent Card 相当） |
| `spawn` | プロファイルからセッションを起動。`task` を渡すと最初のメッセージになり、返事は自分宛てに届く |
| `send` | セッション（id か名前）へ送信。管理下セッションの最終応答は、自動で送信元に返る |
| `inbox` | 未読メッセージを受け取る。`wait` 秒まで待てる |
| `list_sessions` / `status` | 一覧と状態（直近の応答、経過時間、使用量） |
| `terminate` | 自分が起動したセッション（または自分）を終了。記録を回収してからコンテナを破棄 |
| `history` | 実行履歴を原文のまま取り出す |

操作者（鍵の持ち主）は `hb call <tool> '<json>'` で同じツールを呼べます。

```sh
hb call spawn '{"profile": "reviewer", "task": "src/ の変更をレビューして"}' --config hb.yaml
hb call inbox '{"wait": 300}' --config hb.yaml
hb call history '{"session": "reviewer"}' --config hb.yaml
```

## 実行履歴

`<state_dir>/history/` に保存します。

- `events/YYYY-MM-DD.jsonl`：全イベントを 1 行 1 JSON で追記（要約・切り詰めなし）
- `index.sqlite3`：検索用の索引と、セッション・メッセージの表
- `sessions/<id>/harness/`：終了時にコンテナから回収した、ハーネス自身のセッション記録
- `sessions/<id>/agent.stderr.log`：アダプタの標準エラー

主なイベント：`session.spawn`（解決済みプロファイルの全文と fingerprint）、`session.workspace`（コミットと除去したパス）、`message.send`（送信原文）、`message.inject`（実際に投入した文面）、`acp.out` / `acp.in`（ACP のフレームそのまま）、`acp.permission`、`turn.end`、`session.end`

セッションのトークンと、プロファイルが宣言した秘密情報の値は、書き込む前に `[REDACTED:…]` に置き換えます。操作者はすべての履歴を読めます。セッションが読めるのは、自分、自分が起動したセッション、自分が参加したスレッドの履歴だけです。

## セキュリティ

- コンテナ：`--cap-drop ALL`（展開に必要な最小限だけ追加）、`no-new-privileges`、プロセス数の上限、ホストのディレクトリはマウントしない、エージェントは uid 10001 で実行
- 認証：ブローカーの鍵（操作者と外部セッションの登録用）と、セッションごとのトークン
- spawn：`spawnableBy`、深さの上限、同時起動数の上限
- 受信メッセージはほかのエージェントからの情報として扱うよう、ブリッジの指示とプロファイルの例に書いています

## テスト

```sh
cd tests
python3 -m unittest test_units test_e2e     # ローカルランナーと偽の ACP エージェント、公式 MCP SDK クライアント
docker build -f docker/Dockerfile -t hb-test-agent ..
HB_TEST_DOCKER_IMAGE=$(docker inspect --format '{{.Id}}' hb-test-agent) python3 -m unittest test_docker
```

## 未実装・未確認

- **本物のハーネスでは未検証**：テストは偽の ACP エージェントで行っています。`claude-agent-acp` と `codex-acp` の版（Dockerfile の既定値）は公開情報から取ったもので、実機での確認が必要です。
- **Codex の認証**：セッションごとに新しい `CODEX_HOME` を使うため、ログイン情報の渡し方が未確定です。
- **Gemini**：指示と skill のファイルは用意しますが、Gemini CLI への組み込みは未実装です。
- **MCP Tasks**：`spawn` は Tasks 拡張ではなく、`wait` で起動完了まで待つか `status` で確認する方式です。
- **外向き通信の制限**：`network.egress` は宣言だけです。実際に絞るには `network.proxy` にフィルタ付きプロキシを指定するか、`network.mode: none` にします。
- **実行中ターンへの割り込み**：キューに積むだけで、割り込みはしません。
- **起動後の設定検証**：読み込まれた指示ファイルが想定どおりかを、起動直後に確かめる仕組みはまだありません。
- **履歴**：保持期間の設定と、宣言外の秘密情報のマスキングはありません。
- **単一ホストのみ**です。
- 当初は TypeScript を想定していましたが、開発環境の都合で Python で実装しました。MCP ブリッジは、Claude Code Channels の通知を任意のタイミングで送るために自前で実装し、公式 MCP SDK のクライアントで動作を確認しています。
