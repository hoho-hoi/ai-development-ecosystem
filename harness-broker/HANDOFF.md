# 引き継ぎメモ（harness-broker）

Claude アプリのセッションで作った v0.1 を、Claude Code で続けるためのメモです。

## 目的

ハーネス（Claude Code、Codex など）を抽象化し、次のことを MCP 経由でできるようにする。

- プロファイルからセッションを起動する
- セッション同士で送受信する

プロファイルから生まれるセッションは隔離コンテナで動かし、ホストやプロジェクトの設定・skill を読ませない（再現性のため）。実行履歴は送信原文まで残す。

仕様書（ドラフト。v0.4 で一から書き直した）：https://claude.ai/code/artifact/8941d9d5-a033-44a9-8457-579be8a8a514

## 現状

- 実装は `src/hbroker/` にあり、構成と使い方は README.md のとおり。
- テストは `cd tests && python3 -m unittest test_units test_e2e` で、60 件すべて通る（v0.5 時点）。
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
- **配送の方式**：v0.3 で作り直した（下記）。
- **履歴の閲覧権限**：操作者はすべて読める。セッションが読めるのは、自分、自分が起動したセッション、自分が参加したスレッドだけ。
- **待ち受け先**：docker ランナーでは、ブローカーは 127.0.0.1 と `hb-net` のゲートウェイ IP の両方で待ち受ける。

## v0.2 で追加したこと（2026-10-17）

- **モジュール**：`module.json` を持つ、ハーネスに依存しない定義を、プロファイルの `module`（`repo` + `ref` + `path`、または開発用の `local`）で参照できるようにした。コミットごとに `<state_dir>/modules/` へ一度だけ取得する。取得元とファイルのハッシュを `session.module` イベントに記録する。
- **runtime の新しい項目**：`messageFormat`（`envelope` / `body`）、`sessionMode`（`persistent` / `per_message`）、`brokerTools`、`permissions.deniedTools`。
- **`send` の `wait`**：v0.3 で廃止（`receive` に置き換え）。
- **既存の試験用モジュールを移行**：akinator（branch `task/harness-broker`）と romaji-converter（同）。プロファイルの例は `profiles/examples/akinator*.yaml`、`romaji-converter.yaml`。偽の ACP エージェントで、ローカル参照とコミット固定の両方の通しの確認を済ませた。
- 履歴のビューワーは、ユーザーから既存のビューワーを受け取ってから対応する。

## v0.5 で変えたこと（2026-10-19）：Codex を ChatGPT プランで動かす

ユーザーの依頼：GPT のサブスクリプションで実際に検証したい。Plus・Pro ではアクセストークン（`CODEX_ACCESS_TOKEN`、`at-…`）は作れない（Business・Enterprise のみ）ため、ChatGPT ログイン（`auth.json`）を使う。

- **調べたこと**（Codex のソース 8c07694、codex-acp 2.2.2 のソース）：リフレッシュトークンは1回限り（再使用すると `refresh_token_reused`）。Codex が更新するのは、アクセストークンの期限まで5分を切ったとき（JWT の `exp`）。アクセストークンはおよそ10日有効。注釈のない MCP ツールは既定（Auto）で承認が要る。codex-acp は `NO_BROWSER` でブラウザログインを隠し、`INITIAL_AGENT_MODE` で承認と sandbox の組を選ぶ（既定の `agent` は Codex 自身の自動レビュー）。ウェブ検索の既定は `cached`（有効）。
- **作ったもの**：`logins.py`（ブローカーのログインを読み、リフレッシュトークンを空にした写しを作る。期限が `maxRuntimeMinutes` + 30分より短ければ起動しない）、`runtime.login: chatgpt`、設定 `codex_login_dir`、`hb codex-login [--status] [--device-auth]`、Codex の `config.toml` に `cli_auth_credentials_store = "file"` と（deny なら）`web_search = "disabled"`、環境変数 `NO_BROWSER=1` と `INITIAL_AGENT_MODE`（deny → read-only、それ以外 → workspace-write）。収集した記録から `auth.json` を消す。トークンは履歴で伏せる。ブリッジのツールに MCP 注釈を付けた（send・receive・spawn は破壊的でない・外に出ない、terminate は破壊的、問い合わせは読み取りのみ）。
- `images/codex/Dockerfile` の codex-acp を 2.2.2 にした（以前の 1.10.0 は誤り）。Codex 用のプロファイル例は `profiles/examples/codex/`（id は Claude 版と同じなので、どちらかを `profiles_dir` に置く）。
- 未確認：実機での一連の動作（ここからは npm と Docker Hub に届かず、イメージを作れない）。model 名（例は `gpt-5.5`。プランで選べるものに合わせる）。ログインの自動更新は未実装（curie の例のように、ブローカーが `codex app-server` の `account/read {refreshToken: true}` で更新する案がある）。
- テスト：`test_units` と `test_e2e` で 60 件、`test_docker` 2 件が通る。

## v0.4 で変えたこと（2026-10-19）：最後の文章に意味を持たせない

ユーザーの指摘：返事を求めないメッセージもあるので、最終応答を特別扱いすべきではない。akinator や romaji も本来 send で送り合う想定で、`ecosystem_send` のような接頭辞も要らない。元の仕様書は捨てて一から書き直す。

