# agora

コーディングエージェント（Claude Code、Codex、OpenCode など）のセッションを、モジュールから隔離コンテナで起動し、セッション同士を MCP 経由でやりとりさせる仕組みです。

セッションは操り人形ではなく、ひとりの「人」として扱います。人は自分の鍵と住所と受信箱を持ち、誰とでも `send` と `receive` で話します。現実の人と違うのは次の点です。必要なときに作れ、要らなくなればすぐ消せ、同じ人からいくらでも複製（clone）でき、何を考えているかを誰でも読めます。

- **中心を置かない**：人が住むのは「場所（place）」です。マシンごとに1つ動かす `agora daemon` が場所で、場所どうしは対等に直接話します。別のマシンにいる人にも、住所 `id@場所` 宛てに送れます。
- **ハーネスの抽象化**：管理下セッションは ACP（Agent Client Protocol）で操作するので、ハーネスの違いはアダプタが吸収します。
- **モジュール**：1つのエージェントを作るための情報を全部持つディレクトリです（形式は Agent Plugins の `plugin.json`）。指示・skill・MCP サーバーに加えて、どう起動するか（ハーネスとその版、モデル、使えるツール、上限、認証の参照）も `plugin.json` に書きます。agora は起動条件をすべてモジュールから読みます。
- **再現性**：モジュールとワークスペースはコミット SHA で、ハーネスはモジュールが指定する版で固定し、実際に使ったイメージの ID・モデル・ファイルの hash を履歴に残します。CLAUDE.md・AGENTS.md・`.claude/` などプロジェクト側の設定は全階層から除去し、ホストの `~/.claude` や `~/.codex` も読ませません。
- **実行履歴**：送信原文、セッションに実際に投入した文面、ACP の生のやりとりまで、追記専用で記録します。履歴は誰でも読め、動いている最中の人の考えも追いかけられます。

