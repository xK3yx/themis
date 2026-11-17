# Themis

An autonomous research agent for parameter-efficient fine-tuning (LoRA-family) of small LLMs.
It searches papers, extracts claims, maps conflicting evidence, preregisters hypotheses, runs
bounded experiments, and writes a report in which **every sentence resolves to evidence**.

The point of the project is an auditable research process, not a chatbot. When in doubt,
choose the option that makes Themis's reasoning more traceable.

## Non-negotiable invariants

1. **No claim without provenance.** Every ledger node is one of:
   - `cited`: from a paper; must reference a stored source span or table cell
   - `measured`: from a Themis run; must reference a run with `n_seeds >= 3`
   - `derived`: Themis's inference; must reference >= 2 parent nodes
   - `prior`: unsourced model assertion; never admissible in conclusions
   Enforce in the data layer (DB constraints + DAO checks + tests), never only in prompts.
2. **Ledger is append-only.** Corrections are new nodes with a `supersedes` edge. No UPDATE
   or DELETE on node payloads.
3. **Node IDs are content hashes** (sha256 of canonical JSON + schema version). Re-ingesting
   the same input must be idempotent.
4. **Extraction never paraphrases.** Store structured fields plus exact source offsets or
   table coordinates. Prose is generated only at report time.
5. **Numbers come from tables first.** Parse results tables from arXiv LaTeX source; the LLM
   labels rows/columns and reads regime details. Numbers taken from prose get lower confidence.
6. **Preregister before running.** A hypothesis's prediction, analysis, and success threshold
   are hashed into the ledger before its experiment starts. The analysis may not change afterwards.
7. **Critic has veto.** Conclusions reach the report only after an adversarial review pass,
   using a different model family than the one that drew the conclusion.
8. **Report contract.** The report renderer rejects any sentence with zero supporting nodes.

## Runtime LLMs: free cloud tiers only

- No paid APIs and no local LLMs at runtime. The local GPU is used only for experiments.
- Code calls **roles**, never model names. `config/providers.yaml` maps each role to an
  ordered list of provider/model fallbacks:
  - `extractor`: long-context model for reading papers
  - `reasoner`: hypotheses, conflict adjudication, report drafting
  - `critic`: must be a different model family from `reasoner`
  - `embedder`: embeddings for dedupe and retrieval
- All providers go through one OpenAI-compatible client in `src/themis/router/`, with
  per-role fallbacks, retry with backoff on 429, and quota exhaustion treated as a normal state.
- **Never hardcode rate limits or model IDs in code.** Free tiers and free model lists change
  constantly; keep them in config and learn limits from response headers and 429s.
- **Every LLM call is cached** by `sha256(model_id + prompt + schema_version)`. Cache hit rate
  is the budget.
- **Daily caps are expected.** Every pipeline stage must be resumable from the ledger, so a
  run can stop when quota runs out and continue tomorrow without redoing work.
- Free tiers may train on prompts. Only send public paper content and Themis's own outputs.
  API keys live in `.env`, never in code or config files.

## Stack

- Python 3.11, `uv` for env and deps, `src/` layout, package `themis`
- SQLite for the ledger; content-addressed blob store at `data/store/<sha256[:2]>/<sha256>`
- `pydantic` v2 for all schemas, `httpx` for HTTP, `typer` for CLI
- Experiments: PyTorch + PEFT, run locally (RTX 4060 Laptop, 8GB VRAM) or on Kaggle
- Tests: `pytest`; lint: `ruff`

## Commands

```
uv sync --extra dev        # install
uv run pytest              # tests
uv run ruff check . --fix  # lint
uv run themis --help       # CLI
```

## Layout

```
src/themis/
  ledger/       schema, DAO, validity rules, blob store
  router/       role-based LLM client, failover, cache
  ingest/       arXiv/OpenAlex search, LaTeX source fetch
  extract/      table parsing, claim extraction, prompts/
  entities/     canonical names + alias resolution
  analyze/      conflict detection, evidence pooling
  experiments/  typed templates, run harness
  loop/         hypotheses, preregistration, next-step selection, critic
  report/       renderer with evidence contract
config/         providers.yaml, entity seed list
docs/           one spec per milestone, decisions log
evals/          settled-questions benchmark
data/           ledger DB and blob store (gitignored)
```

## Working rules for Claude Code

- Read the current milestone spec in `docs/` before writing code. If it doesn't exist,
  draft it and ask for approval before implementing.
- One milestone at a time. Don't scaffold future modules ahead of need.
- Ledger and router code is test-first. Every invariant above needs a test that fails
  when it's violated.
- Ask before adding a dependency. Prefer the standard library.
- Windows host: use `pathlib`, never hardcode `/` or `\` separators, no Unix-only modules.
- Never commit `.env`, `data/`, model weights, or anything under `data/store/`.
- Small commits with conventional messages (`feat:`, `fix:`, `test:`, `docs:`).
- When a design question isn't covered here or in a spec, ask rather than guess.
- Keep this file short. Detailed designs go in `docs/`, not here.
- After finishing a milestone, update the status table below.

## Milestones

| | Scope | Done when | Status |
|---|---|---|---|
| M0 | Repo setup, ledger schema, validity rules, blob store | Invalid nodes rejected by tests | done |
| M1 | Router, ingest, LaTeX table parsing, claim extraction | >= 80% field accuracy on 5 hand-labelled papers | todo |
| M2 | Entity resolution, conflict detection, evidence pooling | Finds known conflicts in LoRA literature | todo |
| M3 | Experiment harness, replicate 2-3 published results | Measurements within reported ranges | todo |
| M4 | Hypotheses, preregistration, next-step selection, critic | Loop runs on a hard budget and stops itself | todo |
| M5 | Report with evidence contract, settled-questions eval | Reaches known answers on most hidden questions | todo |
