# harness-broker

プロファイルからコーディングエージェント（Claude Code、Codex など）のセッションを隔離コンテナで起動し、セッション同士を MCP 経由で送受信させるブローカーです。

- **ハーネスの抽象化**：管理下セッションは ACP（Agent Client Protocol）で操作するので、ハーネスの違いはアダプタが吸収します。
- **モジュールとプロファイル**：モジュールはエージェントの役割（指示と skill）だけを持つ、ハーネスに依存しない定義です。プロファイルはそれを実行環境（ハーネス、モデル、イメージ、権限、ワークスペース）に結びつける YAML で、A2A の Agent Card の項目も持ちます。
- **再現性**：イメージは digest、ワークスペースはコミット SHA で固定します。CLAUDE.md・AGENTS.md・`.claude/` などプロジェクト側の設定は全階層から除去し、ホストの `~/.claude` や `~/.codex` も読ませません。
- **実行履歴**：送信原文、セッションに実際に投入した文面、ACP の生のやりとりまで、追記専用で記録します。

状態は v0.3 です。末尾の「未実装・未確認」も読んでください。

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
- 通信の道具は `send` と `receive` の2つだけです（次の「通信モデル」）。

## 通信モデル

セッションは全員（管理下・外部・操作者）、受信箱を1つ持ちます。`send` は宛先の受信箱に入れてすぐ返り、メッセージは次のどちらかで取り出されるまで未読のままです。

| 取り出し方 | 待てる時間 | 使う場面 |
| --- | --- | --- |
| `receive` を呼ぶ（`from`・`reply_to` で絞り込める） | 1回最大 600 秒 | 来ると分かっている返事を待つ |
| ターンを終える（管理下セッションのみ） | 無期限 | 次の依頼を待つ。ブローカーが最も古い未読を次のプロンプトとして投入する |

- **通知**：`receive` 以外のツールの結果には、未読の件数と送信元を添えます（ブリッジは本文の後ろに1行足します。HTTP では `X-HB-Unread` ヘッダー）。中身は `receive` で読みます。Channels でつないだ Claude Code には本文ごと push します。
- **最終応答の翻訳規則**：管理下セッションのターンがメッセージ M で始まり、M 自身が返信（`reply_to` 付き）でないとき、ターンの終わりに次のどれかが起きます。
  - そのターン中に M の送信元へ `reply_to: M` で `send` していれば、最後の文章は履歴に残すだけ。
  - していなければ、最後の文章（最後のツール呼び出しより後の文章）を M への返信として送信元に送る。
  - 最後の文章が空なら、「返信なしでターンが終わった」という通知を送信元に送る。
- 返信で始まったターンは何も自動で送りません。自動の返信同士が往復し続けないためです。
- セッションが終わると、未読のまま残ったメッセージは破棄し、返事を待っている送信元（返信でないメッセージの送信元）に「答える前に終了した」と知らせます。
- 受け取ったメッセージの `origin` で出どころが分かります：`send`（`send` ツール）、`final`（最後の文章を返信にしたもの）、`notice`（ブローカーの通知）。

依頼して答えを受け取る基本の形はこうなります。