状態は v0.9 です。設計は[仕様書](https://claude.ai/code/artifact/8941d9d5-a033-44a9-8457-579be8a8a514)にあります。末尾の「未実装・未確認」も読んでください。

## 構成

```
            場所 pc1（agora daemon）                                場所 pc2（agora daemon）
┌──────────────────────────────────────┐   署名付き HTTP   ┌──────────────────────────────────────┐
│ 住人の受信箱・SessionManager           │◀────────────────▶│ 住人の受信箱・SessionManager           │
│ 履歴（JSONL + SQLite）・場所の鍵        │  /federation/*   │ 履歴（JSONL + SQLite）・場所の鍵        │
│ Runner（docker / local）              │                  │ Runner（docker / local）              │
└──┬──────────────────────┬────────────┘                  └──┬───────────────────────────────────┘
   │ MCP（agora mcp）         │ ACP                               │ ACP
┌──┴───────────────┐  ┌───┴──────────────────────┐        ┌───┴──────────────────────┐
│ 外部セッション     │  │ 管理下セッション（コンテナ） │        │ 管理下セッション（コンテナ） │
│ Claude Code など  │  │ ハーネス + agora ブリッジ      │        │ ハーネス + agora ブリッジ      │
└──────────────────┘  └──────────────────────────┘        └──────────────────────────┘
```

- **人（セッション）**：管理下セッション（場所がコンテナで起動する）、外部セッション（人が自分で起動した Claude Code などをつなぐ）、操作者（`agora` コマンドを使うあなた）の3種類です。全員が Ed25519 の鍵を持ち、id は公開鍵から作ります（`s-` + SHA-256 の先頭16桁）。秘密鍵は住んでいる場所が持ち、コンテナの中のエージェントには渡しません。
- **住所**：`id@場所`。同じ場所の中なら id か名前だけで通じます。
- **場所**：マシンごとの `agora daemon`。自分の住人の受信箱と履歴を持ち、話してよい場所（`peers`）を名前・URL・公開鍵で設定します。
- セッションは全員、stdio の MCP サーバー `agora mcp`（ブリッジ）を通して自分の場所のツールを使います。ブリッジは標準ライブラリだけで書かれた単一ファイル（`src/agora/bridge.py`）で、イメージにもそのままコピーされます。

## 通信モデル

通信の道具は `send` と `receive` の2つだけです。ツール名に接頭辞は付けません。

人は全員（管理下・外部・操作者）、受信箱を1つ持ちます。受信箱は住んでいる場所にあります。`send` は宛先の受信箱に入れてすぐ返り（別の場所の人宛てなら、その場所に届けてから返ります）、メッセージは次のどちらかで取り出されるまで未読のままです。

| 取り出し方 | 待てる時間 | 使う場面 |
| --- | --- | --- |
| `receive` を呼ぶ（`from`・`reply_to` で絞り込める） | 1回最大 600 秒 | 来ると分かっている返事を待つ |
| ターンを終える（管理下セッションのみ） | 無期限 | 次の依頼を待つ。場所が最も古い未読を次のプロンプトとして渡す |

- **最後の文章は誰にも届かない**：ターンの終わりにエージェントが書いた文章は、履歴（`turn.end`）に残すだけです。返事も報告も `send` で送ります。場所が自分からメッセージを作ることはありません。
- **署名**：メッセージは送った人の鍵で署名します。受け取った場所は、届けた場所の言い分ではなく、署名と「公開鍵から作った id」が一致するかで送り主を確かめます。操作者のメッセージは場所の鍵で署名します。
- **返信**：`reply_to` にもとのメッセージの id を付けた `send`。宛先を省くと、もとのメッセージの送信元に届き（別の場所でも）、スレッドも引き継ぎます。
- **相手が黙ったとき**：`receive` に `from` を付けて待っていると、その相手がもう何も送れない状態（管理下セッションが待機中で未読もない、または終了した）になった時点で、空のまま返ります（`from_quiet: true`）。相手が別の場所にいれば、その場所に問い合わせます。返事をせずにターンを終えたことも、落ちたことも、待つ側はこれで知ります。
- **通知**：`receive` 以外のツールの結果には、未読の件数と送信元を添えます（ブリッジは結果の後ろに1行足します。HTTP では `X-Agora-Unread` ヘッダー）。中身は `receive` で読みます。Channels でつないだ Claude Code には本文ごと push します。
- **管理下セッションへの渡し方**：見出しを付けた本文を、ACP のプロンプトとして渡します。

```
from: akinator-case (s-3f2a91c0e1b2a4d7@pc1)
message_id: m-81c04d2e7a19
thread_id: t-6c76c0c21fa4

対象を1つ決めました。質問を始めてください。
```

- セッションが終わると、未読のまま残ったメッセージは捨てて、履歴（`message.dropped`）に残します。誰にも何も送りません。

依頼して答えを受け取る基本の形はこうなります。

```sh
agora call send '{"to": "reviewer", "body": "src/ の変更をレビューして"}' --config agora.yaml   # → message_id
agora call receive '{"from": "reviewer", "reply_to": "<message_id>", "wait": 600}' --config agora.yaml
```

## 必要なもの

- Python 3.11 以上、git、Docker
- 依存パッケージ：pyyaml、pydantic、starlette、uvicorn、cryptography（テストでは公式の `mcp` SDK も使います）

## セットアップ

```sh
pip install -e .
cp agora.example.yaml agora.yaml        # place、modules（登録するモジュール）などを調整
agora modules --config agora.yaml       # 登録したモジュールの検証と、この場所で使う起動条件・fingerprint の表示
agora daemon --config agora.yaml
```

ハーネスのイメージは、モジュールが指定したハーネスと版から、初めて使うときに `images/<harness>/Dockerfile` でビルドします（`agora/<harness>:<版>`）。ベースイメージやビルドのネットワークは `docker.build_args`・`docker.build_network` で場所ごとに変えられます。既にあるイメージを使うときは `harnesses.<harness>.image` に書きます。

docker ランナーでは、場所は `127.0.0.1` に加えてセッション用ネットワーク（既定 `agora-net`）のゲートウェイアドレスでも待ち受けます。コンテナはそこへ接続するので、1台で使うだけなら API を全インターフェイスに公開する必要はありません。

認証情報は、モジュールの起動条件の `env` に「コンテナの中の変数名 → マシンの環境変数」の参照として書きます（`"CLAUDE_CODE_OAUTH_TOKEN": "${AGORA_CLAUDE_OAUTH_TOKEN}"`）。値は `agora daemon` を起動したシェルの環境から取り、`docker create --env NAME` で渡します。値はモジュールにも argv にも履歴にも残りません。

```sh
export AGORA_CLAUDE_OAUTH_TOKEN=...   # claude setup-token で作ったもの
agora daemon --config agora.yaml
```

## 場所をつなぐ（複数のマシン）

マシンごとに `agora daemon` を動かし、互いを `peers` に書きます。中心になるサーバーはありません。

```sh
# pc1 で：自分の名前・URL・公開鍵を、相手の設定に貼る形で表示する
agora place --config agora.yaml
# place pc1: add this to the peers of the places it talks to
# peers:
#   - name: pc1
#     url: http://pc1.tailnet.example:8765
#     public_key: 9f2c...（64桁）
```

```yaml
# pc1 の agora.yaml
place: pc1
public_url: http://pc1.tailnet.example:8765
listen_host: 0.0.0.0          # ほかのマシンから届くように（VPN のアドレスでもよい）
peers:
  - name: pc2
    url: http://pc2.tailnet.example:8765
    public_key: 3b6a...       # pc2 で agora place を実行して得たもの
```

pc2 にも同じように pc1 を書けば、双方向に話せます。

- 場所どうしの要求は、送る側の場所の鍵で署名します（メソッド・パス・時刻・本文のハッシュ）。`peers` にない場所や鍵の合わない要求、時刻が5分以上ずれた要求は断ります。
- 相手の場所を信頼することは、その場所が自分の住人と操作者の代わりに話すのを認めることです。別の場所の操作者は、こちらでも操作者として扱います。
- 場所は第三の場所への中継はしません。
- 通信は暗号化しません（署名だけ）。マシンの間は Tailscale などの VPN か SSH トンネルを通してください。

別の場所でのセッションの起動と会話：

```sh
agora call spawn '{"module": "reviewer", "place": "pc2", "task": "src/ の変更をレビューして"}' --config agora.yaml
# → {"address": "s-…@pc2", "task_message_id": "m-…", …}
agora call receive '{"from": "s-…@pc2", "reply_to": "m-…", "wait": 600}' --config agora.yaml
```

起動されたセッションは pc2 に住み、pc2 に登録されたモジュールとそこで作ったイメージで動きます。親は `operator@pc1` です。

## LLM ゲートウェイ（LiteLLM）

本物の API キーは [LiteLLM Proxy](https://docs.litellm.ai/) だけに持たせます。`agora.yaml` に `llm_gateway` を書くと、agora はセッションを起動するたびに LiteLLM で仮想キーを1本発行し（`/key/generate`。metadata にセッション・モジュール・版・fingerprint・場所）、終了時に削除します（`/key/delete`）。

- セッションには `ANTHROPIC_BASE_URL`（LiteLLM）と `ANTHROPIC_API_KEY`（仮想キー）を渡します。仮想キーは履歴で伏せます。いまは Claude Code のハーネスだけが対象です。
- 費用は LiteLLM がキーごとに集計するので、セッション・モジュール・版ごとに読めます（LiteLLM のデータベースが要ります）。
- モジュールの起動条件に認証を書く必要はなくなります。
- LiteLLM の設定（モデルの一覧、本物のキー、データベース）は LiteLLM 側で行います。

## トレース（OpenTelemetry）

`agora.yaml` に `telemetry.otlp_endpoint` を書くと、セッションのトレースを OTLP/HTTP で送ります。閲覧は Phoenix など OTLP を受けるものなら何でも構いません。追記専用の履歴は今のまま残り、トレースは閲覧用です。

| スパン | 内容 |
| --- | --- |
| `invoke_agent <モジュール>` | 1セッション。spawn されたセッションは、起動したセッションの子になる（1本の木）。属性：`gen_ai.agent.name`、`gen_ai.request.model`、版、fingerprint、ハーネス |
| `turn` | 1ターン（届いたメッセージ1通の処理）。message_id、終了理由 |
| `execute_tool <名前>` | ACP のツール呼び出し1回 |
| イベント `send` | セッションが送ったメッセージ（宛先、message_id） |

## クローン

`clone` は、ある人のその時点の記憶を持った別の人を作ります。

- ワークスペースのその時点の中身と、ハーネス自身の会話の記録を写し、新しいコンテナで ACP の `session/load` によって会話を再開します。指示や設定はモジュールどおりに作り直し、記録だけを引き継ぎます。
- 新しい人には新しい鍵・住所・受信箱が付きます。クローンは元の人と同じ場所に住み、メッセージが来るまで待機します。その後は別々に考え、別々に作業します。
- 元の人は止まりません。作業中（ターンの途中）でも写せ、自分自身（`"self"`）も写せます。
- ハーネスが会話の再開（ACP の `loadSession`）に対応していないと失敗します。`sessionMode: per_message` のモジュールでは、会話は引き継がずワークスペースだけを写します。
- 写せるのは、そのモジュールを起動してよい人だけです（`spawnableBy` と深さの上限）。

```sh
agora call clone '{"session": "reviewer", "name": "reviewer-b"}' --config agora.yaml
```

## モジュール

モジュールは、1つのエージェントを作るのに必要な情報をまとめたディレクトリです。Docker でいう Dockerfile にあたり、agora はそれをコンテナとして動かします。agora 側にはモジュール固有のことを置きません。

形式は [Agent Plugins](https://agent-plugins.org/specification) 1.0.0 です。目印はルートの `plugin.json` で、Agent Plugins が扱わない「指示」と「起動条件」を、規格どおり agora 専用の拡張名前空間 `io.github.hoho-hoi.agora` に置きます。

```
my-module/
├── plugin.json                         # 目印と起動条件（必須）
├── skills/<名前>/SKILL.md              # Agent Skills（skills/ の直下だけ）
├── mcp.json                            # MCP サーバー（任意）
└── io.github.hoho-hoi.agora/AGENTS.md  # 指示（任意）
```

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "module-authoring",
  "version": "2.0.0",
  "description": "呼び出す側が読む用途の説明",
  "extensions": {
    "io.github.hoho-hoi.agora": {
      "runtimes": [
        {"harness": "claude-code", "version": "0.75.1", "model": "claude-sonnet-5-5",
         "env": {"CLAUDE_CODE_OAUTH_TOKEN": "${AGORA_CLAUDE_OAUTH_TOKEN}"}}
      ],
      "tools": ["send", "receive", "list_modules", "spawn", "status", "history", "terminate"],
      "builtinTools": true,
      "sessionMode": "persistent",
      "limits": {"maxRuntimeMinutes": 60, "maxSpawnDepth": 1, "maxConcurrent": 4},
      "spawnableBy": ["external", "*"]
    }
  }
}
```

| 項目（拡張の中） | 内容 |
| --- | --- |
| `runtimes` | 起動条件の候補。`harness`（`claude-code` / `codex` / `opencode`）、`version`（ハーネスの版。イメージはこれから作る）、`model`、`env`、`login`（`chatgpt`：Codex を場所の ChatGPT ログインで）。上から順に、この場所で使えるもの（参照している環境変数がすべてある）を選び、`session.spawn` に記録する。再現性のため、ふだんは1つにする |
| `env` | 値は文字列そのもの、またはマシンの環境変数を1つ参照する `${VAR}`。参照した値は秘密として扱い、履歴で伏せる |
| `tools` | 渡す agora のツール（`[send]` など）。`true` はすべて（既定）、`false` はなし。場所もセッションのトークンに同じ制限をかける |
| `builtinTools` | ハーネス組み込みのツール（シェル、ファイル操作、Web など）を使うか（既定 `true`）。権限はいつもフルで、確認待ちで止まらない。やらせたくないことは、ツールや MCP を渡さないことで防ぐ。`false` なら組み込みのツールを止め（Claude Code は拒否リスト、Codex は読み取り専用、権限要求は拒否）、agora のツールと MCP だけで動く |
| `sessionMode` | `persistent`（既定。1つの会話を続ける）か `per_message`（メッセージごとに新しい会話。コンテナは同じ） |
| `limits` | `maxRuntimeMinutes`、`maxSpawnDepth`（さらに何段下まで spawn できるか。0 なら不可。親の残りと場所の `max_spawn_depth` も超えない）、`maxConcurrent` |
| `spawnableBy` | 起動を許す相手。`external`（操作者・外部セッション）、`*`（どのセッションでも）、モジュール名。既定は両方 |
| `instructions` | 指示ファイル（名前空間ディレクトリからの相対パス。既定 `AGENTS.md`） |

- **plugin.json**：Agent Plugins の閉じたスキーマに従います（`$schema`・`name`・`version`・`description`・`author`・`homepage`・`repository`・`license`・`keywords`・`extensions` のみ）。`name` がモジュールの id で、`list_modules` と `spawn` で使います。
- **指示**：ハーネスの指示ファイル（Claude Code なら `CLAUDE.md`、Codex なら `AGENTS.md`、OpenCode なら agent の system）として配置します。ほかのクライアントはこの名前空間を無視し、skill と MCP だけを使います。
- **skill**：`skills/` の直下のディレクトリだけを読みます。`SKILL.md` の `name` はディレクトリ名と同じにします。
- **MCP サーバー**（`mcp.json`）：`stdio` と `http`（`streamable-http`）に対応します。モジュール全体をセッションの中に写し、`${PLUGIN_ROOT}` と `./` で始まる `command` をその場所に置き換えて、ACP の `session/new` で渡します。`command` は1つの実行ファイル（コマンドラインは不可）、`cwd` は使えません（ACP にない）、リモートは https のみ、名前 `agora` はブリッジ用に予約しています。
- **モデルの確認**：ハーネスが ACP でモデルの選択肢を示す場合、起動時に現在のモデルを確かめ、違えばモジュールのモデルに切り替えます。選択肢にないときは、別のモデルで動かさずに起動を失敗させます（`session.model` に記録）。
- **作業場所**：作業するリポジトリは依頼ごとに変わるので、`spawn` の `workspace`（`{repo, ref}`）で渡します。CLAUDE.md・AGENTS.md・`.claude`・`.mcp.json`・`.codex`・`.gemini`・`.opencode` などは全階層から除去します。
- サブエージェントは持ちません。別のエージェントが必要なら、agora で別のセッションを起こして話します。
- モジュールのディレクトリの外のファイルはエージェントに渡りません。リポジトリのどこに何を置くかは利用者の自由です。
- 以前の `module.json` は読みません（見つかると移行を促すエラーになります）。

### 登録

どのモジュールのどの版をこの場所で起動できるかは、`agora.yaml` の `modules` に書きます。リポジトリのモジュールはコミットごとに一度だけ取得してキャッシュします。

```yaml
modules:
  - repo: https://github.com/ysgrProgramming/romaji-converter.git
    ref: 5d1255bf8f9b226703984332a03461d2884d6316   # コミット SHA（ブランチ名は拒否）
    path: execution                                  # リポジトリ内のディレクトリ（既定はルート）
  - local: examples/modules/caller                   # 開発用。中身のハッシュが fingerprint に入る
```

見本のモジュール：[examples/modules/caller](examples/modules/caller)（依頼に合う登録済みのモジュールを選んで起動し、返事を取り次ぐ。セッションから別のセッションを起動できるかの確認用）

## OpenCode で動かす

OpenCode V2 は自身が ACP を話す（`opencode acp`）ので、アダプタは要りません。起動条件の例：

```json
{"harness": "opencode", "version": "2.3.0", "model": "opencode/deepseek-v4.1-flash-free"}
```

- 設定はモジュールだけから作ります。エージェントを1つ（`agora`）作り、モジュールの指示をその system に、skill を `skills` に入れた `opencode.json` を、セッション専用のディレクトリに置きます。
- `OPENCODE_CONFIG`・`OPENCODE_CONFIG_DIR` をそこに向け、プロジェクトの設定と CLAUDE.md 系の読み込み、自動更新を切り、XDG のディレクトリもセッションの中にします。ホストの `~/.config/opencode` や認証情報は読みません。
- provider の認証情報がないと、OpenCode は無料の別モデルで始めようとします。agora はそれを検出して起動を失敗させます。
- **無料モデル**：OpenCode Zen には期間限定の無料モデルがあり、API キーなしで使えます（`opencode/<名前>`。無料モデルは OpenCode のハーネスからだけ使える）。提供元がデータを保持・学習に使うものがあるので、ゼロリテンションと明記されたもの（`deepseek-v4.1-flash-free`、`glm-5.3-flash-free`、`minimax-m3.1-flash-free` など）を選びます。条件は OpenCode の Console 文書の「Free models」「Privacy」で確かめてください。
- コンテナが HTTP プロキシ経由でしか外に出られない場所では、`agora.yaml` の `network.proxy` を指定します（`HTTPS_PROXY` として渡ります。ブリッジの agora への通信はプロキシを通りません）。

## Codex を ChatGPT プランで動かす

ChatGPT（Plus・Pro など）のプランで Codex のセッションを動かせます。API キーは要りません。

```sh
npm install -g @openai/codex                     # ホストの Codex CLI（ログインに使う）
agora codex-login --config agora.yaml            # ブラウザでログイン（--device-auth でデバイスコード）
agora codex-login --status --config agora.yaml   # プランと有効期限の確認（トークンは表示しない）
agora daemon --config agora.yaml
```

モジュールの起動条件は `{"harness": "codex", "version": "2.2.2", "model": "gpt-5.5", "login": "chatgpt"}` のようにします。

- **ログインは場所が持つ**：`agora codex-login` は、ふだんの `~/.codex` とは別のディレクトリ（`codex_login_dir`、既定 `<state_dir>/codex-login`）にログインします。ChatGPT のリフレッシュトークンは1回しか使えず、2か所で更新するとログインが無効になるため、場所の外には出しません。
- **セッションにはアクセス用の写しだけ**：`login: chatgpt` のセッションには、リフレッシュトークンを空にした `auth.json` を渡します。アクセストークンはログインからおよそ10日有効で、その間はセッションの中で更新は起きません。
- **期限の確認**：アクセストークンの残りが、モジュールの `maxRuntimeMinutes` に30分を足した時間より短いと、セッションを起動せずに失敗させます。そうなったら `agora codex-login` でログインし直してください。
- **記録に残さない**：`auth.json` は実行履歴に回収しません。トークンの値は履歴に書く前に伏せます。
- **承認なしで send を使える**：agora のツールには MCP の注釈（`send` は破壊的でない・外部に出ない）を付けています。Codex は注釈のない MCP ツールには毎回承認を求めるためです。`builtinTools: false` のモジュールは Codex を読み取り専用（`INITIAL_AGENT_MODE=read-only`）で動かし、ウェブ検索も切ります。
- 使用量はそのアカウントのプランの上限に数えられます。

## 外部セッションをつなぐ

外部セッションのブリッジは、ブローカーの鍵（`<state_dir>/broker.key`、権限 0600）で自分を登録します。

**Claude Code（Channels で即時に受信）**

```sh
claude mcp add --scope user agora -- agora mcp --harness claude-code --channel
claude --dangerously-load-development-channels server:agora
```

Channels は claude.ai ログインが必要で、セッションを開いている間だけ届きます。`--channel` を付けなければ、Codex と同じ方式で受け取ります。

**Codex（ツール結果の通知を見て `receive`）**

```sh
codex mcp add agora -- agora mcp --harness codex
```

## ツール

| ツール | 内容 |
| --- | --- |
| `send` | 宛先（`id@場所`、または同じ場所の id か名前）の受信箱に入れて、すぐ `message_id` を返す。`reply_to` があれば宛先は省ける |
| `receive` | 自分宛ての未読を取り出す。`from`・`reply_to` で絞り込み、`wait` 秒（最大 600）まで待てる。`from` の相手が黙れば待たずに返る |
| `spawn` | モジュール（`module` に id）のセッションを起動（`place` で別の場所に）。`task` を渡すと最初のメッセージになり、結果の `task_message_id` で返事を `receive` できる。`workspace`（`{repo, ref}`）で作業するリポジトリを渡す。`module_ref` にコミット SHA を渡すと、そのモジュールを別の版（改善の候補など）にして起動する（リポジトリから登録したモジュールのみ。`session.spawn` に記録） |
| `clone` | セッション（`"self"` なら自分）の複製を起動。ワークスペースと会話をその時点のまま引き継ぐ |
| `list_modules` | 登録されたモジュールの一覧（id、説明、版、起動条件）と、ほかに知っている場所。`place` で別の場所に聞く |
| `list_sessions` / `status` | 一覧と状態（未読の数、直近のターンで書いた文章、経過時間、使用量）。`place`・`id@場所` で別の場所 |
| `terminate` | 自分が起動したセッション（または自分）を終了。記録を回収してからコンテナを破棄 |
| `history` | 誰の実行履歴でも原文のまま取り出す。`wait` で新しい記録が出るまで待てる |

操作者（鍵の持ち主）は `agora call <tool> '<json>'` で同じツールを呼べます。

```sh
agora call spawn '{"module": "reviewer", "task": "src/ の変更をレビューして"}' --config agora.yaml
agora call receive '{"from": "reviewer", "reply_to": "<task_message_id>", "wait": 600}' --config agora.yaml
agora call history '{"session": "reviewer"}' --config agora.yaml
agora call history '{"session": "s-…@pc2", "after_seq": 120, "wait": 60}' --config agora.yaml   # 動いている人を追う
```

## 実行履歴

`<state_dir>/history/` に保存します。

- `events/YYYY-MM-DD.jsonl`：全イベントを 1 行 1 JSON で追記（要約・切り詰めなし）
- `index.sqlite3`：検索用の索引と、セッション・メッセージの表
- `sessions/<id>/harness/`：終了時にコンテナから回収した、ハーネス自身のセッション記録
- `sessions/<id>/agent.stderr.log`：アダプタの標準エラー

履歴はその人が住む場所にあります。別の場所の人の履歴は、`history` に `id@場所` を渡せばその場所から取り出します。

主なイベント：`session.spawn`（解決した起動条件の全文と fingerprint、公開鍵）、`session.process`（使ったイメージの ID）、`session.module`（モジュールの取得元とファイルの hash）、`session.model`（確かめたモデル）、`session.clone`（元の人と、引き継いだ会話）、`session.workspace`（コミットと除去したパス）、`message.send`（送信原文と署名）、`message.arrive`（別の場所から届いたもの）、`message.deliver`（取り出し方：`turn` / `receive` / `dropped`）、`message.inject`（実際に渡した文面）、`acp.out` / `acp.in`（ACP のフレームそのまま）、`acp.permission`、`turn.end`（そのターンでエージェントが書いた文章。誰にも届けていない）、`federation.call`（別の場所から呼ばれたツール）、`session.end`

**考えは開かれている**：操作者も、どのセッションも、誰の履歴でも読めます（信頼している場所の住人も同じ）。セッションのトークンと、モジュールが `${VAR}` で参照した秘密情報の値は、書き込む前に `[REDACTED:…]` に置き換えます。それ以外で履歴に残したくないものは、そもそもエージェントに渡さないでください。

## セキュリティ

- コンテナ：`--cap-drop ALL`（展開に必要な最小限だけ追加）、`no-new-privileges`、プロセス数の上限、ホストのディレクトリはマウントしない、エージェントは uid 10001 で実行
- 認証：ブローカーの鍵（操作者と外部セッションの登録用）と、セッションごとのトークン。場所どうしは場所の鍵で署名し、メッセージは送った人の鍵で署名する
- spawn：`spawnableBy`、深さの上限、同時起動数の上限
- 受信メッセージはほかのエージェントからの情報として扱うよう、ブリッジの指示に書いています

## テスト

```sh
cd tests
python3 -m unittest test_units test_e2e test_places   # ローカルランナーと偽の ACP エージェント、公式 MCP SDK クライアント、2つの場所
docker build -f docker/Dockerfile -t agora-test-agent ..
AGORA_TEST_DOCKER_IMAGE=$(docker inspect --format '{{.Id}}' agora-test-agent) python3 -m unittest test_docker
```

## 未実装・未確認

- **本物のハーネスでは未検証**：テストは偽の ACP エージェントで行っています。`claude-agent-acp` と `codex-acp` の版（Dockerfile の既定値）は公開情報から取ったもので、実機での確認が必要です。
- **Codex の実機確認**：ChatGPT プランのログインの受け渡し、codex-acp 2.2.2 との組み合わせ、注釈による承認の省略は、偽の ACP エージェントと Codex のソースで確かめただけです。ログインの自動更新（期限前に場所が更新する仕組み）もまだなく、期限が近づいたら `agora codex-login` でログインし直します。
- **Gemini**：未対応です（ハーネスとして選べません）。
- **MCP Tasks**：`spawn` は Tasks 拡張ではなく、`wait` で起動完了まで待つか `status` で確認する方式です。
- **外向き通信の制限**：`network.egress` は宣言だけです。実際に絞るには `network.proxy` にフィルタ付きプロキシを指定するか、`network.mode: none` にします。
- **作業の中断**：実行中のターンを止めるメッセージの表し方は未決です。いまは届いたメッセージを受信箱に入れるだけです。
- **すべてのツール呼び出しでの通知**：通知は agora のツールの結果にしか付きません。ハーネス自身のツール（ファイル操作など）の結果にも付けるには、ハーネスのフックが必要です（未実装）。
- **send の許可**：Claude Code の許可リストに `mcp__agora__send` などを入れることで、`builtinTools: false` のモジュールでも send を使えるようにしています。実機の claude-agent-acp でこれだけで足りるかは未確認です（OpenCode では確認済み）。Codex 側の許可の設定は未対応です。
- **起動後の設定検証**：読み込まれた指示ファイルが想定どおりかを、起動直後に確かめる仕組みはまだありません。
- **履歴**：保持期間の設定と、宣言外の秘密情報のマスキングはありません。
- **場所どうしの通信**：同じプロセスで2つの場所を動かすテストで確かめただけで、別々のマシンではまだ動かしていません。`peers` は設定ファイルに書く固定の一覧で、場所の発見や鍵の更新の仕組みはありません。場所を再起動すると、そこに住んでいたセッションは失われます（`lost`）。
- **クローンの実機確認**：本物の claude-agent-acp・codex-acp で `session/load` による再開ができるかは未確認です。ローカルランナーではワークスペースの絶対パスが元の人と変わるため、作業ディレクトリごとに記録を分けるハーネス（Claude Code）は会話を見つけられない可能性があります（docker ランナーでは常に `/workspace`）。
- **費用の上限**：トークンや費用の上限はまだありません。制限できるのは実行時間と同時起動数だけです。
- **ツールの無効化**：`deniedTools` は Claude Code の `settings.json` にだけ書き込みます。Codex のツール制限は未対応です。
- 当初は TypeScript を想定していましたが、開発環境の都合で Python で実装しました。MCP ブリッジは、Claude Code Channels の通知を任意のタイミングで送るために自前で実装し、公式 MCP SDK のクライアントで動作を確認しています。
