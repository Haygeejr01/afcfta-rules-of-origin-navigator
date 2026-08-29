# Agent use — disclosure and trajectories

The submission package asks for **"representative trajectories for every agent you
used, easy to follow from the agent instructions through to the final result"**, and
the rules state that **"coding-agent use is required. You must disclose the tools you
used and submit the required trajectories for evaluation."**

Three distinct agents were used. Two of them run *inside the product*; one of them
*built the product*. This file says which is which and where each one's evidence
lives, because they are easy to confuse — both produce files called "trajectories".

## The three agents

| Agent | What it is | Its instructions | Its trajectories |
|---|---|---|---|
| **Coding agent** | Claude Code `2.1.250`, model **Claude Opus 5**. Wrote the source, tests, evaluation harness and documentation in this repository. | The operator's prompts, verbatim, starting with the role/spec prompt at the top of `session-01-…md` | **This directory** — 2 sessions, 456 tool calls |
| **Product workflow agent** | `qwen2.5-coder:7b` on local Ollama, in three node roles: `EXTRACT`, `CLASSIFY_HS`, `GENERATE_REPORT` | [`src/nodes.py:64`](../../src/nodes.py) `EXTRACT_SYSTEM`, [`:187`](../../src/nodes.py) `CLASSIFY_SYSTEM`, [`:702`](../../src/nodes.py) `REPORT_SYSTEM` | [`logs/trajectories/`](../trajectories/) — 6 full runs incl. one refusal |
| **Baseline agent** | The same model, one zero-shot call, no tools, no schema, no deterministic arithmetic. The thing the workflow is measured against. | [`src/baseline.py:48`](../../src/baseline.py) `SYSTEM_PROMPT` | [`logs/baseline_results.json`](../baseline_results.json) — all 10 calls with verbatim `raw_response` |

No other model, agent, or API was used. No cloud inference. No API keys exist in this
project — see CHANGELOG's "Cloud escalation NOT enabled" for the escalation path that
was built and deliberately left switched off.

## What is in this directory

| File | What it is |
|---|---|
| `session-01-8701954b.jsonl` | Raw transcript, **16.9 h**, 16 human instructions, 3 human interrupts, 365 tool calls, 11 tool errors. The substantive build session |
| `session-01-8701954b.md` | The same session rendered readable |
| `session-02-289f5beb.jsonl` | Raw transcript, **1.1 h**, 2 human instructions, 91 tool calls, 1 tool error. A second window that ran *inside* session 01's span |
| `session-02-289f5beb.md` | The same, rendered readable |

**18 human instructions, 456 tool calls and 12 tool errors across the two sessions.**

**Read the `.md` files.** The `.jsonl` files ship so that any line of the digest can be
checked against its source, not because anyone should read 6.5 MB of JSON. The digests
are produced mechanically by [`eval/digest_agent_session.py`](../../eval/digest_agent_session.py)
— **no model summarised anything in them**, which is the same rule the rest of this
project follows about numbers.

Regenerate either one:

```bash
python eval/digest_agent_session.py logs/agent_sessions/session-01-8701954b.jsonl \
    --out logs/agent_sessions/session-01-8701954b.md
```

## What was redacted, and what was deliberately not

These transcripts are the operator's real messages to a coding agent over two days, and
some of them were personal. A public repository is a bad place for that, so a declared
set of content was removed by [`eval/redact_agent_session.py`](../../eval/redact_agent_session.py)
before publishing. **73 redactions across 41 entries**, every one leaving a visible
`[redacted: category]` marker in place:

| Category | Session 01 | Session 02 | What it was |
|---|---|---|---|
| Personal remark | 43 | — | Lines about how the operator felt about the outcome. They shaped no engineering step |
| Pasted third-party claim | 8 | — | An assertion about the contest rules the operator asked the agent to check, and its truncated previews |
| The operator's account handle | 8 | — | Their username on the host platform |
| Pasted third-party evaluation | 7 | — | A scored review of this project the operator obtained elsewhere and pasted in for context |
| Pasted third-party review | 3 | 1 | The same, in prose form |
| Discussion of a target score | 2 | — | See the note below — the one category that is not the operator's own words |
| The host's gated challenge page | 1 | — | The contest page, pasted for reference. Behind a login, so not republished here |
| **Total** | **72** | **1** | |

