#!/usr/bin/env python3
"""
Claude Code の会話ログ (~/.claude/projects/*/*.jsonl) を読みやすい Markdown に変換する。

使い方:
    # 単一ファイル
    python claude_log_to_md.py ~/.claude/projects/<project>/<uuid>.jsonl

    # プロジェクトフォルダ一括変換
    python claude_log_to_md.py ~/.claude/projects/<project>/

    # 全プロジェクト一括(各 .jsonl の隣に .md を生成)
    python claude_log_to_md.py ~/.claude/projects/

    # 出力先を指定(単一ファイル時のみ)
    python claude_log_to_md.py session.jsonl -o session.md

    # ツール呼び出し詳細を省略してプロンプトと応答だけにする
    python claude_log_to_md.py session.jsonl --no-tools
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


TRUNCATE_TOOL_RESULT = 2000  # ツール結果の最大表示文字数


def format_timestamp(ts: str) -> str:
    """ISO 8601 タイムスタンプを読みやすい形式に整形"""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts


def shorten_model_name(model: str) -> str:
    """モデル名を読みやすく短縮。

    例:
        claude-opus-4-7-20260201   → opus-4-7
        claude-sonnet-4-6-20250929 → sonnet-4-6
        claude-haiku-4-5-20251001  → haiku-4-5
    """
    if not model:
        return "unknown"
    name = model
    if name.startswith("claude-"):
        name = name[len("claude-"):]
    # 末尾の -YYYYMMDD を除去
    parts = name.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
        name = parts[0]
    return name


# Anthropic API 単価 (USD / 100万トークン)。2026-10 時点の公開価格。
# キー: shorten_model_name() 後のモデル名
# 値: (input, output, cache_read)
#   キャッシュ書き込みは input 単価 × 1.25 (TTL 5分) / × 2 (TTL 1時間)
#   新モデル追加・価格改定時はここを更新する
MODEL_PRICING = {
    "fable-5-1":  (10.00, 50.00, 0.25),
    "mythos-5-1": (10.00, 50.00, 0.25),
    "fable-5":    (10.00, 50.00, 1.00),
    "mythos-5":   (10.00, 50.00, 1.00),
    "opus-5-5":   (4.00, 20.00, 0.20),
    "opus-5":     (5.00, 25.00, 0.50),
    "opus-4-8":   (5.00, 25.00, 0.50),
    "opus-4-7":   (5.00, 25.00, 0.50),
    "opus-4-6":   (5.00, 25.00, 0.50),
    "sonnet-5-5": (2.00, 10.00, 0.20),
    "sonnet-5":   (2.00, 10.00, 0.20),
    "sonnet-4-6": (3.00, 15.00, 0.30),
    "haiku-5-5":  (0.10, 0.50, 0.01),
    "haiku-4-5":  (1.00, 5.00, 0.10),
}
# Haiku 5.5 は 1 リクエストの入力が 100K トークンを超えると単価が 5 倍
LONG_CONTEXT_PRICING = {
    "haiku-5-5": (100_000, 5.0),  # (閾値, 倍率)
}
# fast mode (usage.speed == "fast") の単価倍率。価格が判明しているモデルのみ
FAST_MODE_MULTIPLIER = {
    "opus-5-5": 2.0,
    "opus-5":   2.0,
}
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0


def calc_message_cost(model: str, usage: dict):
    """1 API 応答の API 単価換算コスト (USD) を返す。単価不明なら None"""
    if not isinstance(usage, dict):
        return 0.0
    def tok(d, k):
        v = d.get(k) if isinstance(d, dict) else None
        return int(v) if isinstance(v, (int, float)) else 0

    in_t = tok(usage, "input_tokens")
    out_t = tok(usage, "output_tokens")
    read_t = tok(usage, "cache_read_input_tokens")
    write_t = tok(usage, "cache_creation_input_tokens")
    # トークン 0 の応答 (Claude Code の <synthetic> 等) は単価に関係なく 0
    if in_t + out_t + read_t + write_t == 0:
        return 0.0

    short = shorten_model_name(model)
    price = MODEL_PRICING.get(short)
    if price is None:
        return None
    p_in, p_out, p_read = price
    # TTL 別内訳があれば使う。無ければ API デフォルトの 5 分とみなす
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        w1h = tok(cc, "ephemeral_1h_input_tokens")
        w5m = tok(cc, "ephemeral_5m_input_tokens")
    else:
        w1h, w5m = 0, write_t

    mult = 1.0
    if usage.get("speed") == "fast":
        if short not in FAST_MODE_MULTIPLIER:
            return None
        mult *= FAST_MODE_MULTIPLIER[short]
    if short in LONG_CONTEXT_PRICING:
        threshold, m = LONG_CONTEXT_PRICING[short]
        if in_t + read_t + write_t > threshold:
            mult *= m

    cost = (
        in_t * p_in
        + out_t * p_out
        + read_t * p_read
        + w5m * p_in * CACHE_WRITE_5M_MULTIPLIER
        + w1h * p_in * CACHE_WRITE_1H_MULTIPLIER
    ) / 1_000_000
    return cost * mult


def update_usage_stats(stats: dict, model: str, usage: dict) -> None:
    """モデル別のトークン使用量とコストを累積。stats は in-place 更新"""
    if not model:
        return
    rec = stats.setdefault(model, {
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost": 0.0,
        "unpriced_turns": 0,  # 単価不明で cost に含められなかったターン数
    })
    rec["turns"] += 1
    if isinstance(usage, dict):
        for k in ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"):
            v = usage.get(k)
            if isinstance(v, (int, float)):
                rec[k] += int(v)
    cost = calc_message_cost(model, usage)
    if cost is None:
        rec["unpriced_turns"] += 1
    else:
        rec["cost"] += cost


def render_content(content, include_tools: bool = True) -> str:
    """message.content を Markdown 文字列にレンダリング"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")

        if itype == "text":
            parts.append(item.get("text", ""))

        elif itype == "thinking":
            text = item.get("thinking", "")
            parts.append(f"<details><summary>💭 Thinking</summary>\n\n{text}\n\n</details>")

        elif itype == "tool_use" and include_tools:
            name = item.get("name", "?")
            inp = item.get("input", {})
            inp_str = json.dumps(inp, indent=2, ensure_ascii=False)
            parts.append(
                f"**🔧 Tool Use: `{name}`**\n\n"
                f"```json\n{inp_str}\n```"
            )

        elif itype == "tool_result" and include_tools:
            result = item.get("content", "")
            if isinstance(result, list):
                result = "\n".join(
                    r.get("text", "") if isinstance(r, dict) else str(r)
                    for r in result
                )
            result_str = str(result)
            truncated = ""
            if len(result_str) > TRUNCATE_TOOL_RESULT:
                result_str = result_str[:TRUNCATE_TOOL_RESULT]
                truncated = f"\n...(残り {len(str(result)) - TRUNCATE_TOOL_RESULT} 文字を省略)"
            parts.append(
                f"<details><summary>📤 Tool Result</summary>\n\n"
                f"```\n{result_str}{truncated}\n```\n\n</details>"
            )

    return "\n\n".join(p for p in parts if p)


