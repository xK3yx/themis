# M1: Router, ingest, table parsing, claim extraction

Status: **draft, awaiting approval**. No code is written until this spec is approved.

## 1. Goal and "done when"

Turn LoRA-family papers into ledger evidence: find papers, fetch their arXiv LaTeX, parse
results tables into cell-addressed grids, and have an LLM label those grids so that each
reported result becomes a claim whose every field traces to a table cell or byte span.

**Done when:** field-level accuracy ≥ 80% on 5 hand-labelled papers (§8), every stage
resumes cleanly after a simulated quota stop, and the test suite (offline, no network)
plus `ruff check` pass in CI on Windows and Linux.

### In scope
Router (all four roles), arXiv + OpenAlex search, dedupe, LaTeX source fetch and unpack,
table parsing, row/column labelling, regime extraction, claim assembly, a CLI to run
stages, the evaluation harness.

### Out of scope
- Canonical entity names and alias resolution ("LoRA" vs "lora" vs "Low-Rank Adaptation"): M2.
  M1 keeps names **verbatim** as they appear in the paper.
- Using embeddings for anything. The `embedder` role is implemented and tested in M1 but
  first used for dedupe/retrieval in M2.
- Conflict detection, pooling, `contradicts` edges: M2.
- PDF-only papers (no LaTeX source): recorded as skipped, not parsed.
- Figures, and tables included as images.

## 2. Changes to M0

M1 needs four changes to what M0 shipped. All are additive, and none rewrites an existing node.

