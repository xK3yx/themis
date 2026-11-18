# M1: Router, ingest, table parsing, claim extraction

Status: **draft v2, awaiting approval**. Revised 2026-11-18 with the answers in §12.
No code is written until this spec is approved.

## 1. Goal and "done when"

Turn LoRA-family papers into ledger evidence: find papers, fetch their arXiv LaTeX, parse
results tables into cell-addressed grids, and have an LLM label those grids so that each
reported result becomes a claim whose every field traces to a table cell or byte span.

**Done when all of these hold:**
- Field-level accuracy ≥ 80% on the **3 held-back** eval papers (§8). All 5 are reported;
  only the held-back 3 decide pass or fail.
- Every stage resumes cleanly after a simulated quota stop (§7.2).
- The mutation check (§10.3) has zero surviving mutants across migrations, the table
  parser and the provenance checks.
- The test suite (offline: no live API calls) and `ruff check` pass in CI on Windows and Linux.

### In scope
Router (all four roles), arXiv + OpenAlex search, dedupe, LaTeX source fetch and unpack,
table parsing, row/column labelling, regime extraction from tables and methods prose,
claim assembly from tables, a CLI to run stages, the evaluation harness.

### Out of scope
- Canonical entity names and alias resolution ("LoRA" vs "Low-Rank Adaptation"): M2.
  M1 keeps names **verbatim** as they appear in the paper.
- Using embeddings for anything. The `embedder` role is implemented and tested in M1 but
  first used in M2.
- **Results stated only in prose** ("LoRA reaches 71.2 on MNLI" with no table): deferred
  until after M1 passes its eval. Prose *is* used in M1, but only for regime details.
- **Semantic Scholar**: not used in M1. Its config slot exists, disabled (§3.1).
- Conflict detection, pooling, `contradicts` edges: M2.
- PDF-only papers (no LaTeX source): recorded as skipped, not parsed.
- Figures, and tables included as images.

## 2. Changes to M0

All additive, and none rewrites an existing node.