# role='user' & type='text' のテキストを分類するためのパターン定義
# (prefix, label, icon) — label は Markdown 見出しとgrep用、icon は視認用
# 先頭一致で判定。新しいパターンを追加する場合はここに足すだけでよい。
SYSTEM_INJECTED_USER_PATTERNS = [
    (
        "This session is being continued from a previous conversation",
        "SessionContinuation",
        "📋",
    ),
    (
        "Caveat: The messages below were generated by the user while running local commands",
        "LocalCommandContext",
        "💻",
    ),
    (
        "<command-name>",
        "SlashCommand",
        "⚡",
    ),
    (
        "<local-command-stdout>",
        "LocalCommandOutput",
        "💻",
    ),
]


def classify_user_prompt_text(text: str, entry: dict) -> tuple:
    """ユーザーテキストプロンプトを (label, icon) に分類。

    Claude Code が auto-compact やローカルコマンド連携で role='user' として
    挿入する system 由来テキストを、本物のプロンプトと区別する。
    """
    # 1. JSONL 側のメタフラグ(将来 Claude Code がフィールド追加する可能性に対応)
    if entry.get("isCompactSummary") or entry.get("isMeta"):
        return ("SessionContinuation", "📋")

    # 2. テキスト先頭パターンで判定
    stripped = text.lstrip() if isinstance(text, str) else ""
    for prefix, label, icon in SYSTEM_INJECTED_USER_PATTERNS:
        if stripped.startswith(prefix):
            return (label, icon)

    # 3. デフォルトは本物のユーザープロンプト
    return ("UserPrompt", "🧑")


