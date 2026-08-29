"""Redact personal content from a coding-agent transcript before publishing it.

The submission package requires the coding agent's trajectories, and this repository
publishes them. It does not require publishing the operator's private remarks, and a
public repository is a bad place for them.

So this removes a bounded, declared set of content from the transcript. What it does
NOT touch:

  * any technical instruction, correction, or piece of feedback
  * any error, retry, or human checkpoint
  * timestamps, ordering, session metadata, or the structure of the trace

It applies to **every string in the file**, not only the operator's own messages. That
is deliberate and was found the hard way: the first version redacted operator messages
only, and the verification step then caught surviving copies of the same sentences in
the agent's own replies, in context summaries that quote the operator back, and in the
output of commands run over the transcript. A redaction that leaves a copy behind is
not a redaction, so the pass is recursive over all string values and the run fails if
any anchor survives anywhere.

The distinction that matters: **this redacts, it does not rewrite.** No message is
reworded, no instruction is invented, nothing is made to look more professional than
it was. Every removal leaves a visible marker naming its category, the categories are
declared in `logs/agent_sessions/README.md`, and the counts below are printed on every
run so the total is checkable. A transcript with prompts rewritten to look better
would be a fabricated trace, which is a different thing entirely and not what this is.

Rules are exact anchors, not patterns, so a rule cannot quietly over-match. Any rule
that matches nothing is a hard error -- otherwise a spec silently rots as its source
changes and the operator believes content was removed when it was not.

**The rules live outside this file, and that is not an accident.** An anchor is a
verbatim copy of the sentence it removes, so the first version of this tool -- which
declared them inline right here -- shipped a tidy list of exactly the content it had
taken out of the transcripts. A secret scan over the files going public is what caught
it. `eval/redaction_rules.local.json` holds the real anchors and is not committed;
`eval/redaction_rules.example.json` ships in its place with the same structure and
placeholder anchors, and the category-by-category counts are published in
`logs/agent_sessions/README.md`. That is what a reader needs -- how much was removed, of
what kind, and the assurance that nothing technical was among it.

Usage:
    python eval/redact_agent_session.py <session.jsonl> --out <redacted.jsonl>
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DEFAULT_RULES = Path(__file__).with_name("redaction_rules.local.json")

MARKER = "[redacted: {category} — see logs/agent_sessions/README.md]"

# Base64 payloads (pasted images) can contain any letter sequence by chance, so they
# are collapsed before probing rather than reported as leaks.
_B64_RUN = re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")


class Rules:
    """Loaded redaction spec.

    `literal` entries are (start, end_or_None, category): end None removes exactly the
    start string, end set removes the span start..end inclusive.

    `regex` entries cover content that appears in more than one wording. This is not
    belt-and-braces: the transcripts were compacted several times, and each context
    summary re-states earlier messages in its own words, so an exact anchor matches the
    original and misses the restatement quoting it back.

    `probes` are independent of both. They are strings that must not survive anywhere,
    whichever rule was meant to catch them -- because the rules-survived check is
    circular, and every leak this tool ever had passed it.

    Probe the *middle* of a removed sentence, not its opening. The one leak that got
    past a fully passing run and into a pushed commit did so because all twelve probes
    were opening words: a compaction summary had quoted a sentence from the middle, so
    every rule matched, every probe passed, and four fragments were published anyway.

    Deliberately absent: any rule against the agent's own unflattering conclusions about
    the work, or against names it discusses from a redacted paste. This removes the
    operator's private remarks, not critical engineering feedback.
    """

    def __init__(self, path: Path):
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.path = path
        self.literal: list[tuple[str, str | None, str]] = [
            (r["start"], r.get("end"), r["category"]) for r in raw.get("redactions", [])
        ]
        self.regex: list[tuple[str, str]] = [
            (r["pattern"], r["category"]) for r in raw.get("regex", [])
        ]
        self.probes: tuple[str, ...] = tuple(raw.get("probes", []))
        if not self.literal and not self.regex:
            raise ValueError(f"{path} declares no rules")


def _redact_text(text: str, rules: Rules, counts: dict[str, int]) -> str:
    for start, end, category in rules.literal:
        marker = MARKER.format(category=category)
        while True:
            i = text.find(start)
            if i < 0:
                break
            if end is None:
                j = i + len(start)
            else:
                k = text.find(end, i + len(start))
                if k < 0:
                    break
                j = k + len(end)
            text = text[:i] + marker + text[j:]
            counts[category] = counts.get(category, 0) + 1

    for pattern, category in rules.regex:
        marker = MARKER.format(category=category)
        text, n = re.subn(pattern, marker.replace("\\", r"\\"), text)
        if n:
            counts[category] = counts.get(category, 0) + n
    return text


def _walk(node, rules: Rules, counts: dict[str, int]):
    """Redact every string value in the structure, in place where possible."""
    if isinstance(node, str):
        return _redact_text(node, rules, counts)
    if isinstance(node, list):
        for i, item in enumerate(node):
            node[i] = _walk(item, rules, counts)
        return node
    if isinstance(node, dict):
        for key, value in node.items():
            node[key] = _walk(value, rules, counts)
        return node
    return node


def redact(path: Path, out: Path, rules: Rules) -> tuple[int, dict[str, int]]:
    counts: dict[str, int] = {}
    lines_out: list[str] = []
    touched = 0

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            lines_out.append(line)
            continue

        before = counts.copy()
        entry = _walk(entry, rules, counts)
        if counts != before:
            touched += 1
        lines_out.append(json.dumps(entry, ensure_ascii=False))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    return touched, counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--rules",
        type=Path,
        default=DEFAULT_RULES,
        help=f"rule file (default: {DEFAULT_RULES.name}, which is not committed — "
        f"see redaction_rules.example.json for the format)",
    )
    parser.add_argument(
        "--allow-unmatched",
        action="store_true",
        help="do not fail when a rule matches nothing (use only for a session the rule was not written for)",
    )
    args = parser.parse_args()

    if not args.session.is_file():
        print(f"No such transcript: {args.session}")
        return 1
    if not args.rules.is_file():
        print(
            f"No rule file at {args.rules}.\n"
            f"The real anchors are deliberately not committed — publishing them would\n"
            f"republish the content they remove. Copy {DEFAULT_RULES.with_name('redaction_rules.example.json').name}\n"
            f"to {DEFAULT_RULES.name} and fill in your own."
        )
        return 1

    rules = Rules(args.rules)
    touched, counts = redact(args.session, args.out, rules)

    print(f"{args.session.name} -> {args.out}")
    print(f"  rules: {args.rules.name} "
          f"({len(rules.literal)} literal, {len(rules.regex)} regex, {len(rules.probes)} probes)")
    print(f"  operator messages altered: {touched}")
    total = 0
    for category, n in sorted(counts.items()):
        print(f"  {n:>3}  {category}")
        total += n
    print(f"  {total:>3}  TOTAL redactions")

    # A rule that matches nothing means the spec has drifted from the transcript.
    unmatched = [c for _s, _e, c in rules.literal if c not in counts]
    if unmatched and not args.allow_unmatched:
        print("\nERROR: these rules matched nothing — the spec no longer fits this file:")
        for c in dict.fromkeys(unmatched):
            print(f"  - {c}")
        return 2

    # Nothing that was removed may survive anywhere in the output.
    raw = args.out.read_text(encoding="utf-8")
    leaks = [s for s, _e, _c in rules.literal if s in raw]
    leaks += [p for p, _c in rules.regex if re.search(p, raw)]
    if leaks:
        print("\nERROR: redacted text still present in the output:")
        for s in leaks:
            print(f"  - {s[:70]!r}")
        return 3

    # And the independent probes, which is the check that actually catches paraphrases.
    probed = _B64_RUN.sub("", raw)
    hits = [(p, len(re.findall(re.escape(p), probed, re.I))) for p in rules.probes]
    surviving = [(p, n) for p, n in hits if n]
    if surviving:
        print("\nERROR: probe strings survive — a rule is missing or too narrow:")
        for p, n in surviving:
            print(f"  - {p!r} ×{n}")
        return 4

    # The trace's structure must be untouched: same entries, all still parseable.
    src_lines = [l for l in args.session.read_text(encoding="utf-8").splitlines() if l.strip()]
    out_lines = [l for l in raw.splitlines() if l.strip()]
    if len(src_lines) != len(out_lines):
        print(f"\nERROR: entry count changed — {len(src_lines)} in, {len(out_lines)} out")
        return 5
    for n, line in enumerate(out_lines, 1):
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"\nERROR: line {n} is no longer valid JSON: {exc}")
            return 5

    print(
        f"\nVerified: no redacted string, pattern, or probe survives; "
        f"{len(out_lines):,} entries in, {len(out_lines):,} out, all parseable."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

