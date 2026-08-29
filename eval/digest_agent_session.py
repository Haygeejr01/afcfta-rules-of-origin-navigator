"""Turn a raw coding-agent session transcript into a followable walkthrough.

The submission package asks for trajectories "easy to follow from the agent
instructions through to the final result", showing "what the agent did and how
its tools responded" plus "the feedback that shaped its next step, and any
retries or human checkpoints".

A 4.8 MB JSONL file satisfies the first requirement in letter and none of it in
practice, so this renders the same file as markdown. It is a lossless-by-reference
view: nothing here is summarised by a model, every line is mechanically derived
from the raw file, and the raw file ships beside it so any line can be checked.

Two things are dropped rather than shown, both deliberately:

  * assistant reasoning blocks -- the bulk of the bytes, and not what the rubric
    asks for. Their presence is counted in the header so the omission is visible.
  * long tool results -- truncated to the first lines, which is where the
    feedback that changed the next step actually lives.

Usage:
    python eval/digest_agent_session.py <session.jsonl> --out <digest.md>
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

# A human instruction is the input that steers everything after it, so these are
# kept whole -- the opening prompt IS the agent's instruction set, and truncating it
# would drop the one thing the submission package explicitly asks to see. Tool output
# is evidence of a response, so a few lines is enough.
HUMAN_CHARS = 12000
ASSISTANT_CHARS = 700
RESULT_CHARS = 400
RESULT_LINES = 6

# Injected context, not something the operator typed. Recorded as a checkpoint
# with its payload dropped, because the payload is a file the repository already has.
# These identify an entry that is synthetic *in its entirety*.
_SYNTHETIC_MARKERS = (
    "This session is being continued from a previous conversation",
    "<command-name>",
    "<task-notification>",
)

# These are wrappers the harness *appends to* a genuine operator message -- the IDE
# reporting which file is open, or a system reminder. Treating a message as synthetic
# because it carries one of these was a real bug: it silently reclassified more than
# half of the operator's instructions in session 01 as "editor state" and under-reported
# the human instruction count as 9 against a true 16. Strip the wrapper, keep what the
# human actually typed.
_INJECTED_WRAPPERS = ("system-reminder", "ide_opened_file", "ide_selection")

# A real human action rather than an instruction: the operator stopped the agent
# mid-execution. Counted separately because it is the clearest evidence of a human
# checkpoint inside the coding loop, which the submission package asks for by name.
_INTERRUPT_MARKER = "[Request interrupted by user"


def _strip_injected(text: str) -> str:
    """Remove harness-appended wrappers, leaving only what the operator typed."""
    for tag in _INJECTED_WRAPPERS:
        text = re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL)
        text = re.sub(rf"<{tag}>.*", "", text, flags=re.DOTALL)  # unclosed tail
    return text.strip()


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n*[… {len(text) - limit:,} more characters in the raw transcript]*"


def _first_lines(text: str, lines: int, chars: int) -> str:
    text = (text or "").strip()
    if not text:
        return "*(empty)*"
    kept = text.splitlines()[:lines]
    out = "\n".join(kept)
    truncated = len(text.splitlines()) > lines or len(out) > chars
    return out[:chars].rstrip() + (" …" if truncated else "")


def _blocks(entry: dict) -> list[dict]:
    """Normalise message content to a list of typed blocks."""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _tool_signature(block: dict) -> str:
    """One line describing what the agent asked a tool to do."""
    name = block.get("name", "?")
    args = block.get("input") or {}
    for key in ("file_path", "path", "pattern", "command", "url", "notebook_path"):
        if key in args and isinstance(args[key], str):
            value = " ".join(args[key].split())
            if len(value) > 160:
                value = value[:160].rstrip() + " …"
            return f"`{name}` — {value}"
    if "description" in args and isinstance(args["description"], str):
        return f"`{name}` — {args['description']}"
    return f"`{name}`"


def _result_text(block: dict) -> tuple[str, bool]:
    """Extract a tool result's text and whether it reported an error."""
    is_error = bool(block.get("is_error"))
    content = block.get("content")
    if isinstance(content, str):
        return content, is_error
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts), is_error
    return "", is_error


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def digest(path: Path) -> str:
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Resolve tool_use_id -> tool name so results can be attributed to a call
    # even though they arrive in a later entry.
    tool_names: dict[str, str] = {}
    for entry in entries:
        for block in _blocks(entry):
            if block.get("type") == "tool_use" and block.get("id"):
                tool_names[block["id"]] = block.get("name", "?")

    body: list[str] = []
    tool_counter: Counter[str] = Counter()
    human_turns = 0
    assistant_turns = 0
    thinking_blocks = 0
    error_results = 0
    checkpoints = 0
    interrupts = 0
    duplicates = 0
    last_human: str | None = None
    stamps: list[datetime] = []
    meta: dict[str, str] = {}

    for entry in entries:
        stamp = _parse_ts(entry.get("timestamp"))
        if stamp:
            stamps.append(stamp)
        for key in ("cwd", "gitBranch", "version", "sessionId"):
            if entry.get(key) and key not in meta:
                meta[key] = str(entry[key])

        kind = entry.get("type")

        if kind == "user":
            blocks = _blocks(entry)
            results = [b for b in blocks if b.get("type") == "tool_result"]
            if results:
                for block in results:
                    text, is_error = _result_text(block)
                    name = tool_names.get(block.get("tool_use_id", ""), "tool")
                    if is_error:
                        error_results += 1
                        body.append(
                            f"  - ⚠️ **`{name}` reported an error** — this is the feedback that "
                            f"changed the next step:\n\n    ```\n    {_first_lines(text, RESULT_LINES, RESULT_CHARS)}\n    ```\n"
                        )
                    else:
                        body.append(
                            f"  - ↳ `{name}` responded:\n\n    ```\n    {_first_lines(text, RESULT_LINES, RESULT_CHARS)}\n    ```\n"
                        )
                continue

            text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            if not text:
                continue
            if text.startswith(_INTERRUPT_MARKER):
                interrupts += 1
                body.append(
                    "\n### 🛑 Human interrupt\n\nThe operator stopped the agent mid-execution. "
                    "The next instruction is the correction that followed.\n"
                )
                continue
            # `isMeta` marks a user turn the harness wrote itself. In these transcripts
            # it is always "Continue from where you left off.", emitted on resuming a
            # session -- which is a checkpoint, not something the operator typed.
            # Counting it as an instruction inflated the total by one per session.
            if entry.get("isMeta"):
                checkpoints += 1
                body.append(
                    "\n### ⟲ Checkpoint — session resumed\n\n"
                    "*(the harness's own resume prompt, not an operator instruction)*\n"
                )
                continue
            if any(marker in text for marker in _SYNTHETIC_MARKERS):
                checkpoints += 1
                if "ran out of context" in text:
                    label = "context restored after the session ran out of window"
                elif "<task-notification>" in text:
                    label = "background task reported back to the agent"
                else:
                    label = "injected context / editor state"
                body.append(f"\n### ⟲ Checkpoint — {label}\n\n*(payload omitted; it is a file already in this repository)*\n")
                continue

            # The harness appends editor state and system reminders to real operator
            # messages. Strip those, then judge what the human actually typed.
            typed = _strip_injected(text)
            if not typed:
                checkpoints += 1
                body.append(
                    "\n### ⟲ Checkpoint — injected context / editor state\n\n"
                    "*(payload omitted; it is a file already in this repository)*\n"
                )
                continue
            # The harness re-records a queued message when a run is resumed, so the same
            # instruction can appear twice in a row under different uuids. Collapsing
            # only *adjacent* duplicates catches that without swallowing a genuine repeat
            # ("continue" typed twice at different points is two real instructions).
            normalised = " ".join(typed.split())
            if normalised == last_human:
                duplicates += 1
                continue
            last_human = normalised
            human_turns += 1
            body.append(f"\n### 🧑 Human instruction {human_turns}\n\n{_clip(typed, HUMAN_CHARS)}\n")

        elif kind == "assistant":
            blocks = _blocks(entry)
            spoke = False
            for block in blocks:
                btype = block.get("type")
                if btype == "thinking":
                    thinking_blocks += 1
                elif btype == "text" and block.get("text", "").strip():
                    if not spoke:
                        assistant_turns += 1
                        spoke = True
                    body.append(f"\n**🤖 Agent:** {_clip(block['text'], ASSISTANT_CHARS)}\n")
                elif btype == "tool_use":
                    tool_counter[block.get("name", "?")] += 1
                    body.append(f"  - 🔧 {_tool_signature(block)}")

    stamps.sort()
    start, end = (stamps[0], stamps[-1]) if stamps else (None, None)
    duration = f"{(end - start).total_seconds() / 3600:.1f} h" if start and end else "unknown"

    header = [
        f"# Coding-agent trajectory — `{path.name}`",
        "",
        "Mechanically rendered from the raw transcript by "
        "[`eval/digest_agent_session.py`](../../eval/digest_agent_session.py). "
        "No model summarised anything here; every line below is derived from the JSONL "
        f"file beside it (`{path.name}`), which ships in full so any line can be checked.",
        "",
        "| | |",
        "|---|---|",
        f"| Session id | `{meta.get('sessionId', 'n/a')}` |",
        f"| Agent | Claude Code (`{meta.get('version', 'n/a')}`), model **Claude Opus 5** |",
        f"| Working directory | `{meta.get('cwd', 'n/a')}` |",
        f"| Git branch | `{meta.get('gitBranch', 'n/a')}` |",
        f"| Wall-clock span | {start.isoformat() if start else '?'} → {end.isoformat() if end else '?'} (**{duration}**) |",
        f"| Human instructions | **{human_turns}** |",
        f"| Human interrupts (operator stopped the agent mid-run) | **{interrupts}** |",
        f"| Agent replies | {assistant_turns} |",
        f"| Tool calls | **{sum(tool_counter.values())}** |",
        f"| Tool calls that returned an error | **{error_results}** — the retry signal |",
        f"| Context checkpoints / background task reports | {checkpoints} |",
        f"| Adjacent duplicate instructions collapsed | {duplicates} |",
        f"| Reasoning blocks (omitted below, present in the raw file) | {thinking_blocks} |",
        "",
        "**Tool usage:** " + (", ".join(f"`{n}` ×{c}" for n, c in tool_counter.most_common()) or "none"),
        "",
        "---",
        "",
    ]
    return "\n".join(header) + "\n".join(body) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if not args.session.is_file():
        print(f"No such transcript: {args.session}")
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(digest(args.session), encoding="utf-8")
    kb = args.out.stat().st_size / 1024
    print(f"{args.session.name} -> {args.out}  ({kb:,.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