def classify_user_content(content) -> str:
    """role='user' のメッセージを 'prompt' / 'tool_result' / 'mixed' に分類。

    Claude Code の JSONL では、ツール実行結果は API 仕様上
    role='user' & content type='tool_result' として記録される。
    これを本物のユーザープロンプト (type='text') と区別する。
    """
    if isinstance(content, str):
        return "prompt"
    if not isinstance(content, list):
        return "prompt"
    has_text = False
    has_tool_result = False
    for item in content:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "text":
            has_text = True
        elif t == "tool_result":
            has_tool_result = True
    if has_tool_result and not has_text:
        return "tool_result"
    if has_tool_result and has_text:
        return "mixed"
    return "prompt"


def render_tool_results_only(content) -> str:
    """content から tool_result ブロックだけを抽出してレンダリング"""
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_result":
            continue
        tool_use_id = item.get("tool_use_id", "")
        is_error = item.get("is_error", False)
        result = item.get("content", "")
        if isinstance(result, list):
            result = "\n".join(
                r.get("text", "") if isinstance(r, dict) else str(r)
                for r in result
            )
        result_str = str(result)
        truncated = ""
        if len(result_str) > TRUNCATE_TOOL_RESULT:
            result_str = result_str[:TRUNCATE_TOOL_RESULT]
            truncated = f"\n...(残り {len(str(result)) - TRUNCATE_TOOL_RESULT} 文字を省略)"
        status = " ❌ **ERROR**" if is_error else ""
        header = f"**Tool Use ID**: `{tool_use_id}`{status}" if tool_use_id else status
        body = f"```\n{result_str}{truncated}\n```"
        if header:
            parts.append(
                f"<details><summary>📤 Result{status}</summary>\n\n"
                f"{header}\n\n{body}\n\n</details>"
            )
        else:
            parts.append(f"<details><summary>📤 Result</summary>\n\n{body}\n\n</details>")
    return "\n\n".join(parts)


