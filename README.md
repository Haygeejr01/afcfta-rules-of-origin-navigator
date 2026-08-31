# AfCFTA Rules-of-Origin Compliance Navigator

A CLI tool that helps an African SME manufacturer work out whether their product is
likely to qualify for preferential tariff treatment under the AfCFTA — by parsing
their bill of materials, applying the Regional Value Content rule for their HS code,
and putting a human reviewer in front of the answer before any document is issued.

> **Scope, stated plainly.** This system assesses eligibility **against a controlled
> set of AfCFTA rule data** bundled with the repository. It is **not** an
> authoritative legal determination of origin, not a certificate of origin, and
> carries no official standing. The HS subheadings in the dataset are real HS-2022
> codes; **the RVC thresholds are synthetic, illustrative values**, labelled as such
> in the dataset's own `provenance` field. Any real claim must be confirmed with a
> national customs authority or a licensed customs broker.

**Demo video:** [youtu.be/8y7oUg1b3nU](https://youtu.be/8y7oUg1b3nU) — 4:55, captioned. The
video description carries chapter markers and a written walkthrough of `CHANGELOG.md`.

---

## The user and the bottleneck

**Who:** A small manufacturer or exporter — a chocolate maker in Accra, a knitwear
factory in Athi River, a soap works in Sapele. They have a product and they know
what goes into it: components, where each one came from, what each one cost.

**What they need:** Under AfCFTA, a product only gets preferential tariff treatment
if enough of its value originates in the region. "Enough" is a percentage threshold
that depends on the product's HS code. Meet it and you pay a lower duty; miss it and
you don't.

**Why it's hard today:** Finding out means reading tariff schedules written for
customs professionals, correctly classifying your own product into a six-digit HS
code, locating the rule that governs it, and doing the value arithmetic — or paying a
customs broker for a few hours of their time. For a firm shipping a $10,000 batch,
the cost of finding out can rival the benefit of knowing. So many simply don't claim
the preference they're entitled to.

**What this does:** Reads the manifest as the exporter would actually write it,
identifies the applicable rule from the controlled dataset, computes the regional
value content **deterministically**, and shows a reviewer the numbers *before*
producing anything. Under a minute of compute instead of a broker's invoice.

---

## What existed before this build

**Nothing.** The repository was initialised empty — zero commits — at the start of
this hackathon. We wrote every file here during the build. There is no prior
codebase, no forked project, and no pre-existing dataset. You can check the claim
rather than take it: `git log` on this repository begins at the first commit of this
work.

**Everything below was added in this build:**

| Added | What it is |
|---|---|
| `data/mock_tariff_db.json` | 10 real HS-2022 subheadings with synthetic RVC thresholds (30–55%) |
| `data/synthetic_manifests.json` | 10 exporter manifests in raw unstructured form, each with full known ground truth |
| `data/afcfta_state_parties.json` | Eligible origins, recognised external origins, and an alias map |
| `src/state.py` | Typed state; every model `extra="forbid"` |
| `src/tools.py` | `LOOKUP_RULE`, `CALCULATE_RVC` — pure functions, no LLM, no network |
| `src/llm.py` | Minimal Ollama client, no retry logic |
| `src/nodes.py` | The seven workflow nodes |
| `src/graph.py` | State machine + CLI |
| `src/baseline.py` | Zero-shot control system |
| `eval/run_comparison.py` | Head-to-head evaluation harness |
| `eval/digest_agent_session.py` | Renders a coding-agent transcript into a readable trajectory |
| `eval/redact_agent_session.py` | Removes a declared set of personal content from a transcript before publishing |
| `tests/` | 129 tests, model-free |

---

## Agent use — disclosure

**Coding agent: [Claude Code](https://claude.com/claude-code) `2.1.250`, model Claude
Opus 5.** Every file in this repository was written in a session with it. Nothing else
was used — no second assistant, no code generation service, no copied project.

Its full trajectories are committed, not described: **[`logs/agent_sessions/`](logs/agent_sessions/)**
holds two raw transcripts plus a readable rendering of each. Between them, **18 human
instructions and 456 tool calls over 18 hours, 12 of which failed** — including the two
test failures that drove fixes, and the lost background-task handle that caused the
contaminated evaluation run documented in CHANGELOG. The digests open with the operator's
original role-and-spec prompt, reproduced whole, because that prompt is where several of
the decisions defended in this README come from.

A declared set of the operator's personal remarks and of third-party text they pasted in
for context was removed before publishing — 73 redactions, each leaving a visible marker,
with the categories and counts tabulated in
[`logs/agent_sessions/README.md`](logs/agent_sessions/README.md). **No message was
reworded and no instruction was invented**; the prompts are the real ones, typos and all,
and nothing technical was taken out — including a section where the agent scores this
project against the rubric and says what each criterion loses points for.

**Do not confuse the two kinds of trajectory in this repository.** They have the same
name and different subjects:

| | The agent | Its trajectories |
|---|---|---|
| **Built the product** | Claude Code (Opus 5) | [`logs/agent_sessions/`](logs/agent_sessions/) |
| **Runs inside the product** | `qwen2.5-coder:7b`, in three node roles | [`logs/trajectories/`](logs/trajectories/) |
| **The control it is measured against** | the same model, one zero-shot call | [`logs/baseline_results.json`](logs/baseline_results.json) |

**What the human decided rather than the agent.** The scope (assess, never determine),
the refusal to build a frontend, the model strategy, the rule that no LLM touches
arithmetic or a lookup, and the decision to publish five claims that turned out narrower
than first written — all operator calls, and all traceable to a specific instruction in
`session-01-…md`. [`logs/agent_sessions/README.md`](logs/agent_sessions/README.md) maps
each of the three agents to the exact file and line its instructions live in.

---

## Architecture

```
  raw manifest text
         |
         v
  [EXTRACT]        LLM   -- parse text into typed components (JSON-constrained)
         |
         v
  [CLASSIFY_HS]    LLM   -- pick one code from the controlled shortlist, or decline
         |                  (proposed code is then VALIDATED against the dataset)
         v
  [LOOKUP_RULE]    pure  -- hs_code -> rvc_threshold
         |
         v
  [CALCULATE_RVC]  pure  -- Decimal arithmetic; sets PASS/FAIL/BORDERLINE
         |
         v
  [VERIFY]         pure  -- internal consistency; separates errors from warnings
         |
         v
  [HUMAN_REVIEW]   HUMAN -- execution pauses; state checkpointed to disk
         |                        |
     approve                     edit --> back to LOOKUP_RULE (bounded)
         |                        |
         v                     reject --> stop, no document
  [GENERATE_REPORT] LLM  -- prose only; every figure copied from state
```

### The division of labour is the whole design

| Job | Owner | Why |
|---|---|---|
| Reading messy text | LLM | Genuinely hard for code, genuinely easy for a model |
| Choosing an HS code | LLM, then **validated by code** | The model is good at this (9/10 measured) but cannot be trusted to decline |
| Threshold lookup | **Pure function** | A lookup has one right answer; there is nothing to reason about |
| All arithmetic | **Pure function** | Measured: the model got **0/10** on this |
| Consistency checking | **Pure function** | Protects reviewer attention |
| The decision to issue | **Human** | Not a technical step |
| Writing prose | LLM | Actual language work |

### One model, on purpose

Everything runs on **`qwen2.5-coder:7b`** locally. `qwen3.5:cloud` is wired as an
escalation target but **not enabled** — no node demonstrated a need for it. The 1.5B
model on the same machine is **unused**; no experiment justified introducing a third
model. One model is a defensible architecture when one model does the job, and
component count is not an achievement.

### Structural guarantees, not prompt requests

Four constraints are enforced by code paths rather than asked for in prompts,
because a prompt is a request and a guard is a guarantee:

1. **The LLM cannot produce a number that reaches the report.** `GENERATE_REPORT`
   assembles every numeric field from state in Python. The model's output is confined
   to named prose fields. There is no code path from a model token to a reported
   figure. A regex scan additionally flags any figure the model writes into prose.
   This is verified by test, not by inspection: `tests/test_report_integrity.py` stubs
   the model with a narrative that invents a 99.99% RVC, a 5% threshold, a
   USD 1,000,000 cost base and calls itself a legally binding certificate — and asserts
   the assessment block is unmoved.

2. **The LLM cannot introduce an HS code from outside the dataset.** Whatever
   `CLASSIFY_HS` returns is checked for membership in the rule table; anything absent
   becomes `CLASSIFICATION_UNAVAILABLE` and routes to human review. A well-behaved
   reply and a hallucinated one are treated identically.

   **What this guard does not do — measured, not theorised.** It cannot catch a code
   that is *wrong but present in the table*. On M010 (a solar PV module, correctly
   HS 8541.43, deliberately absent from the dataset) the model proposed `8544.42` —
   insulated electrical conductors, an adjacent rule that **is** in the table. The
   guard passed it through, and the agent returned a confident `FAIL` where the
   correct answer was `NEEDS_HUMAN_REVIEW`. The baseline made the identical error.

   Worse: `VERIFY` raised **no warning at all** on that run. The trajectory log
   records why — the model self-reported a confidence of **1.00** on the wrong
   rule, and the low-confidence trigger fires below 0.50. Nothing automated in this
   system flagged M010.

   And the confidence score is worse than uncalibrated. Across every run where it
   was logged — four trajectories plus their checkpoints, nine records in all — the
   model returned **exactly 1.00, every time**, on the eight correct classifications
   and on the one wrong one alike. The only `0.0` in the logs is the value our own
   code writes when it overrides a code as unavailable. So the trigger can only fire
   on the case the guard has *already* caught, and never on the case it was meant to
   catch. **We shipped a warning path that cannot fire when it matters**, and I only
   know that because I went looking through the logs for the number instead of
   citing the threshold in the source.

   A membership check bounds the *vocabulary* of answers, not their correctness — and
   a self-reported confidence is not evidence of anything.

   What actually defends against this is the human checkpoint — a reviewer seeing
   "solar photovoltaic module" classified under "insulated electrical conductors"
   rejects it in a second. **This is the one case in the evaluation where the human
   step is load-bearing rather than ceremonial**, and I only learned it because we
   had built the dataset with a case that has no right answer available.

3. **No document exists without human approval.** `GENERATE_REPORT` is unreachable
   except through an explicit `approve`. State is checkpointed to disk *before* the
   pause, so the interrupt survives process exit. `tests/test_checkpoint.py` runs the
   whole workflow with the model stubbed and a scripted reviewer, and asserts that
   `reject`, a bare `edit`, and an exhausted edit loop all terminate with **no report
   node ever running** — and that the reviewer's edit re-runs the arithmetic against
   the new threshold rather than relabelling the old result.

4. **The narrative cannot quietly claim legal standing.** This guard exists because
   the prompt-level version of it failed in front of us. The prompt says, in as many
   words, *do not claim the product is legally certified, approved, or cleared by any
   authority*. On the first full approved run of M001 the model wrote:

   > "You can now export your chocolate bars without any issues related to trade
   > regulations."

   Three lines above a disclaimer stating the document is *not a legal determination
   of origin, not a certificate of origin, and carries no authority*. And the
   `limitations` field — the one field that would have counterbalanced it — had been
   generated on every run since the node was written and **never rendered**: asked for
   in the prompt, present in the JSON, dropped on the floor by `render_report`.

   So the claims now get the same structural treatment the figures already had:
   `_scan_for_overclaims` flags affirmative assurances (with a negation guard, so
   *"this is **not** a certificate of origin"* stays legal), the hits are recorded in
   the report metadata and the trace, the node's status drops to `warn`, and a warning
   block is printed **above** the summary — a caveat printed after the claim is not a
   caveat. The offending wording is left visible rather than silently scrubbed, so the
   reviewer sees what the model tried to say. `limitations` renders now.

   **Then we ran the fix against a live model on a different product**, to test it on
   output it had never seen. M008 (Nigerian toilet soap) produced *four* overclaims in
   one narrative — "found to comply with the regulations", "you can export them without
   any issues", "you can continue exporting", "meet the necessary trade compliance
   requirements" — and the first version of the scan caught **one of the four**. The list
   was widened on that evidence, and both real narratives are now pinned as test
   fixtures, so it can only grow against output we have actually observed.

   **Two limits, stated rather than implied.** That M008 run *is* the demonstration of
   the first: it is a phrase list, so like guard 2 it bounds *recognised phrasings*, not
   meaning. And `GENERATE_REPORT` runs **after** the checkpoint, so the reviewer approves
   the figures and never reads the prose — the scan stands in for a second review rather
   than replacing one. Both of those are arguments for a prose-review step, which is in
   "Where this goes next".

---

## Results

Same model on both sides (`qwen2.5-coder:7b`), same 10 manifests, same grading. A case
is correct only if the **HS code**, the **RVC percentage** (±0.5pp) and the **verdict**
are all right.

The baseline is one zero-shot prompt: the raw manifest, no tools, no schema, no
deterministic arithmetic. **The full rule table is pasted into its prompt** — our
thresholds are synthetic, so withholding them would have guaranteed a 0% baseline and
rigged the comparison. It is a fair control, given every fact it needs.

| | Baseline | Structured workflow |
|---|---|---|
| HS code correct | 9/10 | 9/10 |
| **RVC percentage correct (±0.5pp)** | **0/10** | **9/10** |
| Verdict correct | 5/10 | 9/10 |
| **All three correct** | **0/10** | **8/10** |
| Model calls per case | 1 | 2 |
| Avg wall-clock per case | 37.8 s | 97.7 s *(not reproducible — see below)* |
| Marginal API cost | $0.00 | $0.00 |
| **Human work left over per case** | Recompute all 10 by hand | Read one panel; reject 1 of 10 |

**The accuracy rows are reproducible. The latency row is not, and the difference is
measured.** I ran the whole 10-case comparison a second time on the same machine, same
model, same prompts (`logs/comparison_results_run2.json`):

| | Run 1 | Run 2 |
|---|---|---|
| HS / RVC / verdict / all-three correct | 9 / 9 / 9 / 8 | **9 / 9 / 9 / 8** |
| Per-case result payloads identical to run 1 | — | **10 of 10, byte for byte** |
| Mean agent wall-clock per case | 97.7 s | **198.3 s** |

Every graded output is bit-identical across the two runs — not merely the same score, the
same JSON. That is what `temperature=0`, grammar-constrained JSON and deterministic
arithmetic are supposed to buy, and it is now measured rather than asserted. Latency is the
one thing that moved, and it moved uniformly (1.9–2.6× on all ten cases), which is machine
load rather than any property of the workflow.

So read the latency figure as an order of magnitude, not a constant. **The workflow costs
roughly 2.6× the baseline's latency under run 1's conditions and twice the model calls.**
Both run locally, so neither has an API bill; the real price is time and local compute. That
is a genuine trade, not a free win — and the honest version of it is that the *ratio* was
measured once, under one machine state, and I would not defend the second decimal place.

**On that last row, stated carefully.** We did not stopwatch a reviewer, so we are not
quoting minutes we did not measure. What we can show is what each system *leaves for a
human to do*, which follows directly from the accuracy numbers above:

- The baseline got **0/10** RVC figures right and showed its working on **none** of
  them. There is no subset a user could trust and no derivation to audit, so validating
  any single answer means redoing the whole calculation from the manifest by hand — the
  task the tool was supposed to remove. Its 37.8 s is fast at producing something that
  saves no work.
- The workflow prints the extracted bill of materials line by line with each origin
  classified, then the totals and the division that produced the percentage. The
  reviewer's job is bounded: check the transcribed costs against the costing sheet they
  already have, then answer. On **1 of 10** cases (M010) the correct answer is to reject,
  and that is the case a human is genuinely needed for.

The claim is not "we made the human faster by N minutes". It is that we moved the human
from *recomputing the answer* to *checking a transcription* — and we can point at which
case still needs them.

### What the numbers actually say

**HS classification is not where the architecture earns its keep.** Both systems scored
9/10. I expected the structured version to win here and it did not — the model is simply
good at picking a code from a closed list. Claiming classification as the win would have
been false.

**Arithmetic is where it earns its keep, completely.** The baseline got **zero** of ten
RVC percentages right, with errors up to **52.3pp** (M007: said 35.48%, truth 87.80%).
The workflow got nine, exact to two decimal places, because a pure Python function does
the division.

**Verdict accuracy understates the damage.** The baseline's 5/10 correct verdicts all
rest on a wrong number — right answer, wrong reasoning, no way for a user to tell. And
the errors run both ways:

- **M008: said FAIL, truth PASS** (67.32% against a 30% threshold). An exporter abandons
  a preference claim they were entitled to.
- **M004: said FAIL, truth PASS.** Same again.
- **M006: said PASS at 41.86%.** The truth is 34.00% against a 35% threshold — short of
  the rule, inside the borderline band, and therefore a case that needs documentation
  and review. The baseline turned "you are just short, get this checked" into "you are
  fine, file it."

Both directions cost real money.

### Where the workflow still failed

Two of ten, and they are not the same kind of failure:

- **M010 — the classification guard's blind spot.** Covered above: `8544.42 / FAIL`
  instead of `NEEDS_HUMAN_REVIEW`, with no automated warning. Only a human catches it.
- **M007 — extraction drift, not arithmetic.** Reported 89.02% against a truth of
  87.80%. The division was *correct*; one component cost was mis-read out of the
  manifest. The verdict still landed on PASS.

M007 is the honest boundary of the whole design: moving arithmetic into a pure function
eliminates arithmetic error, **not input error**. What it does is convert a fabrication
problem into a transcription problem — narrower, bounded, and checkable by a human
against a costing sheet, because the extracted bill of materials is printed line by line
on the review panel. Narrower is not zero.

---

## Hot take

**The baseline never showed its working, and that is more dangerous than getting the
number wrong.**

Every one of the ten zero-shot responses came back as three bare lines:

```
HS_CODE: 3401.11
RVC_PERCENT: 27.69%
VERDICT: FAIL
```

No derivation. No components listed. No total. `27.69%` carries two decimal places and
reads like the output of a calculation — the truth was `67.32%`, and the correct verdict
was PASS. The figure was not computed and then rounded; it was produced whole.

**The proof that it was never computed is in the log.** Two of the ten cases came back
byte-identical:

```
M002  HS_CODE: 8544.42   RVC_PERCENT: 37.00%   VERDICT: FAIL
M010  HS_CODE: 8544.42   RVC_PERCENT: 37.00%   VERDICT: FAIL
```

M002 is a Moroccan wiring harness whose true RVC is **11.33%**. M010 is a South African
solar photovoltaic module with a different component list, different origins and a
different total cost — and it is not covered by the dataset at all. Nothing about these
two bills of materials is the same. The model produced one number for both because it was
matching the *HS code* to a plausible-looking percentage, not dividing one cost by
another. Whatever it was doing, arithmetic was not involved.

I had asked for two decimal places thinking it would encourage care. It bought **false
precision instead of arithmetic**: the more confident the format looked, the less there
was behind it. A wrong number that announces itself as approximate invites a check. A
wrong number formatted to two decimals invites a filing.

This is why the fix is structural rather than a better prompt. The report's figures are
assembled from state in Python and the model is confined to named prose fields — there
is no code path from a model token to a reported number, and a test proves it by feeding
the model a narrative that invents a 99.99% RVC and asserting the document is unmoved.
You cannot instruct your way out of a failure mode whose symptom is confidence.

---

## Limitations, honestly

- **The RVC thresholds are invented.** Real AfCFTA product-specific rules live in
  Annex 2 Appendix IV and are not reproduced here. The HS codes are real; the
  thresholds are plausible synthetic values.
- **The RVC formula is simplified.** Real RVC works off ex-works price and would count
  domestic labour and overhead as regional value. This uses a materials-only cost
  base, which makes it *conservative* — it understates RVC relative to an ex-works
  basis, erring toward review rather than toward a false PASS. Because the cost base
  is exactly the bill of materials, the build-up and build-down methods are
  algebraically identical here, so the choice of method cannot change a verdict.
- **Ten HS codes, not the full nomenclature.** Anything outside them correctly
  returns `CLASSIFICATION_UNAVAILABLE` rather than a guess — but only if the model
  proposes something outside them. It may instead propose a wrong code that is inside
  the table, which the guard cannot detect (see M010 above).
- **The self-reported confidence score is worthless as a signal.** In every run we
  logged it, the model returned `1.00` — on right answers and on M010's wrong one
  equally. It is presented on the review panel labelled "model self-reported" so a
  reviewer discounts it accordingly, and the low-confidence trigger built on top of it
  has never fired on a case that needed it. Removing it would be defensible; keeping it
  visible and labelled is what we chose, because a reviewer who sees `1.00` beside
  "solar photovoltaic module → insulated electrical conductors" learns something true
  about the model.
- **Origin is taken at face value.** The system does not verify that a component
  claimed as Ghanaian actually originates in Ghana. That is a documentary question.
- **The prose is generated after the human signs off, and the claim scan is a phrase
  list.** The reviewer approves the *figures*; the narrative is written afterwards, so
  nobody reads it before it ships. The overclaim scan (guard 4) is what stands in for
  that missing review, and it recognises the constructions we have actually seen a
  model produce plus close paraphrases — not every possible way of overstating a
  result. It is the same limitation as the HS membership guard, one layer up.
- **No cumulation rules, no de minimis, no tariff-shift criteria.** Real RoO offers
  several routes to qualification; this implements the RVC route only.
- **Single-user CLI.** No auth, no multi-tenancy, no audit trail beyond local logs.

## Where this goes next without a rewrite

The seam is already in the right place. `LOOKUP_RULE` reads a local JSON file behind a
typed interface; pointing it at a real tariff database changes one function. The
origin list is data, not code. `run_pipeline_to_verify` is already separated from the
human step, so a web or API frontend would call the same function and render the same
review panel to a queue of reviewers instead of a terminal. Nothing here assumes a
terminal except the decision provider, which is already an injectable callable.

The two changes with the clearest evidence behind them, in order:

1. **Generate the narrative before the checkpoint and put it on the review panel.**
   Guard 4 exists because prose currently reaches a document nobody read. Moving
   `GENERATE_REPORT` ahead of the pause turns a regex backstop into an actual review —
   the reviewer approves the sentences alongside the figures. It costs a model call on
   rejected runs, which is why it is a change and not a patch.
2. **A description-versus-classification plausibility check.** M010 is the case that
   needs it, and nothing automated caught M010. But it needs its own evidence before it
   earns a model call — and, given guard 2's history, before I would claim it works.

---

## Repository layout

```
data/     mock_tariff_db.json, synthetic_manifests.json, afcfta_state_parties.json
src/      state.py, tools.py, llm.py, nodes.py, graph.py, baseline.py
eval/     run_comparison.py, digest_agent_session.py
tests/    test_tools.py, test_ground_truth.py, test_report_integrity.py,
          test_checkpoint.py, conftest.py
logs/     baseline_results.json          all 10 zero-shot calls, verbatim
          comparison_results.json        per-case grading of both systems
          comparison_results_run2.json   an independent re-run, for variance
          comparison_results_contended.json   a discarded run, kept on purpose
          trajectories/                  6 full runs of the product, incl. one refusal
          agent_sessions/                the coding agent's own trajectories
          checkpoints/                   written before each pause (gitignored:
                                         runtime state, not evidence)
```

Every number quoted in this README comes from a file in `logs/`. **REPRODUCTION.md §8**
maps each claim to the file that backs it.

See **REPRODUCTION.md** for exact commands and **CHANGELOG.md** for the build log.