### 2.1 Schema migrations
M0's `connect()` refuses any DB whose schema differs from `schema.sql`, so new tables need
a migration path:
- `src/themis/ledger/migrations/0001_m0.sql` (today's `schema.sql`, moved), `0002_m1.sql`, …
- `connect()` on an empty DB applies all migrations; on an existing DB with
  `user_version = k` it applies `k+1..n` in one transaction, then runs the integrity check
  against a fresh in-memory install of all migrations.
- Migrations may only `CREATE` (tables, indexes, triggers), or replace a trigger with a
  stricter one. A test asserts no migration contains `DROP TABLE`, `ALTER ... DROP` or
  `DELETE`.
- Every new ledger table gets the same append-only triggers as M0 (no UPDATE / DELETE /
  overwrite), generated from one template so none can be forgotten.

### 2.2 Transactions serialise check-then-insert
M0's idempotency is "look up, then insert". Two writers could both see "absent" and race;
today the loser gets a V18 abort (safe, but an error instead of idempotency). M1 fixes it:
- The **outermost** `tx()` opens with `BEGIN IMMEDIATE` (takes the write lock before the
  read), and nested `tx()` calls stay savepoints. Every DAO check-then-insert happens
  inside one `tx()`.
- `connect()` sets `PRAGMA busy_timeout` (value from config, not code).
- New test: two connections in two threads add the same node concurrently, and both
  return the same id with no error. The test fails if `BEGIN IMMEDIATE` is removed.

### 2.3 `TableCell` locator gains a required cell span
M0's `TableCell = {table: Span, row, col}` addresses a cell by grid coordinates, which
depend on how the parser expands `\multicolumn` / `\multirow`. M1 makes
`cell: Span` **required**: the exact bytes of that cell's content, lying inside `table`.
Row/col stay as the semantic address, but provenance never depends on trusting the parser.
- pydantic: `TableCell.cell: Span`, validated to satisfy `table.start <= cell.start <
  cell.end <= table.end`.
- DB (migration 0002, new trigger on `nodes`): **V5b**, cell span well-formed and inside
  the table span; **V6b**, cell text byte length = `end - start`.
- DAO: **V7b**, the blob bytes at the cell span equal the cell text.
- `NODE_SCHEMA_VERSION` stays 1: no `table_cell` node exists yet, so no IDs change.

### 2.4 Producers point at the exact LLM call
For LLM-produced nodes, the observation's producer is
`{"role", "provider", "model", "llm_call": <cache key>, "prompt": <template id@hash>}`,
so every node traces to the stored request and response that produced it (§3.5).

### 2.5 Claim provenance enforced in the data layer (new rules)
Migration 0002 adds a trigger for `derived` nodes whose `rule` is `table_result`
(the only claim rule in M1):
- **V21**: every `from` value anywhere in the claim content (found with `json_tree`) is
  one of the node's parents.
- **V22**: `value.from` is a `cited` node with a `table_cell` locator.
- **V23**: the claim's `value.number` equals that cell atom's `content.number.value`,
  and likewise `pm`.

The DAO adds the check that needs blob bytes:
- **V24**: for every number in a claim (the result value, its `pm`, and every numeric
  regime value), the `from` atom's bytes in the blob contain a numeric literal that
  parses to exactly that number. The atom's content records the literal's byte span
  (`number.span`), which must lie inside the cell or quote span, and its bytes must
  re-parse to the value.

No claim enters the ledger without passing V21–V24, whether it's written through the DAO
or by raw SQL (V21–V23).

## 3. Router (`src/themis/router/`)

### 3.1 Config: `config/providers.toml`
Committed, parsed with stdlib `tomllib`. It contains no keys and no hardcoded limits, and
model IDs live only here.

```toml
[defaults]
temperature = 0
timeout_s = 120
max_backoff_s = 60          # a 429 asking to wait longer than this moves to the next entry
max_retries_per_entry = 3
busy_timeout_ms = 5000
max_concurrency = 4

[network]
allowed_hosts = [           # outbound allowlist; every request is checked against it
  "generativelanguage.googleapis.com", "api.groq.com", "openrouter.ai",
  "api.jina.ai", "router.huggingface.co",
  "export.arxiv.org", "arxiv.org", "api.openalex.org",
]

[providers.gemini]
base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
key_env  = "GEMINI_API_KEY"
[providers.groq]
base_url = "https://api.groq.com/openai/v1/"
key_env  = "GROQ_API_KEY"
[providers.openrouter]
base_url = "https://openrouter.ai/api/v1/"
key_env  = "OPENROUTER_API_KEY"
[providers.jina]
base_url = "https://api.jina.ai/v1/"
key_env  = "JINA_API_KEY"
[providers.hf]
base_url = "https://router.huggingface.co/v1/"
key_env  = "HF_TOKEN"

# Ordered fallbacks: first entry is tried first.
[[roles.extractor]]
provider = "gemini"
model    = "<long-context model id>"
family   = "gemini"
context  = 1000000
[[roles.extractor]]
provider = "openrouter"
model    = "<free model id>"
family   = "qwen"
context  = 131072

[[roles.reasoner]]
provider = "gemini"
model    = "<model id>"
family   = "gemini"
[[roles.reasoner]]
provider = "openrouter"
model    = "<model id>"
family   = "deepseek"

[[roles.critic]]            # validated: no family may appear anywhere in roles.reasoner
provider = "groq"
model    = "<model id>"
family   = "llama"
[[roles.critic]]
provider = "openrouter"
model    = "<model id>"
family   = "mistral"

[[roles.embedder]]
provider = "jina"
model    = "<embedding model id>"
family   = "jina"
dims     = 1024
[[roles.embedder]]
provider = "hf"
model    = "<embedding model id>"
family   = "bge"
dims     = 1024

[sources.arxiv]
min_interval_s = 3
[sources.openalex]
mailto_env = "OPENALEX_MAILTO"
[sources.semantic_scholar]  # slot kept for later; not used in M1
enabled = false
key_env = "SEMANTIC_SCHOLAR_API_KEY"
```

Loaded into pydantic models at startup, then validated:
- Every role has ≥ 1 entry. Entries whose `key_env` is unset or blank are **disabled with a
  warning**, not an error. A role with zero enabled entries is an error.
- **Critic family check:** the set of `family` values over *every* `roles.critic` entry
  must be disjoint from the set over *every* `roles.reasoner` entry, so a fallback can never
  quietly make the critic the same family as the reasoner. Providers may repeat across the
  two roles. The check runs on **all** entries, including disabled ones, so enabling a key
  later can't create a violation.
- Embedder entries must declare `dims`.
- `sources.semantic_scholar.enabled = true` is rejected in M1 with a clear message, until
  the source is implemented.

### 3.2 `.env`
A ~20-line stdlib parser (`KEY=value`, optional quotes, `#` comments, blank values
allowed), and no dependency. Real environment variables override `.env`. Values are never
logged; the loader exposes only `get(name) -> str | None`. Keys are read by the `key_env` /
`mailto_env` names from config, never named in code.

### 3.3 Client
One `httpx.Client` speaking the OpenAI-compatible API (`/chat/completions`, `/embeddings`):

```python
router.chat(role: str, messages: list[dict], *, schema: type[BaseModel] | None,
            template: TemplateRef) -> ChatResult      # parsed + validated output, call key
router.embed(role: str, texts: list[str]) -> EmbedResult  # vectors + model that made them
```
Code passes a **role** and a **prompt template ref**, never a model name. The transport is
injectable, which is how tests substitute the fake provider (§10.1).

### 3.4 Fallback, backoff, quota
For each call, entries are tried in config order:

| Response | Action |
|---|---|
| 200 + output validates | Done. |
| 200 + output malformed (not JSON, fails schema) | One repair retry on the same entry, with the validation error appended. Then the next entry. |
| 429 with wait ≤ `max_backoff_s` (`Retry-After`, `x-ratelimit-reset-*`, or Gemini `RetryInfo`) | Sleep that long (plus jitter) and retry the same entry, up to `max_retries_per_entry`. |
| 429 with longer or unknown wait, or a body indicating a daily/quota cap | Mark the entry **exhausted until** its reset time (when given, else next UTC midnight). Next entry. |
| 5xx, timeout, connection error | Exponential backoff with jitter, then the next entry. |
| 400 context-length error | Next entry. |
| Other 4xx (401, 403, bad request) | Disable the entry for this process and log it. Next entry. |

When every entry for a role is exhausted, the router raises `QuotaExhausted(role,
earliest_reset)`. This is a **normal state, not a crash** (§7).

Limits are *learned*: response headers (`x-ratelimit-remaining-*`, `x-ratelimit-reset-*`,
`retry-after`) and 429s are appended to `quota_events` in the **operational DB** (§3.6).
Before each call the router checks the latest event per entry, and skips entries known to
be exhausted without spending a request. No limit values appear in code; config may carry
optional `rpm_hint` / `rpd_hint` per entry, used only until headers are seen.

### 3.5 Cache (in the ledger: it's provenance)
`key = sha256(canonical_json({"provider", "model", "request": <canonical request body>,
"output_schema_version": int}))`, where the request body includes messages, temperature,
response_format, and any other parameter that affects output. It never includes auth
headers. This pins down CLAUDE.md's `sha256(model_id + prompt + schema_version)`.

- **Cache-first across the fallback list:** before any network call, look up the key for
  *every* entry of the role in order, and use the first hit. A cache hit **never** touches
  the transport (tested by a transport that fails the test if called).
- Only successful, schema-valid responses are cached.

Ledger table (migration 0002, append-only):
```sql
llm_calls(key PK, role, provider, model, request_blob → blobs, response_blob → blobs,
          prompt_template, output_schema_version, prompt_tokens, completion_tokens,
          latency_ms, created_at)
```
Request and response bodies are blobs (`application/json`), so the exact prompt and raw
output behind any node can be re-read.

### 3.6 Operational DB: `data/ops.db`
Separate from the ledger, because it holds operational state, not evidence:
```sql
quota_events(id INTEGER PK, provider, model, kind CHECK IN
             ('headers','rate_limited','exhausted','error','disabled'),
             reset_at, detail_json, observed_at)
run_stats(run_id, stage, role, cache_hits, cache_misses, calls, started_at, ended_at)
```
- Not append-only-enforced, and not covered by the ledger integrity check. It may be pruned.
- **Nothing in the ledger depends on it.** Deleting `ops.db` loses only learned limits and
  stats, and costs a few wasted requests on the next run. Resumability (§7) uses the
  ledger alone. A test deletes `ops.db` mid-pipeline and checks the result is unchanged.
- `themis status` reads cache hit rate and quota state from here.

### 3.7 What may be sent
Free tiers may train on prompts. Prompts are built only by templates in
`src/themis/extract/prompts/`, whose inputs are typed as blob refs of public arXiv sources,
or ledger nodes. A template can't take a free-form string from elsewhere. The outbound host
allowlist blocks anything not in config. API keys go only in the `Authorization` header to
their own provider's host. `OPENALEX_MAILTO` goes only to `api.openalex.org`.

## 4. Ingest (`src/themis/ingest/`)

### 4.1 Search
- **arXiv API** (`export.arxiv.org/api/query`, Atom XML parsed with stdlib
  `xml.etree.ElementTree`; Python 3.11's bundled expat resists entity-expansion attacks and
  external entities aren't resolved). Pacing from `sources.arxiv.min_interval_s`.
- **OpenAlex** `/works` with `mailto` from `OPENALEX_MAILTO` (polite pool), using cursor
  paging. The arXiv id comes from `ids` and `locations`.
- Queries come from a committed seed file `config/search.toml` (topics, date range, page
  caps), not from code.

Every HTTP GET (search pages, e-prints) goes through one fetcher that caches by
`sha256(canonical {"method","url","params"})` into a new append-only `http_fetches` ledger
table (`key, url, status, response_blob, fetched_at`). Re-running a search replays it
from the ledger without network, and each paper's metadata traces to the exact response.
Secrets are excluded from the key and the stored URL: the `mailto` value is replaced by
`<OPENALEX_MAILTO>` before hashing and storing.

### 4.2 Paper records and dedupe
- `paper_records` (append-only): one row per (paper, source API response), with
  `source_api`, `fetch_key → http_fetches`, extracted ids (arXiv id without version, DOI
  lowercased, OpenAlex id), title, first author, year.
- **Canonical paper key**, by first available: `arxiv:<id>` → `doi:<doi>` → `openalex:<id>`.
  Records sharing any strong identifier are the same paper. `paper_links(a, b, reason)`
  records each merge and its reason.
- Title-only matches (NFKC + casefold + strip non-alphanumerics, same first author, year
  ±1) are **not** auto-merged. They're recorded as `possible_duplicate` links for review.

### 4.3 LaTeX source fetch
- `https://arxiv.org/e-print/<id>v<n>`: the **version is pinned** (latest at first fetch,
  recorded); later runs reuse it. A newer arXiv version is a new fetch, never a silent
  replacement.
- The raw e-print bytes are stored as a blob. They may be a gzipped tar, a single gzipped
  `.tex`, or a PDF (→ `no_latex_source`, skipped).
- **Unpacking is in-memory only** (`tarfile` / `gzip` streams; nothing is extracted to
  disk). Rules: reject absolute paths, `..` components, symlinks, hardlinks and device
  files; cap the member count and total uncompressed size (config). Only `.tex`, `.bbl`,
  `.sty`, `.cls`, `.bib` members are kept. Each kept file becomes its own blob and its own
  M0 `sources` row, with origin `{"arxiv", "version", "path", "archive": <blob>}`. Files are
  **not flattened**: a table in `tables/main.tex` is cited by byte offsets in *that* file.
- The main file is found as the `.tex` with `\documentclass` (ties: `\begin{document}`,
  then name `main.tex`). `\input` / `\include` give the reading order, used only for
  context such as macros and section order.

## 5. Table parsing (`src/themis/extract/tables.py`)

Purely mechanical, with no LLM. Output is a `ParsedTable` per tabular environment, stored as a
JSON blob and recorded in `stage_records` with `parser_version`.

### 5.1 Structure
```
ParsedTable
  source_id, table_span: Span           # \begin{table}..\end{table} (or bare tabular)
  tabular_span: Span                    # the tabular environment itself
  caption: Span | None, label: str | None, env: str, colspec: str
  n_rows, n_cols                        # logical grid after span expansion
  rules: [{after_row, kind: hline|toprule|midrule|bottomrule|cline|cmidrule, cols}]
  cells: [Cell]                         # one per anchor cell
  warnings: [str]

Cell
  row, col, rowspan, colspan            # logical grid; covered positions point to the anchor
  span: Span                            # exact bytes of the cell content (incl. markup)
  raw: str                              # == span.text
  text: str                             # markup stripped, macros expanded (display only)
  style: {bold, italic, underline, color, arrow, superscript_marks}
  number: {value, pm, pm_style, percent, span: Span, pm_span: Span | None} | None
  missing: "dash" | "na" | "oom" | "empty" | None
```
Grid coordinates are 0-based, and row 0 is the first row of the tabular. `number.span` is the
byte span of the numeric literal itself (V24 relies on it).

### 5.2 Tokenising
A brace-depth-aware tokeniser (stdlib only) splits on `&` and `\\` at depth 0. It respects
`\&`, `\%`, `%` comments to end of line, `\verb|..|`, nested braces, math mode,
`\\[2pt]` and `\tabularnewline`. All positions are **byte offsets** into the file's blob.
The parser is organised as a registry of named **case handlers** (one per §5.3 row), so
each can be disabled by the mutation check (§10.3).

`pylatexenc` is not used. If the eval shows the tokeniser is the bottleneck, I'll ask
before adding it.

### 5.3 Messy cases handled explicitly (20; each needs a real-table golden test, §10.2)

| # | Case | Handling |
|---|---|---|
| C1 | `\multicolumn{n}{spec}{x}` | colspan n; cell span = `x`'s bytes |
| C2 | `\multirow{n}{w}{x}`, `\multirow[t]{n}{*}{x}`, negative n | rowspan |n|; covered cells marked covered, not empty data |
| C3 | `\multirow` inside `\multicolumn` and vice versa | both spans applied |
| C4 | Hierarchical headers (`\multicolumn` + `\cmidrule(lr){2-4}`) | each column gets a header **path** |
| C5 | Bold/underline best: `\textbf{}`, `{\bf x}`, `\bfseries`, `\mathbf{}`, `\boldsymbol{}`, `\underline{}`, `\uline{}`, `\textit{}` | style flags; number parsed from inner text |
| C6 | Column-wide styles `>{\bfseries}c` | applied to that column's cells |
| C7 | Colour: `\cellcolor{}`, `\textcolor{}{}`, `\colorbox{}{}`, `\rowcolor{}` | `style.color` |
| C8 | Variance: `71.2 \pm 0.3`, `71.2$\pm$0.3`, `$71.2_{\pm 0.3}$`, `71.2\scriptsize{$\pm$0.3}`, `71.2{\tiny ±0.3}`, `71.2 (0.3)`, unicode `±` | `value`, `pm`, `pm_style`, `pm_span`; meaning labelled later (§6.3) |
| C9 | Number formats: `71.2\%`, `1{,}024`, `1,024`, `$-$`, `−`, `1e-4`, `$1\times10^{-4}$`, `\num{}` | normalised into `number` |
| C10 | Missing: `-`, `--`, `–`, `—`, `N/A`, `OOM`, `✗`, `\xmark` | `missing` set, `number = None` |
| C11 | Direction arrows `$\uparrow$`, `$\downarrow$`, `↑`, `↓` | `style.arrow` |
| C12 | Footnote marks `*`, `†`, `‡`, `$^\dagger$`, `\textsuperscript{}` | `superscript_marks`; excluded from the number |
| C13 | Rules `\hline`, `\toprule`, `\midrule`, `\bottomrule`, `\cline`, `\cmidrule`, `\addlinespace` | recorded in `rules`; separate headers from body |
| C14 | Environments `tabular*`, `tabularx`, `longtable` (`\endhead`/`\endfirsthead` repeats removed), `threeparttable`, `table*`, `sidewaystable`, `subtable`/`\subfloat`, wrappers `\resizebox`, `\scalebox`, `adjustbox` | each tabular → one `ParsedTable` |
| C15 | Zero-arg macros from the preamble (`\newcommand{\lora}{LoRA}`, `\def\ours{..}`) | expanded in `text` only; spans keep original bytes |
| C16 | Macros with arguments, unknown macros | left as-is in `text`; warning |
| C17 | Trailing `&`, short rows | padded with `empty` cells; warning |
| C18 | Table contents in an `\input` file | parsed in that file's blob; both files' `source_id`s recorded |
| C19 | Table as `\includegraphics` | zero cells, warning `image_table`; skipped |
| C20 | Transposed tables (methods as columns) | not detected here; the LLM labels orientation (§6.3) |

Malformed input never raises out of the parser. It yields a partial table plus warnings,
or a `parse_failed` stage record with the reason.

### 5.4 Results-table selection
Every table is parsed. A mechanical score orders them, but **no table is dropped
mechanically**. The LLM labelling step marks non-result tables, and that decision is recorded.

## 6. Claim extraction (`src/themis/extract/`)

### 6.1 Layered evidence
1. **Atoms: `cited` nodes, one per evidence piece, no interpretation.**
   - *Cell atom*: `TableCell` locator (with the required cell span); content
     `{"type": "table_cell", "raw", "number", "style", "missing", "parser_version"}`,
     purely mechanical from §5.
   - *Span atom*: `Span` locator on methods prose; content `{"type": "text_span",
     "section": <title verbatim>, "number": {...} | null}`. **In M1, span atoms are only used
     for regime details**, never for result values.
   Atoms are created only for cells and spans that some claim uses.
2. **Claims: `derived` nodes**, `rule: "table_result"`, whose parents are the atoms.
   A claim is Themis's interpretation ("this row is method X on dataset Y"), so it is an
   inference over cited evidence. A result needs at least its value cell plus its row and
   column label cells, which satisfies the ≥ 2-parent rule naturally. V21–V24 (§2.5) apply.

### 6.2 Claim schema (content of a claim node)
```
ResultClaim  (output_schema_version 1)
  type: "result"
  evidence_grade: "table_cell"                     # the only grade in M1
  value:      {number, pm, pm_meaning: "std"|"stderr"|"ci95"|"range"|"unknown",
               percent: bool, from: <cell atom id>}
  method:     {text: <verbatim>, from: <atom id>}
  task:       {text, from}
  metric:     {text, direction: "higher"|"lower"|"unknown", from}
  base_model: {text, from} | null
  setting:    [{text, from}]                       # other header-path qualifiers
  regime:     {<field>: {value, unit|null, text, from}}   # see 6.4; only fields found
  is_best_marked: bool
  source_table: {label, caption_from: <atom id>} | null
```
`text` fields are verbatim, checked as substrings of their atom's text. Normalisation is M2.

### 6.3 Table labelling (LLM, role `extractor`)
Input: the caption, the `\label`, the paragraph that references the table (found
mechanically via `\ref{label}`), and a **rendered grid** with each cell's `(row, col)` and
`text`, header paths shown.

Output (JSON schema `TableLabels`, validated):
```
is_results_table: bool, hyperparameter_table: bool
orientation: "methods_as_rows" | "methods_as_columns"
header_rows: [int], label_cols: [int]
col_roles: [{col, role: task|metric|setting|method|base_model|other}]
row_roles: [{row, role: ...}]
pm_meaning: std|stderr|ci95|range|unknown, pm_evidence: {quote} | null
metric_direction: [{col_or_row, direction}]
result_cells: [{row, col}] | "all_numeric_body"
```
The LLM refers to cells **only by coordinates**, and quotes must be exact substrings of
the caption or paragraph. Validation rejects out-of-range coordinates, unknown quotes, and
roles on covered cells. Then one repair retry, then the next fallback model. A table that
still fails is recorded as `label_failed`.

Claim assembly is then **mechanical**: for each result cell, take its row's label cells,
its column's header path, apply the roles, and build the claim with `from` pointers.

### 6.4 Regime details (LLM, role `extractor`)
From hyperparameter **tables** (cell atoms) and from methods/setup **prose** (span atoms).
Sections are located mechanically (`\section`, `\subsection`, `\paragraph`, and appendix
sections like "Hyperparameters"). The LLM returns
`[{field, value, unit, quote, scope: "global" | {"table_label"} | {"method": <verbatim>}}]`
with fields from a fixed list: `base_model, lora_rank, lora_alpha, lora_dropout,
target_modules, learning_rate, batch_size, epochs, max_steps, warmup, optimizer,
scheduler, quantization_bits, trainable_params, n_seeds, seq_len, hardware`.

Mechanical checks:
- `quote` must occur in the section's bytes. Code finds its offsets, and that span becomes the
  span atom. Ambiguous occurrences take the one in the named section, else reject.
- Numeric values must appear as a numeric literal in the quote, with its byte span recorded (V24).
- Regime entries attach to claims by scope only.

### 6.5 Untrusted input
Paper text is untrusted input to the LLM. The LLM has no tools, and its output is accepted
only when it validates and every reference resolves to coordinates or verbatim bytes.
Free text from the LLM never reaches a node.

## 7. Resumability and concurrency

### 7.1 Stages
`search → resolve (dedupe) → fetch → unpack → parse → label → regime → assemble`

Each stage derives its **work list from the ledger** (e.g. `parse` = source files with no
`stage_records` row for `(parse, source_id, parser_version)`), and processes one item per
transaction (`BEGIN IMMEDIATE`): its nodes, observations and stage record commit together
or not at all. LLM calls happen *before* the write transaction and are cached as they
return, so a crash after a call costs no quota on re-run.

```sql
stage_records(stage, input_key, version, status CHECK IN ('done','skipped','failed'),
              output_ref, detail_json, created_at,
              PRIMARY KEY (stage, input_key, version))   -- ledger, append-only
```
- `done` / `skipped` are never retried. `failed` is retried only with `--retry-failed` or
  when the stage `version` bumps.
- **Quota exhaustion writes no record.** The item stays pending.

### 7.2 Quota stop
`QuotaExhausted` in a stage loop → keep what is committed, print `stopped: <role> quota
exhausted; earliest reset <time>; <n> items pending`, exit code **75** (`EX_TEMPFAIL`).

Test: a fake provider that allows k calls then returns a daily-quota 429. Run to the stop,
restart in a fresh process, and assert (a) no item is duplicated, (b) no LLM call is
repeated (the transport counts calls), (c) the final ledger equals an uninterrupted run's.

### 7.3 Concurrency
**One writer process.** Network calls may run in a thread pool bounded by
`defaults.max_concurrency`, but only the main thread writes to SQLite, and each check-then-insert
sits in one `BEGIN IMMEDIATE` transaction (§2.2). A second `themis` process against the same
DB waits on `busy_timeout` and then fails clearly.

### 7.4 CLI (`typer`)
`themis search | resolve | fetch | parse | label | regime | assemble | run [--until-quota]
| status | eval`, plus `themis dev record` for fixtures (§10.1). `status` prints per-stage
counts, quota state per role entry, and cache hit rate.

## 8. Evaluation (`evals/m1/`)

### 8.1 Papers
| Paper | arXiv | Kind |
|---|---|---|
| LoRA | 2106.09685 | method |
| QLoRA | 2305.14314 | method |
| AdaLoRA | 2303.10512 | method |
| DoRA | 2402.09353 | method |
| LoRA Learns Less and Forgets Less (Biderman et al., 2024) | 2405.09673 (to confirm at pinning) | empirical comparison |

The arXiv **version and e-print sha256 are pinned** in the gold files; the eval refuses to
score if the hash differs. Paper sources are not committed.

**Split:** 2 tuning + 3 held back. You choose which, since you hold the gold; I propose
tuning = LoRA, AdaLoRA and held back = QLoRA, DoRA, LoRA Learns Less, so the held-back set
includes the comparison paper. Held-back gold, tables and LLM outputs must not be inspected by
whoever tunes prompts or the parser before the final measurement. Held-back papers are also
excluded from parser golden fixtures (§10.2) and recorded LLM fixtures (§10.1).

### 8.2 Hand-labelling procedure (you label)
1. Work from the PDF and LaTeX of the pinned version, without seeing any Themis output.
2. For each results table, record one gold claim per reported number in
   `evals/m1/gold/<arxiv_id>.jsonl`: `table`, `value`, `pm`, `pm_meaning`, `method`, `task`,
   `metric`, `direction`, `base_model`, `setting`, `is_best_marked`, and the regime fields
   the paper states for it. Strings verbatim as printed.
3. The scope rule is written in the gold file header: which tables count as results tables,
   and whether averaged columns ("Avg.") are claims (proposed: yes).
4. A second pass a day later on 1 table per paper estimates label noise. A field with
   self-agreement < 95% gets its definition fixed before scoring.
5. Gold files and `evals/m1/README.md` (field definitions, which I draft for you to edit)
   are committed. Held-back gold can be committed encrypted or kept outside the repo until
   the final measurement (Q-B in §13).

### 8.3 Scoring
- **Matching:** each gold claim is matched to at most one predicted claim from the same table
  with equal `value` (at printed precision), then by best row/column text overlap. One-to-one,
  greedy by score.
- **Field accuracy** = correct fields / total gold fields, micro-averaged. An unmatched gold
  claim counts all its fields wrong.
  - Numbers: equal at the printed precision.
  - Verbatim strings: equal after whitespace collapse and casefold.
  - Enums: exact.
  - Regime fields: value and unit equal.
- **Pass: ≥ 0.80 field accuracy on the held-back 3** (micro-averaged over their gold
  fields). Tuning-paper and all-5 scores are reported, but don't count.
- Also reported: claim precision, per-field and per-paper accuracy, provenance validity
  (100% by construction), cache hit rate, LLM calls used.
- `themis eval m1` writes `evals/m1/results/<date>.json` with the config hash.

## 9. Package layout (new)
```
config/providers.toml, config/search.toml
src/themis/ledger/migrations/0001_m0.sql, 0002_m1.sql
src/themis/config.py              # .env + TOML loading, validation
src/themis/ops.py                 # data/ops.db
src/themis/router/                # client.py, cache.py, quota.py, errors.py
src/themis/ingest/                # http.py (cached fetcher), arxiv.py, openalex.py,
                                  # papers.py (dedupe), source.py (e-print, unpack)
src/themis/extract/               # latex.py, tables.py, numbers.py, label.py,
                                  # regime.py, claims.py, prompts/
src/themis/pipeline.py, src/themis/cli.py
scripts/mutation_check.py
evals/m1/                         # gold/, README.md, score.py, results/
tests/router, tests/ingest, tests/extract, tests/pipeline
tests/fixtures/http/, tests/fixtures/llm/, tests/fixtures/latex/{golden,synthetic}/
```

## 10. Testing

Router, ledger and provenance code is **test-first**, as for M0.

### 10.1 No live APIs: record once, replay
- Tests never call a live API. In CI, any transport other than the replay transport raises.
- `themis dev record <scenario>` (opt-in, needs keys and network) runs a scenario against real
  services through the normal client, and writes each request/response pair to
  `tests/fixtures/http/` or `tests/fixtures/llm/` as JSON. Tests replay them with a
  `httpx.MockTransport` that matches on the canonical request.
- **Scrubbing before write:** `Authorization` and other auth headers are dropped, and key-like
  query params and the `mailto` value are replaced with placeholders. The recorder refuses to
  write if any non-blank `.env` value appears anywhere in the payload. A test scans every
  committed fixture for `Authorization`, `Bearer`, `api_key`, `key=` and email addresses, and
  fails on any hit.
- LLM fixtures come only from tuning papers or other non-eval papers, never held-back ones.
- Re-recording is explicit and shows up in review as a fixture diff.

### 10.2 Table parser golden tests
- **Real tables from ≥ 10 different LoRA-family papers** (not the 3 held-back eval papers),
  stored as the original LaTeX snippet in `tests/fixtures/latex/golden/<arxiv_id>/`, with a
  metadata file for arXiv id, version, file path, byte offsets in the original file, licence,
  and the §5.3 case tags it covers.
- Each golden has a hand-written expected `ParsedTable` JSON with **every cell's**
  coordinates, row/col span, byte span, raw text, number (value, pm, percent, literal span),
  missing marker and style flags. Each golden test checks all of them.
- Expected files are written **from the source by hand before the parser handles that case**,
  not generated from parser output. There is no snapshot-update command; changing an
  expected file needs a commit message explaining why.
- **Coverage test:** every case C1–C20 is tagged on at least one real-table golden. The test
  fails if a case has none.
- Licensing: see Q-A in §13. Synthetic fixtures (`latex/synthetic/`) are allowed *in addition*
  for edge cases, never instead of a real golden.

### 10.3 Mutation check (`scripts/mutation_check.py`, committed)
The M0 check, generalised and committed:
- **SQL mutants:** every `RAISE`, every trigger, and the key CHECKs and indexes in **all**
  migrations, each disabled in turn.
- **Python mutants:** the provenance checks (V7, V7b, V11, V24, the label/regime validators,
  the claim schema `from` check) and every parser case handler C1–C20 are registered by name.
  A small pytest plugin, enabled only by the mutation runner, replaces one at a time with a
  pass-through.
- Each mutant must make the suite fail. An equivalent mutant (one that changes nothing, like M0's
  STRICT `NOT NULL`) must be listed in the spec with a reason.
- Runs locally before the milestone is marked done, and in a separate CI workflow on pushes to
  `main` and manual dispatch (too slow for every PR).

### 10.4 Router (fake provider)
A scriptable fake provider (an `httpx.MockTransport`) that can return 200, malformed JSON,
schema-invalid JSON, 429 with short/long `Retry-After`, Gemini-style `RetryInfo`, daily-quota
bodies, 5xx, timeouts and context-length errors, in any per-entry sequence. Tests:
- Fallbacks happen **in config order**, for each failure kind in the §3.4 table.
- The critic family check runs over **every** fallback entry (including disabled ones),
  with parametrised overlap at each position.
- Cached calls **never hit the network**: the transport fails the test if called on a hit,
  including hits from a later fallback entry.
- Backoff honours the wait hints and never sleeps past `max_backoff_s` (injected clock).
- `QuotaExhausted` carries the earliest reset, and exhausted entries are skipped without a request.
- The cache key is stable across key order, differs by model / temperature / schema version, and
  excludes auth. Failed responses are not cached.
- The host allowlist blocks other hosts; keys never appear in URLs, logs or blobs.

### 10.5 Extraction provenance
- **Every extracted number traces to bytes:** after a full replayed pipeline run, a test walks
  every `table_result` claim and checks that its value (and pm) come from a `table_cell` atom
  whose blob bytes at `number.span` re-parse to exactly that number, and the same for every
  numeric regime value against its atom's bytes.
- Direct tests that V21–V24 reject: a claim whose value `from` is a span atom; a `from` not
  among the parents; a value not equal to the cell's number; a number literal span outside
  the cell, or whose bytes parse to a different number. V21–V23 are also tested by raw SQL.

### 10.6 Other
- Migrations: upgrading an M0 DB keeps every M0 row byte-identical and passes the integrity
  check. No destructive statements. New ledger tables are append-only (parametrised as in M0).
- Concurrency: two threads add the same node, and both get its id (§2.2).
- Tar safety: traversal, absolute path, symlink, oversize → rejected; nothing written to disk.
- Resumability (§7.2), and deleting `ops.db` changes nothing in the ledger (§3.6).
- CI stays green on Windows and Linux. Network-marked tests are skipped in CI.

## 11. Dependencies (approved 2026-11-18)

| Package | Why |
|---|---|
| `httpx` | HTTP for all providers and APIs; `MockTransport` gives offline replay without another dep. |
| `typer` | The CLI. |

Config is TOML via stdlib `tomllib` (no PyYAML). Not adding: `python-dotenv`, `tenacity`,
`feedparser`, `defusedxml`, `pylatexenc` (ask first if the eval shows the tokeniser is the
bottleneck), `respx`, `rapidfuzz`, `numpy`.

## 12. Decisions (2026-11-18)
1. `TableCell.cell` byte span is required. Provenance never depends on trusting the parser.
2. Critic rule is declared family only, checked over every fallback entry. Providers may be shared.
3. Semantic Scholar is out of M1; its config slot is kept, disabled.
4. Claims are `derived` nodes over `cited` cell/span atoms.
5. Eval papers: LoRA, QLoRA, AdaLoRA, DoRA, LoRA Learns Less and Forgets Less.
6. You hand-label the gold, with a 2 tuning / 3 held-back split. Pass = ≥ 80% on the held-back 3 only.
7. TOML config (`config/providers.toml`); CLAUDE.md updated.
8. Stdlib tokeniser; ask before adding `pylatexenc`.
9. Quota events in a separate `data/ops.db`; the LLM cache stays in the ledger.
10. Result values from tables only in M1; prose is used only for regime details.

## 13. Open questions (new, raised by the decisions above)

- **Q-A. Licensing of golden fixtures.** The golden tests commit real LaTeX table snippets
  from ≥ 10 papers. Many arXiv papers carry arXiv's default non-exclusive licence, which
  grants arXiv, not us, the right to distribute them. Proposal: choose golden papers whose
  arXiv licence is CC BY, CC BY-SA or CC0 (recorded in metadata, and asserted by a test), and
  keep snippets to the table environment plus caption. If those can't cover all 20 cases, the
  fallback is to keep the snippets out of git and have tests fetch them by pinned sha256 into a
  gitignored cache. But that makes CI depend on arXiv being reachable, which conflicts with
  "never call live APIs". Which do you prefer?
- **Q-B. Keeping the held-back gold blind.** Committing held-back gold to the repo puts it in
  front of anyone (me included) who later tunes prompts. Options: keep it out of the repo
  until the final measurement, or commit it encrypted. Or is commit-in-the-clear fine,
  given you control when it's opened?
- **Q-C. Who writes the golden expected files?** I can write them by hand from the LaTeX before
  the parser handles each case, which is test-first. You'd spot-check a sample. Is that
  acceptable, or do you want to write or review all of them?
