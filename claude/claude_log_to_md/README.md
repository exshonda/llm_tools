# claude_log_to_md

Claude Code の会話ログ (`~/.claude/projects/<project>/<session-id>.jsonl`) を、読みやすい Markdown に変換するスクリプト。

Python 3 標準ライブラリのみで動作する(追加パッケージ不要)。

## 使い方

```bash
# 単一ファイル(隣に同名の .md を生成)
python claude_log_to_md.py ~/.claude/projects/<project>/<session-id>.jsonl

# 出力先を指定(単一ファイル時のみ)
python claude_log_to_md.py session.jsonl -o session.md

# プロジェクトフォルダ一括変換
python claude_log_to_md.py ~/.claude/projects/<project>/

# 全プロジェクト一括(各 .jsonl の隣に .md を生成)
python claude_log_to_md.py ~/.claude/projects/

# ツール呼び出しの詳細を省略し、プロンプトと応答だけにする
python claude_log_to_md.py session.jsonl --no-tools
```

Windows のコンソール (cp932) では完了メッセージの `✓` で `UnicodeEncodeError` になることがある(Markdown 自体は出力済み)。`PYTHONIOENCODING=utf-8` を設定して実行すると回避できる。

## 出力内容

### ヘッダ

- Session ID、元ファイル、開始時刻、メッセージ数、作業ディレクトリ、Git ブランチ
- **Model Usage Summary**: モデル別のトークン使用量(ログの値そのまま)

| 列 | 内容 |
|---|---|
| Turns | API 応答数(`message.id` 単位) |
| Input / Output | 入力 / 出力トークン |
| Cache write | キャッシュ書き込みトークン |
| Cache read | キャッシュ読み込みトークン |

- **Estimated Cost (USD)**: 上記トークンを API 単価で換算した推定コスト。Input / Output / Cache write / Cache read の内訳と Total を表示(→ [コスト計算](#コスト計算))

Claude Code は 1 つの API 応答を content ブロックごとに複数行へ分けて記録し、各行に同じ `usage` を書く。集計は `message.id` ごとに 1 回だけ行うため、重複して数えることはない。`--no-tools` の有無でも集計値は変わらない。

### 本文

各メッセージを見出し付きで時系列に出力する。

| 見出し | 内容 |
|---|---|
| 🧑 UserPrompt | ユーザーの入力 |
| 🤖 Assistant (model) · ctx · in/out/cache_w/cache_r | Claude の応答。コンテキストサイズとトークン数を併記(キャッシュ値は元データにある場合のみ) |
| 📤 Tool Result | ツール実行結果(長い結果は 2000 文字で省略) |
| 📋 SessionContinuation | auto-compact 等による会話継続のサマリ |
| ⚡ SlashCommand / 💻 LocalCommand* | スラッシュコマンドやローカルコマンドの入出力 |
| 🔀 Model switched | 途中でモデルが切り替わった位置 |

Thinking とツール結果は `<details>` で折りたたまれる。

`ctx` はその時点のコンテキストサイズで、そのリクエストでモデルに渡した入力トークンの合計 (`input_tokens + cache_creation_input_tokens + cache_read_input_tokens`)。auto-compact が起きると大きく減る。

## コスト計算

`Estimated Cost (USD)` は Anthropic API の公開単価で換算した **推定値**。Pro / Max などのサブスクリプション利用時の実請求額ではない。

- 単価はスクリプト内の `MODEL_PRICING`(USD / 100 万トークン、2026-10 時点)
- キャッシュ書き込み: 入力単価 × 1.25 (TTL 5 分) / × 2 (TTL 1 時間)。ログの `usage.cache_creation` の TTL 別内訳を使い、内訳がない場合は 5 分とみなす
- キャッシュ読み込み: モデルごとの単価(多くは入力単価 × 0.1)
- fast mode (`usage.speed == "fast"`): `FAST_MODE_MULTIPLIER` にあるモデルのみ単価を掛ける
- Haiku 5.5: 1 リクエストの入力が 10 万トークンを超えると単価 5 倍 (`LONG_CONTEXT_PRICING`)

単価表に無いモデル・モードのターンはコストに含めない。そのモデルの行は `—`、合計は `≥$x.xx`(下限値)と表示し、表の下に注記する。

新しいモデルの追加や価格改定があったときは `MODEL_PRICING` を更新すること。
