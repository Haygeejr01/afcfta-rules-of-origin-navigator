# REPRODUCTION

Exact commands for a clean environment. Windows paths shown; the POSIX equivalents
are noted where they differ.

## 0. Prerequisites

- **Python 3.11+** (developed and measured on 3.14.7)
- **[Ollama](https://ollama.com)** running locally
- ~5 GB free disk for the model
- No API key. No account. No network calls to any third party — the model runs on
  your machine and the rule data is a local JSON file.

## 1. Install the model

```bash
ollama pull qwen2.5-coder:7b
ollama serve          # if not already running as a service
```

Confirm it is reachable:

```bash
ollama list
```

> **Note on the model.** The build brief specified Qwen 2.5 7B *instruct*. Only the
> **coder** variant was available on the build machine, and pulling the instruct
> variant metered at 387 KB/s with a 3h11m ETA — not affordable inside the build
> window. All reported results are from `qwen2.5-coder:7b`. See CHANGELOG.
>
> To run against a different model instead, set `NAVIGATOR_LOCAL_MODEL` (below).
> No code change is required.

## 2. Set up the environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Environment variables (all optional)

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama is listening |
| `NAVIGATOR_LOCAL_MODEL` | `qwen2.5-coder:7b` | The model used for all three LLM nodes |
| `NAVIGATOR_CLOUD_MODEL` | `qwen3.5:cloud` | Escalation target. **Not used by default** — no node demonstrated a need for it |
| `NAVIGATOR_TIMEOUT_S` | `300` | Per-request timeout |

Nothing needs to be set for a default run.

## 4. Verify the deterministic core first

This needs **no model and no network**. Run it before anything else — if this fails,
nothing downstream is trustworthy.

```bash
python -m pytest tests/ -q
```

**Expected:** `129 passed` in about two seconds.

This covers the RVC arithmetic, the rule lookup, origin resolution, the borderline
band, a re-derivation of every ground-truth figure in the dataset from its own bill of
materials, the report's figure isolation, the report's claim scanning, and the human
checkpoint's control flow. The last three stub the model out entirely: the report tests
feed it a deliberately hostile narrative (fabricated percentages, fabricated costs, a
contradicted verdict) and assert the assessment block is unmoved; they also feed it the
**verbatim overclaiming sentence from a real trajectory log** and assert it is flagged
above the prose rather than shipped silently; and the checkpoint tests run the whole
workflow with a scripted reviewer and assert no input can reach `GENERATE_REPORT`
without an approve.

## 5. Run the baseline

```bash
python src/baseline.py
```

Single manifest instead:
```bash
python src/baseline.py --manifest M005
```

**Expected output shape:**
```
Baseline: qwen2.5-coder:7b | 10 manifest(s) | zero-shot, no tools

  M001 ... 1806.32 | 47.5% | PASS | MISMATCH | 45.3s
  ...
```

**Measured runtime:** ~**378 s total**, ~**37.8 s per case**, **1 model call per case**.
**Measured cost:** **$0.00** — local inference, no API billing. The real cost is
latency and local compute.

Writes `logs/baseline_results.json`.

## 6. Run the full workflow (this is where the human interrupt fires)

```bash
python src/graph.py --manifest M005
```

Available manifests:
```bash
python src/graph.py --list
```

The run prints each node as it completes, then **stops** and waits for you:

```
  HUMAN REVIEW REQUIRED — execution is paused
  ...
  RVC             : 51.00%  vs threshold 50.00%   (+1.00 pp)
                    ^ inside the +/-2.0pp borderline band
  STATUS          : BORDERLINE

  [approve] issue the draft report with these findings
  [reject]  stop; do not issue a report
  [edit]    correct the HS code, then recalculate

  Your decision (approve/reject/edit):
```

Type `approve`, `reject`, or `edit` and press Enter. **Nothing is generated until you
answer** — `GENERATE_REPORT` is unreachable except through an explicit `approve`.

State is written to `logs/checkpoints/<id>.checkpoint.json` *before* the pause, so
the interrupt survives process exit.

Your own manifest, from a file or a pipe:
```bash
python src/graph.py --file my_manifest.txt
cat my_manifest.txt | python src/graph.py --stdin
```

Each run writes a full trajectory log to `logs/trajectories/`.

### Four runs worth doing (they exercise the distinct paths)

| Command | What it demonstrates |
|---|---|
| `python src/graph.py --manifest M001 --tag pass` | Clear **PASS** (79.41% vs 40%) |
| `python src/graph.py --manifest M009 --tag fail` | Clear **FAIL** (10.66% vs 45%) |
| `python src/graph.py --manifest M005 --tag borderline` | **BORDERLINE** (51.00% vs 50%) — answer `approve` to see the borderline warning carried into the report |
| `python src/graph.py --manifest M010 --tag declined` | **The most informative run.** Answer `reject`. See below — this is the case the automated guards miss |

**What M010 actually does, and why it is in this table.** M010 is a solar photovoltaic
module. Its correct classification is HS 8541.43, which is **deliberately absent** from
the controlled dataset, so the correct outcome is `CLASSIFICATION_UNAVAILABLE` and a
referral to a human.

That is not what happens. The model proposes `8544.42` — insulated electrical
conductors, an adjacent rule that **is** in the table — so the membership guard passes it
through, and you will watch the run print a confident `8544.42 / 12.31% / FAIL` with a
self-reported confidence of `1.00` and **zero** verification warnings. Nothing automated
catches it. The only thing that does is you, reading "photovoltaic module" beside
"insulated electric conductors" on the panel and typing `reject`.

Run this one. It is the case where the human checkpoint stops being a compliance
formality and starts being the actual control. See README's "Structural guarantees" and
CHANGELOG's "The HS guard is narrower than I had claimed".

### Regenerating the trajectory logs without a terminal

The committed trajectories in `logs/trajectories/` were produced with `--decision`,
which answers the checkpoint from the command line so the logs are reproducible by
anyone:

```bash
python src/graph.py --manifest M001 --tag pass       --decision approve
python src/graph.py --manifest M009 --tag fail       --decision approve
python src/graph.py --manifest M005 --tag borderline --decision approve
python src/graph.py --manifest M010 --tag declined   --decision reject
```

Those four commands produce the four **tagged** logs in the repository. The first three
approve and therefore each write a draft report; **M010 rejects and produces no report
file at all**, which is the point of including it. Two further untagged logs
(`M001-20260829-142355.json` and `M008-20260829-151947.json`) are also committed —
ordinary interactive runs, kept because of what they caught. See §8.

**Why there is no `edit` trajectory, stated plainly.** The reviewer's edit path is real
and bounded, but no manifest in this dataset offers a *legitimate* edit to demonstrate.
The agent classified 9 of 10 correctly, so editing any of those would mean introducing an
error; and on M010 the correct code (8541.43) is not in the dataset by design, so the only
edit available there is the adjacent wrong code the guard exists to catch. Rather than
ship a demonstration that teaches the wrong lesson, the edit loop is proven by test:
`tests/test_checkpoint.py::test_an_edit_triggers_a_genuine_recalculation` asserts that an
edit produces a **second** `CALCULATE_RVC` event against the new threshold rather than
relabelling the first result, and a companion test asserts the loop is bounded at
`MAX_EDIT_ROUNDS`.

You can still see the edit guard refuse an out-of-dataset code, which takes seconds and
needs no model:

```bash
python src/graph.py --manifest M001 --decision edit --edit-hs-code 9999.99
```

It exits with the list of codes the controlled dataset actually contains. A reviewer
cannot introduce a code the rule table does not have — the same restriction the
interactive path enforces.

The panel still renders, state is still checkpointed before the decision, and
`GENERATE_REPORT` is still reachable only through an explicit approve. What changes is
**who answers** — and the flag writes that into `human_notes`, so a trajectory produced
this way says `SCRIPTED DECISION ... This was not a human sign-off` on its face. The
interactive prompt is the default and the only path a real user takes.

## 7. Run the comparison

```bash
python eval/run_comparison.py
```

To reuse the baseline results already on disk rather than re-running them:
```bash
python eval/run_comparison.py --reuse-baseline
```

**Runtime:** the agent side makes 2 model calls per case, so budget roughly
**double the baseline's per-case latency**. Full 10-case comparison from cold:
~15–20 minutes. With `--reuse-baseline`: ~8–10 minutes.

**Cost:** **$0.00** for both systems. Both run on local Ollama; there is no API
charge to report. The workflow makes *more* model calls than the baseline
(2/case vs 1/case) — that is a genuine cost of the architecture, not a hidden
advantage.

> **Why an interactive run prints `Model calls this run: 3` while the table says 2.**
> The two are different pipelines and both numbers are correct. The evaluation harness
> stops at `VERIFY` — `EXTRACT` + `CLASSIFY_HS`, **2 calls** — because it deliberately
> does not simulate the human, so no accuracy figure is credited to a scripted
> approval. A full run that you *approve* continues to `GENERATE_REPORT`, which is the
> third call. **The reported 2/case is the cost of everything that is graded**; the
> third call buys the narrative, which is graded by nothing.

Writes `logs/comparison_results.json` and prints a per-manifest table plus a
scorecard.

### Reproducing the run twice — which figures survive and which do not

**Do not point `--out` at `logs/comparison_results.json` for a second run.** That is the
file every published figure in the README cites; overwriting it destroys the evidence for
the numbers in the documentation. Write somewhere else:

```bash
python eval/run_comparison.py --reuse-baseline --out logs/comparison_results_run2.json
```

That is exactly how `logs/comparison_results_run2.json` was produced, and the two files
together are the reproducibility claim. Check them against each other in one command:

```bash
python - <<'PY'
import json
A = {r["manifest_id"]: r for r in json.load(open("logs/comparison_results.json", encoding="utf-8"))["rows"]}
B = {r["manifest_id"]: r for r in json.load(open("logs/comparison_results_run2.json", encoding="utf-8"))["rows"]}
same = sum(A[m]["agent_result"] == B[m]["agent_result"] for m in A)
print(f"identical agent result payloads: {same}/{len(A)}")
for m in sorted(A):
    print(f"  {m}  {A[m]['agent_seconds']:6.1f}s -> {B[m]['agent_seconds']:6.1f}s"
          f"   ({B[m]['agent_seconds']/A[m]['agent_seconds']:.2f}x)")
PY
```

Expected: **`identical agent result payloads: 10/10`**, and every latency slower by
1.9–2.6×.

| | Run 1 | Run 2 |
|---|---|---|
| HS / RVC / verdict / all-three | 9 / 9 / 9 / 8 | **9 / 9 / 9 / 8** |
| Per-case payloads matching run 1 | — | **10 / 10, byte for byte** |
| Mean agent seconds per case | 97.7 | **198.3** |
| Total model calls | 20 | 20 |

**The accuracy is reproducible; the wall-clock is not.** The graded outputs are not merely
equal in score — the result payloads are byte-identical, which is the behaviour
`temperature=0`, `format: "json"` and deterministic downstream arithmetic are supposed to
produce. Latency doubled uniformly across all ten cases with no reordering, which is
machine load, not workload. Treat any latency number in this repository as an order of
magnitude measured under one machine state; see CHANGELOG's *"Ran the comparison a second
time"*, which is the third time this project has had to correct a latency claim and
explains why the README now prints both columns.

## 8. Agent trajectories — where the evidence lives

**Three agents were used, and all three have their execution recorded.** Two run inside
the product; one built it. They are easy to confuse because both produce files called
"trajectories", so:

| | The agent | Where its trajectories are |
|---|---|---|
| **Built the product** | Claude Code `2.1.250` (Claude Opus 5) | `logs/agent_sessions/` — 2 raw transcripts + a readable rendering of each |
| **Runs inside the product** | `qwen2.5-coder:7b`, in the `EXTRACT` / `CLASSIFY_HS` / `GENERATE_REPORT` node roles | `logs/trajectories/` — 6 full runs |
| **The control it is measured against** | the same model, one zero-shot call, no tools | `logs/baseline_results.json` |

`logs/agent_sessions/README.md` is the disclosure document: it names each agent, points
at the exact file and line holding its instructions, and explains what the digests omit.
**Start there**, then read `session-01-8701954b.md` — it opens with the operator's full
role-and-spec prompt and runs through 365 tool calls to the finished repository.

The digests are produced mechanically, and you can regenerate either one:

```bash
python eval/digest_agent_session.py logs/agent_sessions/session-01-8701954b.jsonl \
    --out logs/agent_sessions/session-01-8701954b.md
```

No model summarises anything in them — the same rule this project applies to numbers.
Counts come straight out of the transcript, so the header is checkable:

```bash
python -c "import json; L=[json.loads(l) for l in open('logs/agent_sessions/session-01-8701954b.jsonl',encoding='utf-8') if l.strip()]; import collections; print(collections.Counter(e.get('type') for e in L))"
```

Verify the **16 human instructions** in session 01's header, which is the count that took
four attempts to get right — the digest's docstring says why:

```bash
python -c "import sys,json,collections; sys.path.insert(0,'eval'); from digest_agent_session import _blocks,_strip_injected,_SYNTHETIC_MARKERS,_INTERRUPT_MARKER as I; L=[json.loads(l) for l in open('logs/agent_sessions/session-01-8701954b.jsonl',encoding='utf-8') if l.strip()]; t=[' '.join(_strip_injected('\n'.join(b.get('text','') for b in _blocks(e) if b.get('type')=='text')).split()) for e in L if e.get('type')=='user' and not e.get('isMeta') and not any(b.get('type')=='tool_result' for b in _blocks(e))]; t=[x for x in t if x and not x.startswith(I) and not any(m in x for m in _SYNTHETIC_MARKERS)]; d=[x for i,x in enumerate(t) if i==0 or x!=t[i-1]]; print('instructions:',len(d),' adjacent duplicates collapsed:',len(t)-len(d))"
```

### The transcripts were redacted before publishing, and here is how to check that

The operator's messages over two days included personal remarks, their account handle on
the host platform, and third-party text they pasted in for context. A declared set of it
was removed by `eval/redact_agent_session.py` — **73 redactions across 41 entries**, each
leaving a visible `[redacted: category]` marker. The categories, per-session counts, and
the list of what was deliberately *left in* are in `logs/agent_sessions/README.md`.

**Nothing technical was removed, and nothing was rewritten.** No message was reworded and
no instruction invented; the prompts are the operator's real messages. The redaction is
mechanical and its integrity checks are in the tool, so running it against the shipped
files is itself the verification — a clean file yields **0 redactions and no surviving
probe**, because there is nothing left to find:

```bash
python eval/redact_agent_session.py logs/agent_sessions/session-01-8701954b.jsonl \
    --out /tmp/check.jsonl --allow-unmatched
#   operator messages altered: 0
#     0  TOTAL redactions
# Verified: no redacted string, pattern, or probe survives; 1,715 entries in, 1,715 out
```

Drop `--allow-unmatched` and it exits 2 instead, reporting that every rule matched
nothing — which is the correct complaint about a file that has already been processed,
and the check that stops a rule from silently rotting as its source changes.

Count the markers in what shipped:

```bash
python -c "print(sum(open(f,encoding='utf-8').read().count('[redacted: ') for f in ['logs/agent_sessions/session-01-8701954b.jsonl','logs/agent_sessions/session-02-289f5beb.jsonl']))"
# 73
```

### The product's own trajectories

Every claim in README.md and CHANGELOG.md about the *product* traces back to one of
these files.

| File | System | What it contains |
|---|---|---|
| `logs/trajectories/M001-pass-*.json` | Workflow | PASS, approved → report issued. 7 nodes |
| `logs/trajectories/M009-fail-*.json` | Workflow | FAIL, approved → report issued. 7 nodes |
| `logs/trajectories/M005-borderline-*.json` | Workflow | BORDERLINE, approved → report carries the warning. 7 nodes |
| `logs/trajectories/M010-declined-*.json` | Workflow | Rejected → **6 nodes, no `GENERATE_REPORT`, no report** |
| `logs/trajectories/M001-20260829-142355.json` | Workflow | An untagged interactive run, kept deliberately: **this is the log that caught the overclaim.** Its narrative reads "You can now export your chocolate bars without any issues related to trade regulations." See below |
| `logs/trajectories/M008-20260829-151947.json` | Workflow | The **verification** run for that fix, on a different product. Kept because it shows the fix working *and* failing: the guard fired on a phrasing it had never seen, and let three others through. Its recorded flag count is `1`; the current code finds `4`. See below |
| `logs/baseline_results.json` | Baseline | All 10 zero-shot calls with the **verbatim `raw_response`**, token counts and latency |
| `logs/comparison_results.json` | Both | Per-case grading of both systems against ground truth |
| `logs/comparison_results_run2.json` | Both | An independent second run of the same comparison — see §7 on variance |
| `logs/comparison_results_contended.json` | Both | A discarded run, kept deliberately — see CHANGELOG "Two eval runs raced each other" |

The first four are the demonstration set, reproducible with the four `--decision`
commands above. The last two are ordinary interactive runs kept because of what they
recorded, and **both are cited by name in `tests/test_report_integrity.py`** — each test
fixture is that file's `narrative` object copied field for field. You can read the sentence
that motivated the claim scan, and watch the scan catch it, without running a model:

```bash
python -c "import sys,json; sys.path.insert(0,'src'); import nodes; n=json.load(open('logs/trajectories/M001-20260829-142355.json',encoding='utf-8'))['final_state']['final_output']['narrative']; print(n['what_this_means']); print(); [print(' FLAG:',h) for h in nodes._scan_for_overclaims(n)]"
```

Note the fourth line of that file's narrative too. The model wrote a perfectly good
`limitations` sentence — *"does not constitute a legal determination"* — and the
renderer threw it away on every run until this was found. The caveat existed; nobody
could read it.

### The M008 log records the guard being incomplete, and that is why it is here

After fixing the overclaim I ran the whole workflow again end to end on a different
manifest — M008, a Nigerian soap exporter — to check the fix against output it had never
seen. It fired on `"without any issues"`, a phrasing the model reached in a new sentence.
It also missed three more overclaims in the same four-sentence narrative.

The log was written **before** the patterns were widened, so the file on disk still carries
the flag count from that moment. Re-scanning the same narrative with the code as it stands
now gives a different answer, and the gap between the two numbers is the whole story:

```bash
python -c "import sys,json; sys.path.insert(0,'src'); import nodes; d=json.load(open('logs/trajectories/M008-20260829-151947.json',encoding='utf-8')); n=d['final_state']['final_output']['narrative']; print('recorded in the log        :', len(d['final_state']['final_output']['metadata']['claims_flagged_in_prose'])); print('rescanned with current code:', len(nodes._scan_for_overclaims(n)))"
```

**Prints `1` and `4`.** Three of the four patterns in the scan exist because that run
produced language nobody had thought to look for — "found to comply with the regulations",
"you can continue exporting", "meet the necessary trade compliance requirements". None of
them were imagined; each was observed and then pinned as a test fixture.

Read that as the honest statement of a limitation rather than a fixed bug. A phrase list
bounds the phrasings it recognises, not the claims a model can make. The one run I used to
verify the fix found three gaps in it, which is a fair estimate of how many remain.

Each workflow trajectory records the raw input, every node in order with its duration,
each deterministic tool call and its arguments, the model call count, the human decision
with its notes, and the complete final state.

**The checkpoint is visible in the logs, not just asserted in prose.** Compare the
`trace` arrays: the three approved runs contain seven nodes ending in `GENERATE_REPORT`;
M010 contains six and stops at `HUMAN_REVIEW`, with `final_output` null. You can check
that in one command:

```bash
python -c "import json,glob; [print(f[-34:], '->', [e['node'] for e in json.load(open(f,encoding='utf-8'))['trace']][-1]) for f in sorted(glob.glob('logs/trajectories/*.json'))]"
```

**The baseline's trajectory is its raw text.** It makes one call and has no tools, no
state and no nodes, so `raw_response` *is* the trajectory. Reading those ten strings is
how the hot take was found — they are three bare lines each, with no derivation behind
the percentage:

```bash
python -c "import json; [print(r['manifest_id'], '|', r['raw_response'].replace(chr(10),' / ')) for r in json.load(open('logs/baseline_results.json',encoding='utf-8'))['results']]"
```

## 9. Timing notes (measured, not estimated)

Measured on a Windows 11 machine running the 7B model on local hardware, across the six
committed trajectory logs plus the two evaluation runs. Yours will differ; the *relative*
comparison between the two systems is what the evaluation is about.

| | Model calls | Measured |
|---|---|---|
| Baseline, per case | 1 | **37.8 s** avg over 10 cases |
| **Graded workflow pipeline** (`EXTRACT` → `VERIFY`) | 2 | **97.7 s** avg over 10 cases — this is the figure in the README's results table |
| Full run you **approve** (adds `GENERATE_REPORT`) | 3 | **158 s – 339 s** wall clock across the five approved trajectories |
| Full run you **reject** | 2 | **85 s** (M010) |

**Budget three to five minutes for an interactive run, not ninety seconds.** The 97.7 s
figure is honest but it measures the *graded* pipeline, which stops at the human
checkpoint. Adding the narrative call roughly doubles it.

Per-node latency, min–max across the six trajectories:

| Node | Range | Notes |
|---|---|---|
| `EXTRACT` | **68–169 s** | Consistently the most expensive call: longest prompt, longest structured output |
| `CLASSIFY_HS` | **14–82 s** | Short output, but the shortlist makes the prompt large |
| `GENERATE_REPORT` | **69–88 s** | Only runs on approve |
| `LOOKUP_RULE`, `CALCULATE_RVC`, `VERIFY` | **0.0 s** | Pure Python. They appear as `0.0s` in the logs because they are single-digit milliseconds |

**Same node, same machine, same model, 2.5× spread.** `CLASSIFY_HS` ran 14 s on one run
and 82 s on another; `EXTRACT` 68 s and 169 s. That is memory pressure and model
eviction on a laptop, not workload — nothing in the prompt changed. It is also why the
one thing this project measured carelessly was latency: see CHANGELOG's "Two eval runs
raced each other". **Do not run anything else against Ollama while timing.**

- **Cold start is worse than a single number suggests.** A first trivial call after
  `ollama serve` returns in ~25 s, but the first *real* run of the workflow after a cold
  start took **339 s** — `EXTRACT` alone was 169 s. Warm the model with a throwaway run
  before you time anything or record anything.

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No module named pytest` | The virtualenv is not active, so `python` resolved to your system install. This is the most common way to trip on step 4. | `.\.venv\Scripts\Activate.ps1` (POSIX: `source .venv/bin/activate`) — your prompt should show `(.venv)`. Or skip activation and call it directly: `.\.venv\Scripts\python.exe -m pytest tests/ -q` |
| `Activate.ps1 cannot be loaded` | PowerShell execution policy | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate. Or use the direct-interpreter form above, which needs no policy change |
| `Cannot reach Ollama` | Ollama not running | `ollama serve` |
| Very slow first call | Cold model load | Expected; second call is faster |
| `No manifest with id ...` | Wrong id | `python src/graph.py --list` |
| Tests fail before any model runs | Data file edited without updating ground truth | The failing assertion names the manifest — that is the guard working |