def render_user_text_only(content) -> str:
    """content から text ブロックだけを抽出(mixed メッセージ用)"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n\n".join(
        item.get("text", "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )


def convert_jsonl_to_md(jsonl_path: Path, output_path: Path = None,
                       include_tools: bool = True) -> Path:
    """JSONL ファイル 1 つを Markdown に変換"""
    if output_path is None:
        output_path = jsonl_path.with_suffix(".md")

    sections = []
    session_id = None
    first_ts = None
    cwd = None
    git_branch = None
    msg_count = 0
    # モデル追跡
    model_stats = {}        # {model_name: {turns, input_tokens, output_tokens, ...}}
    last_model = None       # 直前の assistant ターンのモデル(切替検出用)
    counted_msg_ids = set() # 集計済みの message.id(重複集計防止)

    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # メタ情報を収集
            if session_id is None:
                session_id = entry.get("sessionId")
            if cwd is None:
                cwd = entry.get("cwd")
            if git_branch is None:
                git_branch = entry.get("gitBranch")
            ts = entry.get("timestamp")
            if first_ts is None and ts:
                first_ts = ts

            msg = entry.get("message", {})
            if not isinstance(msg, dict):
                continue
            role = msg.get("role") or entry.get("type", "system")
            content = msg.get("content", "")
            ts_str = format_timestamp(ts)

            # role='user' は本物のユーザー入力とツール実行結果が混在する。分類して別見出しにする。
            if role == "user":
                cls = classify_user_content(content)

                if cls == "tool_result":
                    # ツール結果のみ → "Tool Result" 見出しで出力
                    if not include_tools:
                        continue
                    body = render_tool_results_only(content)
                    if not body.strip():
                        continue
                    sections.append(
                        f"## 📤 Tool Result — {ts_str}\n\n{body}\n"
                    )
                    msg_count += 1
                    continue

                if cls == "mixed":
                    # ユーザー発言 + ツール結果が同一メッセージに同居(稀)
                    # → ユーザー発言部分と Tool Result 部分を別セクションに分けて出力
                    user_text = render_user_text_only(content)
                    if user_text.strip():
                        label, icon = classify_user_prompt_text(user_text, entry)
                        sections.append(
                            f"## {icon} {label} — {ts_str}\n\n{user_text}\n"
                        )
                        msg_count += 1
                    if include_tools:
                        tr_body = render_tool_results_only(content)
                        if tr_body.strip():
                            sections.append(
                                f"## 📤 Tool Result — {ts_str}\n\n{tr_body}\n"
                            )
                    continue

                # 通常のユーザープロンプト(system 注入テキストの可能性もあるので分類)
                rendered = render_content(content, include_tools=include_tools)
                if not rendered.strip():
                    continue
                # text プロンプトの中身を抽出して分類
                user_text = render_user_text_only(content) if isinstance(content, list) else str(content)
                label, icon = classify_user_prompt_text(user_text or rendered, entry)
                sections.append(f"## {icon} {label} — {ts_str}\n\n{rendered}\n")
                msg_count += 1
                continue

            # role='assistant' のトークン集計は表示有無(--no-tools 等)に関係なく行う。
            # 1 つの API 応答が content ブロックごとに複数エントリへ分割され、
            # 同じ usage が重複記録されるため、message.id 単位で 1 回だけ集計する
            if role == "assistant":
                model = msg.get("model") or entry.get("model") or ""
                usage = msg.get("usage") or {}
                msg_id = msg.get("id")
                if not msg_id or msg_id not in counted_msg_ids:
                    if msg_id:
                        counted_msg_ids.add(msg_id)
                    update_usage_stats(model_stats, model, usage)

            # role='assistant' またはその他(system等)
            rendered = render_content(content, include_tools=include_tools)
            if not rendered.strip():
                continue

            msg_count += 1

            if role == "assistant":
                short = shorten_model_name(model) if model else "unknown"

                # モデル切替を検出してマーカー挿入
                if model and last_model is not None and model != last_model:
                    prev_short = shorten_model_name(last_model)
                    sections.append(
                        f"> 🔀 **Model switched**: `{prev_short}` → `{short}`\n"
                    )
                if model:
                    last_model = model

                # 見出しにモデル名と入出力・キャッシュトークンを併記
                # (キャッシュ値は元データに存在する場合のみ表示)
                tok_str = ""
                in_t = usage.get("input_tokens")
                out_t = usage.get("output_tokens")
                if isinstance(in_t, int) or isinstance(out_t, int):
                    tok_str = f" · in={in_t or 0}/out={out_t or 0}"
                cw_t = usage.get("cache_creation_input_tokens")
                cr_t = usage.get("cache_read_input_tokens")
                if isinstance(cw_t, int):
                    tok_str += f"/cache_w={cw_t}"
                if isinstance(cr_t, int):
                    tok_str += f"/cache_r={cr_t}"
                if tok_str and not tok_str.startswith(" · "):
                    tok_str = " · " + tok_str.lstrip("/")
                sections.append(
                    f"## 🤖 Assistant ({short}){tok_str} — {ts_str}\n\n{rendered}\n"
                )
            else:
                icon = "ℹ️"
                sections.append(
                    f"## {icon} {role.capitalize()} — {ts_str}\n\n{rendered}\n"
                )

    header_lines = [
        f"# Claude Code Session Log",
        "",
        f"- **Session ID**: `{session_id or 'unknown'}`",
        f"- **Source file**: `{jsonl_path}`",
        f"- **Started**: {format_timestamp(first_ts)}",
        f"- **Messages**: {msg_count}",
    ]
    if cwd:
        header_lines.append(f"- **Working dir**: `{cwd}`")
    if git_branch:
        header_lines.append(f"- **Git branch**: `{git_branch}`")

    # モデル使用サマリ
    if model_stats:
        header_lines.append("")
        header_lines.append("## Model Usage Summary")
        header_lines.append("")
        header_lines.append(
            "| Model | Turns | Input | Output | Cache create | Cache read | Cost (USD) |"
        )
        header_lines.append(
            "|---|---:|---:|---:|---:|---:|---:|"
        )

        def fmt_cost(cost: float, turns: int, unpriced: int) -> str:
            # 全ターン単価不明なら "—"、一部不明なら下限値であることを示す
            if turns and unpriced == turns:
                return "—"
            s = f"${cost:,.2f}"
            return f"≥{s}" if unpriced else s

        # ターン数の多い順にソート
        for model, rec in sorted(model_stats.items(),
                                 key=lambda x: -x[1]["turns"]):
            short = shorten_model_name(model)
            header_lines.append(
                f"| `{short}` | {rec['turns']} "
                f"| {rec['input_tokens']:,} "
                f"| {rec['output_tokens']:,} "
                f"| {rec['cache_creation_input_tokens']:,} "
                f"| {rec['cache_read_input_tokens']:,} "
                f"| {fmt_cost(rec['cost'], rec['turns'], rec['unpriced_turns'])} |"
            )
        # 合計行
        total_turns = sum(r["turns"] for r in model_stats.values())
        total_in = sum(r["input_tokens"] for r in model_stats.values())
        total_out = sum(r["output_tokens"] for r in model_stats.values())
        total_cc = sum(r["cache_creation_input_tokens"] for r in model_stats.values())
        total_cr = sum(r["cache_read_input_tokens"] for r in model_stats.values())
        total_cost = sum(r["cost"] for r in model_stats.values())
        total_unpriced = sum(r["unpriced_turns"] for r in model_stats.values())
        header_lines.append(
            f"| **Total** | **{total_turns}** "
            f"| **{total_in:,}** | **{total_out:,}** "
            f"| **{total_cc:,}** | **{total_cr:,}** "
            f"| **{fmt_cost(total_cost, total_turns, total_unpriced)}** |"
        )
        header_lines.append("")
        header_lines.append(
            "> Cost は API 単価で換算した推定値です(Pro/Max 等のサブスクリプション利用時の実請求額ではありません)。"
        )
        if total_unpriced:
            header_lines.append(
                f"> 単価表に無いモデル/モードの {total_unpriced} ターンは Cost に含まれていません(`≥` / `—`)。"
            )

    header_lines.extend(["", "---", ""])
    header = "\n".join(header_lines)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(sections))

    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Claude Code の JSONL ログを Markdown に変換"
    )
    parser.add_argument("path", help="JSONLファイル または ディレクトリのパス")
    parser.add_argument("-o", "--output", help="出力先(単一ファイル時のみ)")
    parser.add_argument(
        "--no-tools",
        action="store_true",
        help="ツール呼び出しの詳細を省略(プロンプト/応答テキストのみ)",
    )
    args = parser.parse_args()

    target = Path(args.path).expanduser().resolve()
    include_tools = not args.no_tools

    if target.is_file():
        out_path = Path(args.output).expanduser() if args.output else None
        result = convert_jsonl_to_md(target, out_path, include_tools=include_tools)
        print(f"✓ {result}")
    elif target.is_dir():
        files = sorted(target.rglob("*.jsonl"))
        if not files:
            print(f"JSONL ファイルが見つかりません: {target}", file=sys.stderr)
            sys.exit(1)
        print(f"{len(files)} 件の JSONL を変換します...")
        for jf in files:
            try:
                result = convert_jsonl_to_md(jf, include_tools=include_tools)
                print(f"  ✓ {result}")
            except Exception as e:
                print(f"  ✗ {jf}: {e}", file=sys.stderr)
    else:
        print(f"パスが存在しません: {target}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