**The distinction that matters: this redacts, it does not rewrite.** No message was
reworded, no instruction was invented, and nothing was made to look more professional
than it was. The prompts are the operator's actual messages — typos, shorthand and all.
A transcript with its prompts rewritten to read better would be a fabricated trace, and
the rules for this submission ask for integrity by name.

**The one category worth explaining, because it is the agent's own text and not the
operator's.** "Discussion of a target score" is two passages where the agent answers a
question — itself redacted — about what score the project might reach. It came out for a
reason worth stating plainly rather than burying: it exists only because of a removed
question, it is score-optimisation rather than engineering, and leaving the answer while
removing the question would publish the question by implication. The engineering content
it carried is not lost — the actions it listed are the ones that were then actually
performed, and each is recorded on its own in `CHANGELOG.md`. This is the only place
where anything other than the operator's private material was removed, and the count is
published here so the size of it is visible: 2 of 73.

Nothing technical was removed. Not one instruction, correction, error, retry, checkpoint,
timestamp, or piece of ordering. Specifically **left in place**:

- **The agent's own unflattering conclusions**, including a section where it estimates
  this project's score against the published rubric, criterion by criterion, and lists
  what each one loses points for. It is the agent's engineering judgement and it belongs
  in the trace. Removing critical feedback to look better would be exactly the thing the
  paragraph above refuses.
- **Agent replies that discuss the redacted evaluation.** Its substance reaches the trace
  through the agent's own analysis of it — a banned-words list for the video, a refusal to
  backdate commits. Only the pasted block and the operator's private framing of it are
  gone.
- **The operator's Windows username**, which appears in `cwd` on every entry. It is not a
  credential and it is load-bearing: the backtick inside it caused several of the failures
  listed below.

The tool fails closed. It exits non-zero if a rule matches nothing, if any redacted
string survives anywhere in the output, if any of an independent list of probe strings
survives, or if the entry count or JSON validity changed. That last set of checks exists
because **the first five versions of the redactor all leaked.** Copies of the same
sentences survived in the agent's replies, in context summaries that quote the operator
back in *paraphrased* form, and in the output of commands run over the transcript. The
fifth is the most instructive and is the reason the probe list is 26 strings long: the
rules were exact anchors on whole sentences, and a compaction summary had quoted the
*middle* of one — so every rule matched, every rule-survived check passed, and four
fragments went out in the first published commit anyway. A probe list anchored on
sentence openings cannot catch a quotation of a sentence's tail. A redaction that leaves
a copy behind is not a redaction.


**The rule file is deliberately not committed, and that is the point.** An anchor is a
verbatim copy of the sentence it removes, so a published rule list would republish, in one
convenient place, exactly the content the redaction took out. The first version of the
tool declared its anchors inline in the Python source and was about to be pushed that way;
a secret scan over the files going public is what caught it. The real anchors live in
`eval/redaction_rules.local.json`, which is gitignored.
`eval/redaction_rules.example.json` ships instead, with the same structure and placeholder
anchors, so the mechanism is fully inspectable without the content. The counts above are
published in place of the strings.

Verify the shipped files yourself. Re-running the redactor over a file that has already
been processed should find nothing left to remove:

```bash
python eval/redact_agent_session.py logs/agent_sessions/session-01-8701954b.jsonl \
    --out /tmp/check.jsonl --allow-unmatched
#   operator messages altered: 0
#     0  TOTAL redactions
# Verified: no redacted string, pattern, or probe survives; 1,715 entries in, 1,715 out
```

Count the markers in what shipped — 73 across the two transcripts:

```bash
python -c "print(sum(open(f,encoding='utf-8').read().count('[redacted: ') for f in ['logs/agent_sessions/session-01-8701954b.jsonl','logs/agent_sessions/session-02-289f5beb.jsonl']))"
```

## The four things the rubric asks a trajectory to show, and where they are

**"From the agent instructions"** — `session-01-…md` opens with *Human instruction 1*,
which is the full role-and-spec prompt the build started from, reproduced whole. It is
the origin of several decisions defended elsewhere in this repository: "never let the
LLM perform arithmetic or database lookups that a deterministic function should own",
"don't add complexity until we actually hit the problem it solves", and "do NOT build a
frontend now".

**"What the agent did and how its tools responded"** — every tool call appears as
`🔧 Tool — argument`, followed by `↳` and the first lines of what came back.

**"The feedback that shaped its next step"** — the 11 failed tool calls in session 01
are marked **⚠️**. They are the honest part of the trace, and they are not all the same
kind of failure:

| The error | What it changed |
|---|---|
| **Two `pytest` runs exiting 1** with a single `F` | Real test failures during the build. The next tool call in each case is the fix |
| `No task found with ID: bxc5v2joh` — **and the same error again in session 02** | This is the artifact behind CHANGELOG's *"Two eval runs raced each other"*. The task handle for a running evaluation was lost, the agent assumed the process had died, and relaunched it. It had not died. Both runs then competed for the same local model and the latency figures were worthless. The contaminated output is kept as `logs/comparison_results_contended.json` |
| `gh auth git-credential` → `unexpected EOF while looking for matching`` ` `` | A pre-existing defect in the machine's **global** git config: the credential helper pointed at a Temp path containing the operator's backtick username, and git invokes helpers through `sh -c`. It would have broken the first push of *any* repository. Fixing it is why this project has commits |
| `git` exit **128**, `python` `SyntaxError: unterminated string literal`, PowerShell `ScriptBlock should only be specified as a value of the Command parameter`, an `Edit` whose target string did not match | Ordinary malformed commands, retried correctly. Kept because a trace with no botched commands in it would not be a real trace |

**"Retries or human checkpoints"** — 3 **🛑 Human interrupt** markers, where the
operator stopped the agent mid-execution, plus 14 checkpoints where context was restored,
a session was resumed, or a background task reported back. The product's *own* human
checkpoint is a separate thing and lives in [`logs/trajectories/`](../trajectories/): see
`M010-declined-*.json`, which stops at `HUMAN_REVIEW` with six nodes and no report.

## Honest notes about these files

- **The digests omit assistant reasoning blocks** — 211 in session 01, 51 in session 02.
  They are the bulk of the bytes and are not what the rubric asks for. The counts are
  printed in each header so the omission is visible, and every block is in the raw
  `.jsonl` beside it.
- **The instruction count was wrong in an earlier version of this file, and the bug is
  worth knowing about.** It said 9 for session 01 against a true 16. The digest treated
  any message containing `<ide_opened_file>` as machine-generated, but the IDE *appends*
  that to genuine messages — so more than half the operator's instructions were being
  filed as "editor state". Two smaller counting errors came out of the same review: the
  harness writes its own `"Continue from where you left off."` on resuming a session
  (`isMeta: true`, now counted as a checkpoint, not an instruction), and it re-records a
  queued message when a run resumes, so one instruction appeared twice under two uuids.
  Adjacent duplicates are now collapsed and the count of collapsed ones is in the header.
  The numbers in this file are the corrected ones and each is reproducible from the
  `.jsonl` beside it.
- **Session 01's transcript is a snapshot.** It is the session that produced this file,
  so it cannot contain its own ending. The span in its header is the span at snapshot
  time.
- **Session 02 carries little instruction.** Its two human turns are "continue from where
  u stopped" and one request to evaluate the work against the rules; it is a second window
  that mostly relayed background-task notifications while the evaluation ran. It is
  included because it happened, not because it is informative. Session 01 is the
  representative trace.