### 2.1 Schema migrations
M0's `connect()` refuses any DB whose schema differs from `schema.sql`, so new tables need
a migration path:
- `src/themis/ledger/migrations/0001_m0.sql` (today's `schema.sql`, moved), `0002_m1.sql`, …
- `connect()` on an empty DB applies all migrations; on an existing DB with
  `user_version = k` it applies `k+1..n` in one transaction, then runs the integrity check
  against a fresh in-memory install of all migrations.
- Migrations may only `CREATE` (tables, indexes, triggers) or replace a trigger with a
  stricter one. A test asserts no migration contains `DROP TABLE`, `ALTER ... DROP` or
  `DELETE`.
- Every new table gets the same append-only triggers as M0 (no UPDATE / DELETE /
  overwrite), generated from one template so none can be forgotten. The M0 mutation
  check is extended to cover them.

### 2.2 Transactions serialise check-then-insert
M0's idempotency is "look up, then insert". Two writers could both see "absent" and race.
Today the loser gets a V18 abort (safe, but an error instead of idempotency). M1 fixes it:
- The **outermost** `tx()` opens with `BEGIN IMMEDIATE` (takes the write lock before the
  read), and nested `tx()` calls stay savepoints. Every DAO check-then-insert happens
  inside one `tx()`.
- `connect()` sets `PRAGMA busy_timeout` (value in config, not code).
- New test: two connections in two threads add the same node concurrently, and both
  return the same id with no error. The test fails if `BEGIN IMMEDIATE` is removed.

### 2.3 `TableCell` locator gains a cell span
M0's `TableCell = {table: Span, row, col}` addresses a cell by grid coordinates. But the
grid depends on how the parser expands `\multicolumn` / `\multirow`, so a parser change
could silently move a coordinate. M1 adds a required `cell: Span`, the exact bytes of that
cell's content, which must lie inside `table`. Row/col stay as the semantic address. The
cell span makes the provenance checkable without trusting the parser. The DB trigger (V5/V6)
and the DAO check (V7) are extended to the cell span. Bumping `NODE_SCHEMA_VERSION` to 2
isn't needed, because no `table_cell` node exists yet; pydantic and the trigger simply
gain the field. (Open question Q1.)

### 2.4 Producers point at the exact LLM call
For LLM-produced nodes, the observation's producer is
`{"role", "provider", "model", "llm_call": <cache key>, "prompt": <template id@hash>}`.
That makes every node traceable to the stored request and response that produced it (§3.5).

## 3. Router (`src/themis/router/`)

### 3.1 Config: `config/providers.yaml`
Committed. No keys and no hardcoded limits; model IDs live only here.

```yaml
providers:
  gemini:     {base_url: "https://generativelanguage.googleapis.com/v1beta/openai/", key_env: GEMINI_API_KEY}
  groq:       {base_url: "https://api.groq.com/openai/v1/",  key_env: GROQ_API_KEY}
  openrouter: {base_url: "https://openrouter.ai/api/v1/",    key_env: OPENROUTER_API_KEY}
  jina:       {base_url: "https://api.jina.ai/v1/",          key_env: JINA_API_KEY}
  hf:         {base_url: "https://router.huggingface.co/v1/", key_env: HF_TOKEN}

roles:              # ordered fallbacks; first entry tried first
  extractor:
    - {provider: gemini,     model: "<long-context model id>", family: gemini, context: 1000000}
    - {provider: openrouter, model: "<free model id>",         family: qwen,   context: 131072}
  reasoner:
    - {provider: gemini,     model: "<model id>", family: gemini}
    - {provider: openrouter, model: "<model id>", family: deepseek}
  critic:             # validated: no family may appear in reasoner's list
    - {provider: groq,       model: "<model id>", family: llama}
    - {provider: openrouter, model: "<model id>", family: mistral}
  embedder:
    - {provider: jina, model: "<embedding model id>", family: jina, dims: 1024}
    - {provider: hf,   model: "<embedding model id>", family: bge,  dims: 1024}

defaults:
  temperature: 0
  timeout_s: 120
  max_backoff_s: 60          # a 429 asking to wait longer than this moves to the next entry
  max_retries_per_entry: 3
  busy_timeout_ms: 5000

allowed_hosts:               # outbound allowlist; every request is checked against it
  - generativelanguage.googleapis.com
  - api.groq.com
  - openrouter.ai
  - api.jina.ai
  - router.huggingface.co
  - export.arxiv.org
  - arxiv.org
  - api.openalex.org
  - api.semanticscholar.org
```

Loaded into pydantic models at startup, then validated:
- Every role has ≥ 1 entry. Entries whose `key_env` is unset or blank are **disabled with a
  warning**, not an error. A role with zero enabled entries is an error.
- **Critic family check:** the set of `family` values under `critic` must be disjoint from
  every family under `reasoner`, across *all* fallback entries, not just the first. So a
  fallback can never quietly make the critic the same family as the reasoner. Families
  are declared in config because model IDs don't reliably encode them. (Q2)
- Embedder entries must declare `dims`.

### 3.2 `.env`
A ~20-line stdlib parser (`KEY=value`, optional quotes, `#` comments, blank values allowed),
and no dependency. Real environment variables override `.env`. Values are never logged; the
loader exposes only `get(name) -> str | None`. `SEMANTIC_SCHOLAR_API_KEY` may be blank.
Keys are read by `key_env` name from config, never named in code.

### 3.3 Client
One `httpx.Client` speaking the OpenAI-compatible API (`/chat/completions`, `/embeddings`).
Public interface:

```python
router.chat(role: str, messages: list[dict], *, schema: type[BaseModel] | None,
            template: TemplateRef) -> ChatResult      # parsed + validated output, call key
router.embed(role: str, texts: list[str]) -> EmbedResult  # vectors + model that made them
```

Code passes a **role** and a **prompt template ref**, never a model name.

### 3.4 Fallback, backoff, quota
For each call, entries are tried in config order:

| Response | Action |
|---|---|
| 200 + output validates | Done. |
| 200 + output fails schema validation | One repair retry on the same entry, with the validation error appended. Then the next entry. |
| 429 with wait ≤ `max_backoff_s` (`Retry-After`, `x-ratelimit-reset-*`, or Gemini `RetryInfo`) | Sleep that long (plus jitter) and retry the same entry, up to `max_retries_per_entry`. |
| 429 with longer or unknown wait, or a body indicating a daily/quota cap | Mark the entry **exhausted until** its reset time (when given, else next UTC midnight). Next entry. |
| 5xx, timeout, connection error | Exponential backoff with jitter (1, 2, 4 s … capped), then the next entry. |
| 400 context-length error | Next entry (larger context, possibly). |
| Other 4xx (401, 403, bad request) | Disable the entry for this process and log it. Next entry. |

When every entry for a role is exhausted, the router raises `QuotaExhausted(role,
earliest_reset)`. This is a **normal state, not a crash** (§7).

Limits are *learned*: response headers (`x-ratelimit-remaining-*`, `x-ratelimit-reset-*`,
`retry-after`) and 429s are appended to `quota_events`. Before each call the router checks
the latest event per entry, and skips entries known to be exhausted without spending a
request. No limit values appear in code. Config may carry optional `rpm_hint` / `rpd_hint`
per entry, used only until headers are seen.

### 3.5 Cache (in the ledger)
`key = sha256(canonical_json({"provider", "model", "request": <canonical request body>,
"output_schema_version": int}))`, where the request body includes messages, temperature,
response_format, and any other parameter that affects output. It never includes auth
headers. This is CLAUDE.md's `sha256(model_id + prompt + schema_version)` with "prompt"
pinned down as the full request.

- **Cache-first across the fallback list:** before any network call, look up the key for
  *every* entry of the role in order, and use the first hit. A prompt answered yesterday
  by the fallback model costs nothing today, even if the primary is available again.
- Only successful, schema-valid responses are cached. Errors go to `quota_events` only.
- Hit and miss counters per role are written to `stage_records` (§7), so `themis status`
  can report hit rate.

New append-only tables (migration 0002):

```sql
llm_calls(key PK, role, provider, model, request_blob → blobs, response_blob → blobs,
          prompt_template, output_schema_version, prompt_tokens, completion_tokens,
          latency_ms, created_at)
quota_events(id INTEGER PK, provider, model, kind CHECK IN
             ('headers','rate_limited','exhausted','error','disabled'),
             reset_at, detail_json, observed_at)
```

Request and response bodies are stored as blobs (`application/json`), so the exact
prompt and raw output behind any node can be re-read.

### 3.6 What may be sent
Free tiers may train on prompts. Prompts are built only by templates in
`src/themis/extract/prompts/`, whose inputs are typed as blob refs of public arXiv sources,
or ledger nodes. A template can't take a free-form string from elsewhere. The outbound host
allowlist blocks anything not in config. API keys go only in the `Authorization` header to
their own provider's host.

## 4. Ingest (`src/themis/ingest/`)

### 4.1 Search
- **arXiv API** (`export.arxiv.org/api/query`, Atom XML parsed with stdlib
  `xml.etree.ElementTree`; Python 3.11's bundled expat resists entity-expansion attacks and
  external entities aren't resolved). Polite pacing (minimum interval between requests) is
  set in config.
- **OpenAlex** `/works` with `mailto=OPENALEX_MAILTO` (polite pool), using cursor paging.
  The arXiv id comes from `ids` and `locations`.
- **Semantic Scholar** is optional. It's enabled only when `SEMANTIC_SCHOLAR_API_KEY` is
  non-blank, and then used only to enrich OpenAlex results (e.g. finding an arXiv id
  OpenAlex lacks). With a blank key it is skipped silently, and nothing depends on it. (Q3)
- Queries come from a committed seed file `config/search.yaml` (topics such as LoRA,
  QLoRA, DoRA, AdaLoRA, VeRA, PEFT for small LLMs, plus date range and page caps), not from
  code.

Every HTTP GET (search pages, e-prints) goes through one fetcher that caches by
`sha256(canonical {"method","url","params"})` into a new append-only `http_fetches`
table (`key, url, status, response_blob, fetched_at`). Re-running a search replays it
from the ledger without network, and each paper's metadata traces to the exact response
it came from.

### 4.2 Paper records and dedupe
- `paper_records` (append-only): one row per (paper, source API response), with
  `source_api`, `fetch_key → http_fetches`, extracted ids (arXiv id without version, DOI
  lowercased, OpenAlex id), title, first author, year.
- **Canonical paper key**, by first available: `arxiv:<id>` → `doi:<doi>` → `openalex:<id>`.
  Records sharing any strong identifier are the same paper. `paper_links(a, b, reason)`
  records each merge and its reason.
- Title-only matches (NFKC + casefold + strip non-alphanumerics, same first author, year
  ±1) are **not** auto-merged. They're recorded as `possible_duplicate` links for review,
  because a false merge would mix two papers' evidence.

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
  M0 `sources` row, with origin `{"arxiv", "version", "path", "archive": <blob>}`. Files
  are **not flattened**: a table in `tables/main_results.tex` is cited by byte offsets in
  *that* file's blob, so offsets always refer to bytes exactly as arXiv served them.
- The main file is found as the `.tex` with `\documentclass` (ties: `\begin{document}`,
  then name `main.tex`). `\input` / `\include` are resolved to build a reading order;
  this is used only for context such as macros and section order.

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
  warnings: [str]                       # anything the parser could not handle

Cell
  row, col, rowspan, colspan            # logical grid; covered positions point to the anchor
  span: Span                            # exact bytes of the cell content (incl. markup)
  raw: str                              # == span.text
  text: str                             # markup stripped, macros expanded (for display only)
  style: {bold, italic, underline, color, superscript_marks: ["†", "*", ...]}
  number: {value, pm, pm_style, percent, sign, sci_exp, raw_number_span} | None
  missing: "dash" | "na" | "oom" | "empty" | None
```
Grid coordinates are 0-based, and row 0 is the first row of the tabular, headers included.
Each body row records which header rows sit above it.

### 5.2 Tokenising
A brace-depth-aware tokeniser (stdlib only) splits on `&` and `\\` at depth 0 inside the
tabular body. It respects `\&`, `\%`, `%` comments to end of line (not `\%`), `\verb|..|`,
nested braces, math mode `$..$` / `\(..\)`, `\\[2pt]`, and `\tabularnewline`. All
positions are tracked as **byte offsets** into the file's blob.

### 5.3 Messy cases handled explicitly (each gets a fixture test)

| Case | Handling |
|---|---|
| `\multicolumn{n}{spec}{x}` | colspan n; the cell's span is `x`'s bytes; spec alignment ignored |
| `\multirow{n}{w}{x}`, `\multirow[t]{n}{*}{x}`, negative n (spans upward) | rowspan |n|; the covered cells (blank or `{}`) are marked covered, not empty data |
| `\multirow` inside `\multicolumn` (and vice versa) | both spans applied |
| Hierarchical headers (`\multicolumn` + `\cmidrule(lr){2-4}`) | each column gets a header **path** (top → bottom header cells covering it) |
| Bold/underline best results: `\textbf{}`, `{\bf x}`, `\bfseries`, `\mathbf{}`, `\boldsymbol{}`, `\underline{}`, `\uline{}`, `\textit{}` | style flags set; number parsed from the inner text |
| Column-wide styles `>{\bfseries}c` in colspec | applied to that column's cells |
| Color best/second: `\cellcolor{}`, `\textcolor{}{}`, `\colorbox{}{}`, `\rowcolor{}` | `style.color` = colour name/spec |
| Variance: `71.2 \pm 0.3`, `71.2$\pm$0.3`, `$71.2_{\pm 0.3}$`, `71.2\scriptsize{$\pm$0.3}`, `71.2{\tiny ±0.3}`, `71.2 (0.3)`, `71.2 ± 0.3` (unicode) | `value=71.2, pm=0.3, pm_style=pm\|paren\|subscript`; **what** pm means (std / stderr / CI) is not guessed here, and is labelled later from the caption/text (§6) |
| Percent `71.2\%`; thousands `1{,}024` / `1,024`; minus `$-$`, `−`; sci `1e-4`, `$1\times10^{-4}$`, `\num{1e-4}` (siunitx) | normalised into `number` |
| Missing values: `-`, `--`, `–`, `—`, `N/A`, `n/a`, `OOM`, `✗`, `\xmark` | `missing` set, `number = None` |
| Direction arrows in headers `$\uparrow$`, `$\downarrow$`, `↑`, `↓` | `style.arrow` = up/down (a hint for metric direction) |
| Footnote marks `*`, `†`, `‡`, `$^\dagger$`, `\textsuperscript{}` | `superscript_marks`; not part of the number |
| Rules `\hline`, `\toprule`, `\midrule`, `\bottomrule`, `\cline{a-b}`, `\cmidrule(lr){a-b}`, `\addlinespace` | recorded in `rules`; used to separate headers from body |
| Environments `tabular`, `tabular*`, `tabularx`, `longtable` (with `\endhead`/`\endfirsthead` repeated headers removed), `threeparttable`, `table*`, `sidewaystable`, `subtable` / `\subfloat` (several tabulars per float), wrappers `\resizebox`, `\scalebox`, `adjustbox` | each tabular → one `ParsedTable` |
| Zero-argument macros from the preamble (`\newcommand{\lora}{LoRA}`, `\def\ours{...}`) | expanded in `text` only; `raw`/spans keep the original bytes; the macro table is recorded |
| Macros with arguments, unknown macros | left as-is in `text`; a warning is recorded |
| Trailing `&`, short rows | padded with `empty` cells; warning |
| Table contents in an `\input` file | parsed in that file's blob; `table_span` in the float's file, `tabular_span` in the input's file (both recorded with their `source_id`) |
| Table as `\includegraphics` | `ParsedTable` with zero cells and the warning `image_table`; skipped |
| Transposed tables (methods as columns) | not detected here; the LLM labels row vs column roles (§6.2) |

Malformed input never raises out of the parser. It yields a partial table plus warnings,
or a `parse_failed` stage record with the reason.

### 5.4 Results-table selection
Every table is parsed. A mechanical score (share of numeric cells, caption keywords such
as "accuracy", "results", "GLUE", "rank") orders them, but **no table is dropped
mechanically**. The LLM labelling step marks non-result tables (hyperparameter tables,
dataset statistics) as such, and that decision is recorded too.

## 6. Claim extraction (`src/themis/extract/`)

### 6.1 Layered evidence (Q4)
Claims are built in two layers, so that every field resolves to evidence:

1. **Atoms: `cited` nodes, one per evidence piece, no interpretation.**
   - *Cell atom*: `TableCell` locator; content `{"type": "table_cell", "raw", "number",
     "style", "missing", "parser_version"}`. Content is mechanical (from §5), never from
     the LLM.
   - *Span atom*: `Span` locator on prose; content `{"type": "text_span", "section":
     <section title verbatim>}`. Used for regime details and for numbers found in prose.
   Atoms are created only for cells and spans that some claim uses.
2. **Claims: `derived` nodes** whose parents are the atoms, with `rule:
   "table_result"` or `"prose_result"`. A claim is Themis's *interpretation*: that this
   row is method X on dataset Y. That makes it an inference over cited evidence, which is
   what `derived` means. Every field in the claim names the parent it came from, and a
   claim-schema validator checks that each `from` is among the node's parents.

### 6.2 Claim schema (content of a claim node)
```
ResultClaim  (output_schema_version 1)
  type: "result"
  evidence_grade: "table_cell" | "prose_span"      # numbers from prose rank lower (invariant 5)
  value:      {number, pm, pm_meaning: "std"|"stderr"|"ci95"|"range"|"unknown",
               percent: bool, from: <atom id>}
  method:     {text: <verbatim>, from: <atom id>}  # e.g. row header cell "LoRA (r=8)"
  task:       {text, from}                          # dataset / benchmark, verbatim
  metric:     {text, direction: "higher"|"lower"|"unknown", from}
  base_model: {text, from} | null
  setting:    [{text, from}]                        # other header-path qualifiers verbatim
  regime:     {field: {value, unit|null, text, from}}  # see 6.4; only fields found
  is_best_marked: bool                              # bold/underline in the paper
  source_table: {label, caption_from: <atom id>} | null
```
`text` fields are verbatim from the source, checked as substrings of their atom's text.
Normalising "LoRA (r=8)" to an entity happens in M2, not here.

### 6.3 Table labelling (LLM, role `extractor`)
Input: the caption, the `\label`, the surrounding paragraph that references the table
(found mechanically via `\ref{label}`), and a **rendered grid**: each cell printed with its
`(row, col)` and `text`, header paths shown.

Output (JSON schema `TableLabels`, validated):
```
is_results_table: bool, reason_cell_or_quote
orientation: "methods_as_rows" | "methods_as_columns"
header_rows: [int], label_cols: [int]
col_roles: [{col, role: task|metric|setting|method|base_model|other}]
row_roles: [{row, role: ...}]                     # same, for transposed tables
pm_meaning: std|stderr|ci95|range|unknown, pm_evidence: {quote} | null
metric_direction: [{col_or_row, direction}]
result_cells: [{row, col}] | "all_numeric_body"
```
The LLM refers to cells **only by coordinates**, and any quote must be an exact substring
of the caption or paragraph. Validation rejects out-of-range coordinates, unknown quotes,
and roles on covered cells. Then one repair retry, then the next fallback model. A table that
still fails is recorded as `label_failed` with the last error.

Claim assembly is then **mechanical**: for each result cell, take its row's label cells,
its column's header path, apply the roles, and build the claim with `from` pointers.

### 6.4 Regime details (LLM, role `extractor`)
Sections are located mechanically (`\section`, `\subsection`, `\paragraph` titles; plus
appendix sections such as "Hyperparameters" or "Implementation details"). The LLM gets
those sections' text and returns:

```
[{field, value, unit, quote, scope: "global" | {"table_label": ...} | {"method": <verbatim>}}]
```
with fields drawn from a fixed list: `base_model, lora_rank, lora_alpha, lora_dropout,
target_modules, learning_rate, batch_size, epochs, max_steps, warmup, optimizer,
scheduler, quantization_bits, trainable_params, n_seeds, seq_len, hardware`.

Checks, all mechanical:
- `quote` must occur in the section's bytes. Code finds its byte offsets, and that span
  becomes the span atom. Multiple occurrences take the one in the named section, else
  reject.
- Numeric `value`s must appear in `quote` (after the same number normalisation as §5).
  A rank of 8 must literally come from text containing "8".
- Regime entries attach to claims by scope; a claim never gets a regime field whose
  scope doesn't match.

Hyperparameter **tables** go through §5–6.3 like results tables (labelled
`is_results_table: false`, `hyperparameter_table: true`), and their cells become regime
atoms with `evidence_grade: table_cell`.

### 6.5 Numbers in prose
"LoRA reaches 71.2 on MNLI" with no table: the LLM may return a `prose_result` with
quote-anchored fields. Each field becomes a span atom, the claim is `evidence_grade:
"prose_span"`, and if the same number also appears in a table cell, the table claim
wins and the prose claim is not created.

### 6.6 Untrusted input
Paper text is untrusted input to the LLM (prompt injection). The LLM has no tools, and its
output is accepted only when it validates against the schema and every reference resolves
to coordinates or verbatim bytes. Free text from the LLM never reaches a node.

## 7. Resumability and concurrency

### 7.1 Stages
`search → resolve (dedupe) → fetch → unpack → parse → label → regime → assemble`

Each stage is a function `run(stage, limit=None)` that:
1. Derives its **work list from the ledger**: e.g. `parse` = source files with no
   `stage_records` row for `(parse, source_id, parser_version)`.
2. Processes one item at a time. Each item is **one transaction** (`tx()` with `BEGIN
   IMMEDIATE`): its nodes, observations and stage record commit together or not at all.
   A crash mid-item leaves nothing behind.
3. LLM calls happen *before* the item's write transaction and are cached as they return,
   so a crash after a call but before the commit costs no quota on re-run.

```sql
stage_records(stage, input_key, version, status CHECK IN ('done','skipped','failed'),
              output_ref, detail_json, created_at,
              PRIMARY KEY (stage, input_key, version))   -- append-only
```
- `done` / `skipped` (e.g. `no_latex_source`, `not_results_table`) are never retried.
- `failed` (e.g. `parse_failed`, `label_failed`) is not retried automatically; it is retried
  with `--retry-failed` or when the stage's `version` bumps (new parser or prompt version).
- **Quota exhaustion writes no record.** The item stays pending.

### 7.2 Quota stop
When `QuotaExhausted` reaches a stage loop, the loop finishes nothing more, keeps what is
committed, prints `stopped: <role> quota exhausted; earliest reset <time>; <n> items
pending`, and the CLI exits with code **75** (`EX_TEMPFAIL`). `themis run` the next day
picks up exactly the pending items.

Test: a fake provider that allows k calls then returns a daily-quota 429. Run to the stop,
restart with a fresh process, and assert that (a) no item is duplicated, (b) zero calls
are repeated (cache hits), (c) the final ledger is byte-identical to an uninterrupted run.

### 7.3 Concurrency
M1 runs **one writer process**. HTTP fetches and LLM calls may run in a small thread
pool (bounded by config), but only the main thread writes to SQLite, each item in its own
`BEGIN IMMEDIATE` transaction, so check-then-insert is never split (§2.2). A second
`themis` process against the same DB waits on `busy_timeout` and then fails clearly,
rather than racing.

### 7.4 CLI (`typer`)
`themis search | resolve | fetch | parse | label | regime | assemble | run [--until-quota]
| status | eval`. `status` prints per-stage pending/done/failed counts, quota state per
role entry, and cache hit rate.

## 8. Evaluation (`evals/m1/`)

### 8.1 Papers (Q5)
Five LoRA-family papers with LaTeX source and varied table styles. Proposal:
LoRA (2106.09685), QLoRA (2305.14314), AdaLoRA (2303.10512), DoRA (2402.09353),
VeRA (2310.11454). The arXiv **version and e-print sha256 are pinned** in the gold
files; the eval fetches them and refuses to score if the hash differs. Paper sources are
not committed to the repo.

**Split:** 2 *dev* papers may be inspected while iterating on prompts and parser, and 3
*test* papers are labelled up front but not looked at by whoever tunes prompts until the
final measurement. The bar applies to the test 3 *and* to all 5. (Q6)

### 8.2 Hand-labelling procedure
1. The labeller works from the **PDF and LaTeX of the pinned version**, without seeing
   any Themis output, so gold isn't anchored to the parser's view.
2. For each results table (and each prose result the labeller judges a reported
   result), record one gold claim per reported number in `evals/m1/gold/<arxiv_id>.jsonl`:
   `table` (label or "Table 3"), `value`, `pm`, `pm_meaning`, `method`, `task`, `metric`,
   `direction`, `base_model`, `setting`, `is_best_marked`, and the regime fields the paper
   states for it. Verbatim strings as printed.
3. Scope rule, written into the gold file header: which tables count as results tables,
   and whether averaged columns ("Avg.") are claims (proposed: yes).
4. A second pass a day later by the same labeller on 1 table per paper, to estimate
   label noise. If self-agreement is < 95% on a field, that field's definition gets fixed
   before scoring.
5. Gold files are committed, along with a `README.md` holding the field definitions.

### 8.3 Scoring
- **Matching:** each gold claim is matched to at most one predicted claim from the same
  table with equal `value` (within the printed precision), then by best row/column text
  overlap. One-to-one, greedy by score.
- **Field accuracy** = correct fields / total gold fields, micro-averaged over all gold
  claims. An unmatched gold claim counts *all* its fields wrong, so misses hurt.
  - Numbers: equal at the printed precision.
  - Verbatim strings: equal after whitespace collapse and casefold.
  - Enums (`direction`, `pm_meaning`): exact.
  - Regime fields: value and unit equal.
- **Bar: field accuracy ≥ 0.80**, as in CLAUDE.md.
- Also reported (no bar, but tracked): claim precision (predicted claims with no gold
  match), per-field accuracy, per-paper accuracy, provenance validity (must be 100%,
  enforced by the ledger anyway), cache hit rate and LLM calls used.
- `themis eval m1` prints a table and writes `evals/m1/results/<date>.json`, committed with
  the run's config hash, so scores are reproducible from the cache.

## 9. Package layout (new)
```
config/providers.yaml, config/search.yaml
src/themis/ledger/migrations/0001_m0.sql, 0002_m1.sql
src/themis/config.py            # .env + yaml loading, validation
src/themis/router/              # client.py, cache.py, quota.py, errors.py
src/themis/ingest/              # http.py (cached fetcher), arxiv.py, openalex.py,
                                # s2.py, papers.py (dedupe), source.py (e-print, unpack)
src/themis/extract/             # latex.py (tokeniser, sections, macros), tables.py,
                                # numbers.py, label.py, regime.py, claims.py, prompts/
src/themis/pipeline.py          # stages + stage_records
src/themis/cli.py
evals/m1/                       # gold/, README.md, score.py, results/
tests/router, tests/ingest, tests/extract, tests/pipeline, tests/fixtures/latex/
```

## 10. Tests (offline by default)
No test touches the network. HTTP is faked with `httpx.MockTransport`; the LaTeX fixtures
are small synthetic files written for each messy case in §5.3 (no copyrighted paper
text). Tests marked `@pytest.mark.network` are opt-in and skipped in CI.

Router and ledger additions are **test-first**, as for M0. Key tests:
- Critic/reasoner family overlap anywhere in the fallback lists → config error.
- Disabled entry for a blank key; role with zero enabled entries → error.
- Fallback on 429-long-wait, 5xx, context-length, invalid JSON (after one repair).
- Backoff honours `Retry-After` and Gemini `RetryInfo`; never sleeps past `max_backoff_s`
  (time is injected, not real).
- `QuotaExhausted` carries the earliest reset; exhausted entries are skipped without a
  request until reset.
- Cache key: stable across key order; differs by model / temperature / schema version;
  excludes auth headers. Cache-first across the fallback list. Failed responses aren't cached.
- Outbound host allowlist blocks other hosts; the API key is never in a URL, log line or blob.
- Migrations: 0001 → 0002 upgrade on an M0 DB keeps every M0 row byte-identical; the
  integrity check passes after upgrade; no migration contains a destructive statement;
  all new tables are append-only (parametrised like M0).
- Concurrency: two threads adding the same node both get its id (§2.2).
- Tar safety: traversal, absolute path, symlink, oversize → rejected; nothing written to disk.
- Every §5.3 row has a fixture; every cell span's bytes equal `raw`; every number parse.
- Label/regime validators: out-of-range coordinates, non-verbatim quote, a number not in
  its quote, a `from` not among the parents → rejected.
- Resumability test from §7.2.

## 11. Dependencies (need your approval)

| Package | Why |
|---|---|
| `httpx` | HTTP for all providers and APIs; `MockTransport` gives offline tests without another dep. Already in CLAUDE.md's stack. |
| `typer` | The CLI (`themis ...`). Already in CLAUDE.md's stack. |
| `PyYAML` | Reading `config/providers.yaml` and `config/search.yaml`; the stdlib has no YAML parser. (Q7: or switch to TOML and use stdlib `tomllib`, which needs no dependency.) |

Considered and **not** proposed:
`python-dotenv` (a 20-line stdlib parser suffices), `tenacity` (backoff is ~40 lines and
needs custom 429 handling anyway), `feedparser` / `defusedxml` (stdlib `ElementTree`
with Python 3.11's expat), `pylatexenc` / `TexSoup` (the tabular tokeniser needs exact
byte offsets and custom span handling; a general LaTeX parser adds little; revisit if the
eval shows tokeniser failures, Q8), `respx` (`httpx.MockTransport` covers it),
`rapidfuzz` (no fuzzy matching in M1), `numpy` (embeddings unused until M2).

## 12. Open questions

1. **`TableCell.cell` span (§2.3).** Make the cell's own byte span a required part of the
   `TableCell` locator, so cell provenance is verifiable without trusting the parser?
   I recommend yes. No `table_cell` nodes exist yet, so it's free to change now and
   expensive later.
2. **Model families.** Declaring `family` per entry in config, with a startup check that critic
   and reasoner never share one across any fallback, is my proposal. OK, or do you want a
   stricter rule (e.g. different *provider* too)?
3. **Semantic Scholar.** Enrichment only, enabled when the key is set. Or leave it out of M1
   entirely, since OpenAlex + arXiv cover search?
4. **Claims as `derived` over `cited` atoms (§6.1).** The alternative is a single `cited`
   node on the value cell, with the header/regime references only inside `content`, where
   the DB can't verify them. I recommend derived-over-atoms: every field is checked
   provenance, and "this row means LoRA on MNLI" is honestly Themis's interpretation. Downside:
   more nodes (roughly 3–6 atoms per claim, shared between claims from the same table).
5. **Eval papers.** LoRA, QLoRA, AdaLoRA, DoRA, VeRA: fine, or do you want specific ones
   (e.g. something with messier tables, like a large benchmark-style comparison)?
6. **Who labels, and the split.** I assume you hand-label the gold (I can draft the field
   definitions and the template, but I shouldn't produce gold I'm then scored against). Is the 2 dev /
   3 test split right, with the 80% bar on both test and all 5?
7. **YAML or TOML** for config? CLAUDE.md says `providers.yaml`; TOML would need no
   dependency. If TOML, I'd update CLAUDE.md to match.
8. **LaTeX parsing.** OK to start with a stdlib tokeniser and only add `pylatexenc` if the
   eval shows the tokeniser is the bottleneck?
9. **Quota events in the ledger.** `quota_events` is operational state, not evidence. I put it in
   the ledger DB (append-only) so one file holds everything and resumption needs nothing
   else. Or would you rather keep operational tables in a separate `data/state.db`?
10. **Prose results.** Include `prose_result` claims in M1 (scored in the eval), or tables only
    for M1 and prose later?