```sh
hb call send '{"to": "reviewer", "body": "src/ の変更をレビューして"}' --config hb.yaml   # → message_id
hb call receive '{"reply_to": "<message_id>", "wait": 600}' --config hb.yaml
```

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
export ANTHROPIC_API_KEY=...          # プロファイルの secrets に書いた名前
hb daemon --config hb.yaml
```

claude-agent-acp が `ANTHROPIC_API_KEY` をそのまま使えるかは実機で未確認です。使えなければ `CLAUDE_CODE_OAUTH_TOKEN` で渡します。

## モジュール

モジュールは `module.json` を持つディレクトリです。

```json
{
  "schemaVersion": 1,
  "id": "akinator",
  "name": "アキネーター（質問役）",
  "description": "呼び出す側が読む用途の説明",
  "instructions": "AGENTS.md"
}
```

- `instructions` のファイルがハーネスの指示（Claude Code なら `CLAUDE.md`、Codex なら `AGENTS.md`）として配置されます。
- `skills/<name>/SKILL.md` があれば skill として配置されます（`skills` で場所を変更可）。
- モジュールのディレクトリの外のファイル（requirements、evaluations など）はエージェントに渡りません。
- ハーネスやモデルの設定はモジュールに置きません。それはプロファイルの役目です。

実例：[ysgrProgramming/akinator](https://github.com/ysgrProgramming/akinator)（`execution/` と `requirements/answerer/`）、[ysgrProgramming/romaji-converter](https://github.com/ysgrProgramming/romaji-converter)（`execution/`）。

## プロファイル

例：[akinator.yaml](profiles/examples/akinator.yaml)、[akinator-answerer.yaml](profiles/examples/akinator-answerer.yaml)、[romaji-converter.yaml](profiles/examples/romaji-converter.yaml)（モジュールを使う例）、[reviewer.yaml](profiles/examples/reviewer.yaml)、[implementer.yaml](profiles/examples/implementer.yaml)（指示をプロファイルに直接書く例）

| フィールド | 内容 |
| --- | --- |
| `id` / `version` / `name` / `description` / `skills` | Agent Card 互換。`list_profiles` で返す。モジュールを使う場合、`name` と `description` は省略するとモジュールのものになる |
| `module.repo` / `ref` / `path` | モジュールのリポジトリ、コミット SHA（ブランチ名は拒否）、リポジトリ内のディレクトリ（既定 `execution`）。コミットごとに一度だけ取得してキャッシュする |
| `module.local` / `path` | 開発用。手元のディレクトリを使う。中身のハッシュが fingerprint に入る |
| `spawnableBy` | 起動を許す相手。`external`、`*`、またはプロファイル id |
| `runtime.harness` | `claude-code` / `codex` / `gemini` / `custom` |
| `runtime.adapter` | コンテナ内で起動する ACP アダプタのコマンド |
| `runtime.model` | モデル ID（固定値） |
| `runtime.image` | `sha256:<image id>` または `<name>@sha256:<digest>`。タグは拒否 |
| `runtime.instructions` / `bundledSkills` | モジュールを使わない場合の指示と skill。`module` とは併用できない |
| `runtime.messageFormat` | `envelope`（既定。送信元・メッセージID・スレッドの見出しを付ける）か `body`（本文だけをそのまま渡す） |
| `runtime.sessionMode` | `persistent`（既定。1つの会話を続ける）か `per_message`（メッセージごとに新しい会話。コンテナは同じ） |
| `runtime.brokerTools` | ブローカーのツール（spawn、send など）を持たせるか。既定 `true` |
| `runtime.permissions` | ACP の権限要求への答え方（`mode`、`allowKinds`）と、ハーネス側の許可・拒否リスト（`allowedTools`、`deniedTools`） |
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

**Codex（ツール結果の通知を見て `receive`）**

```sh
codex mcp add hb -- hb mcp --harness codex
```

## ツール

| ツール | 内容 |
| --- | --- |
| `list_profiles` | プロファイル一覧（Agent Card 相当） |
| `spawn` | プロファイルからセッションを起動。`task` を渡すと最初のメッセージになり、結果の `task_message_id` で返事を `receive` できる |
| `send` | セッション（id か名前）の受信箱に入れて、すぐ `message_id` を返す |
| `receive` | 自分宛ての未読を取り出す。`from`・`reply_to` で絞り込み、`wait` 秒（最大 600）まで待てる |
| `list_sessions` / `status` | 一覧と状態（直近の応答、経過時間、使用量） |
| `terminate` | 自分が起動したセッション（または自分）を終了。記録を回収してからコンテナを破棄 |
| `history` | 実行履歴を原文のまま取り出す |

操作者（鍵の持ち主）は `hb call <tool> '<json>'` で同じツールを呼べます。

```sh
hb call spawn '{"profile": "reviewer", "task": "src/ の変更をレビューして"}' --config hb.yaml
hb call receive '{"reply_to": "<task_message_id>", "wait": 600}' --config hb.yaml
hb call history '{"session": "reviewer"}' --config hb.yaml
```

## 実行履歴

`<state_dir>/history/` に保存します。

- `events/YYYY-MM-DD.jsonl`：全イベントを 1 行 1 JSON で追記（要約・切り詰めなし）
- `index.sqlite3`：検索用の索引と、セッション・メッセージの表
- `sessions/<id>/harness/`：終了時にコンテナから回収した、ハーネス自身のセッション記録
- `sessions/<id>/agent.stderr.log`：アダプタの標準エラー

主なイベント：`session.spawn`（解決済みプロファイルの全文と fingerprint）、`session.workspace`（コミットと除去したパス）、`message.send`（送信原文と `origin`）、`message.deliver`（取り出し方：`turn` / `receive` / `dropped`）、`message.inject`（実際に投入した文面）、`acp.out` / `acp.in`（ACP のフレームそのまま）、`acp.permission`、`turn.end`（最後の文章と、翻訳規則で何が起きたか）、`session.end`

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
- **作業の中断**：実行中のターンを止めるメッセージの表し方は未決です。いまは届いたメッセージを受信箱に入れるだけです。
- **すべてのツール呼び出しでの通知**：通知はブローカーのツールの結果にしか付きません。ハーネス自身のツール（ファイル操作など）の結果にも付けるには、ハーネスのフックが必要です（未実装）。
- **起動後の設定検証**：読み込まれた指示ファイルが想定どおりかを、起動直後に確かめる仕組みはまだありません。
- **履歴**：保持期間の設定と、宣言外の秘密情報のマスキングはありません。
- **単一ホストのみ**です。
- **費用の上限**：トークンや費用の上限はまだありません。制限できるのは実行時間と同時起動数だけです。
- **ツールの無効化**：`deniedTools` は Claude Code の `settings.json` にだけ書き込みます。Codex のツール制限は未対応です。
- 当初は TypeScript を想定していましたが、開発環境の都合で Python で実装しました。MCP ブリッジは、Claude Code Channels の通知を任意のタイミングで送るために自前で実装し、公式 MCP SDK のクライアントで動作を確認しています。