- **自動のメッセージを全廃**：v0.3 の「最後の文章を返信にする規則」、空のときと終了時の通知メッセージ、`origin` をなくした。ブローカーは自分からメッセージを作らない。最後の文章は `turn.end` に記録するだけ。終了時に残った未読は捨てて `message.dropped` に残す。
- **相手が黙ったことは receive で分かる**：`receive(from=X)` は、X が終了したか、管理下セッションの X が待機中で未読もないとき、待たずに空で返る（`from_quiet`、`from_state`）。X が待機・終了に移るとき、X を待っている receive を起こす（`Session.watching`）。
- **返信は宛先を省ける**：`send(reply_to=m)` は m の送信元に届く。m が自分宛てでなければ 400。
- **`brokerTools` を一覧に**：`[send]` などで渡すツールを選ぶ。ブリッジは `HB_TOOLS` で一覧を絞り、デーモンもトークンごとに 403 で拒否する。Claude Code の `settings.json` には `mcp__hb__<ツール>` を許可として書き込む（`permissions.mode: deny` でも send が使えるように）。
- **見出しだけの形式に**：`messageFormat` を廃止。管理下セッションには `from:`、`message_id:`、`thread_id:`（返信なら `in_reply_to:`）の見出しと本文を渡す。接頭辞（`[harness-broker]`）も末尾の注記もなくした。
- テストの偽エージェントは、実際のエージェントと同じく、ブリッジを常駐させて `send(reply_to=…)` で返す。
- akinator と romaji-converter のモジュールを「受け取ったメッセージへの返信として send で送る」に戻し（前回、最後の文章で答える形にしたのは読み違い）、プロファイルを新しいコミット（akinator e776597、romaji 802842a）に固定した。指示に send があるときだけ send で返す偽エージェントで、両方の通しを確認した。古い指示のコミットでは、akinator が 0.3 秒で「返信せずにターンを終えた」と止まることも確かめた。

## v0.3 で変えたこと（2026-10-19）：通信を send と receive だけにした（v0.4 で一部を撤回）

ユーザーとの議論で「最終応答は本質的な概念ではない。機能は少なく本質的なものに揃える」と決め、仕様書に「通信モデル」の章を足してから作り直した。

- **受信箱を1つに**：管理下・外部・操作者のどのセッションも、メッセージ表（`messages`）が受信箱。未読は `receive` で取り出すか、管理下セッションならターンの終わりにブローカーが最も古い未読を次のプロンプトにする（ターンを終える＝無期限の receive）。キューは廃止。
- **`receive(from?, reply_to?, wait?)`**：`inbox` を置き換え。管理下セッションもターン中に呼べる。1回の待ちは最大 600 秒。
- **廃止**：`inbox`、`send` の `wait` と `auto_reply`、ブリッジの piggyback（`HB_PIGGYBACK`）。
- **通知**：`receive` 以外のツールの HTTP 応答に `X-HB-Unread`（件数と送信元）を付け、ブリッジが結果の後ろに1行足す。本文は渡さず既読にもしない。
- **最終応答の翻訳規則**（`sessions.py` の `_turn_boundary`）：返信していなければ最後の文章を返信として送る、返信していれば履歴だけ、空なら通知。**返信で始まったターンは何も自動で送らない**（自動の返信が往復し続けるのを防ぐ。仕様書の規則に足した条件）。
- **最後の文章**：ACP の `tool_call` 更新で区切り、最後の空でない区間を使う（ツール呼び出し前の「確認します」などは返信にしない）。
- **`origin`**：メッセージに `send` / `final` / `notice` を記録し、`receive` の結果にも出す。v0.2 の索引には列を自動で足す。
- **`spawn` の結果に `task_message_id`**：最初のタスクへの返事を `receive(reply_to=…)` で待てる。
- **終了時**：未読を破棄し（`message.deliver` の via=`dropped`）、返事を待っている送信元に通知する。外部セッションの終了でも同じ。
- akinator の `run_case.py` と romaji-converter の `run_examples.py` を send → receive(reply_to) に移した。偽の ACP エージェント（役を演じるもの）で、実際のブローカーを通して akinator の正解・上限の2ケースと romaji の例を通した。
- テスト：`test_units` 27 件、`test_e2e` 25 件、`test_docker` 1 件がすべて通る。

## 次にやること（優先順）

1. **実機での確認**：`images/claude-code/Dockerfile` をビルドし、`profiles/examples/romaji-converter.yaml` の image を書き換えて、romaji-converter の `evaluations/run_examples.py` を通す（安い）。次に akinator の `evaluations/run_case.py` を上限を小さくして通す。キーは `ANTHROPIC_API_KEY`（だめなら `CLAUDE_CODE_OAUTH_TOKEN`）。あわせて `claude-agent-acp` の版と挙動、`deniedTools` が効くかを確認する。
2. **外部の Claude Code**：`hb mcp --channel` で、Channels 経由で受信できるか確認する。
3. **Codex（ChatGPT プラン）**：README の「Codex を ChatGPT プランで動かす」の手順で、romaji-converter → akinator の順に実機で通す。最初に見るのは、codex-acp が起動して `session/new` が通るか、モデルが send で返すか、send に承認が求められないか。
4. **リポジトリへの組み込み**：このリポジトリの `Makefile` と CI に harness-broker のテストを入れる。CONTRIBUTING により `make test` が必須。
5. **通信モデルの残り**（仕様書の未決事項）：作業を止めるメッセージの表し方、ハーネスのフックでの通知（すべてのツール呼び出しの切れ目）、通知に載せる内容。実機では、モデルが send で返すか（最後の文章で答えて終わらないか）と、`mcp__hb__send` の許可だけで claude-agent-acp が確認なしに send を使えるかを最初に見る。
6. **残りの未実装**：MCP Tasks、外向き通信の制限（egress プロキシ）、Gemini への組み込み、起動後の設定検証（README の「未実装・未確認」も参照）。

このリポジトリの開発ルールは `.cursor/rules/*.mdc` と CONTRIBUTING.md にある（役割分担と TDD）。
