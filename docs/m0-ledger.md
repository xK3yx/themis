# M0: Ledger, validity rules, blob store

Status: **done 2026-11-18**. Approved with the decisions in §9; notes from implementation in §10.

## 1. Goal and "done when"

Build the evidence ledger every later milestone writes into: a SQLite database of
content-addressed, append-only nodes whose provenance is checked by the database itself,
plus a content-addressed blob store for raw source material.

**Done when:** every invariant in §7 has at least one test that fails when the invariant
is violated, and `uv run pytest` and `uv run ruff check .` pass.

### In scope
- `pyproject.toml` (uv, src layout, package `themis`), ruff + pytest config
- `themis.ledger`: schema, connection factory, pydantic models, canonical hashing, DAO,
  validity rules, blob store, observations
- Tests for invariants 1–3 from `CLAUDE.md`

### Out of scope (later milestones)
- Claim field schemas (what goes inside `content`): M1
- Checking a table cell's row/col against the parsed table: M1
- Run harness that *produces* runs: M3. M0 only defines the `runs` table and a minimal
  results format so `measured` nodes can be validated.
- Agreement counting over observations: later
- `contradicts` edges between competing claims: M2
- Preregistration, critic, report contract (invariants 6–8): M4/M5
- CLI commands

## 2. Package layout

```
src/themis/
  __init__.py
  ledger/
    __init__.py        public API
    schema.sql         DDL, triggers, indexes (single source of truth, package data)
    db.py              connect(): version check, pragmas, install, integrity check, tx()
    canonical.py       canonical JSON + sha256 helpers
    models.py          pydantic v2 models (discriminated union on `kind`), node_id()
    dao.py             Ledger
    blobstore.py       BlobStore
    errors.py          LedgerError hierarchy
tests/ledger/
  conftest.py  test_canonical.py  test_db.py  test_schema_raw.py
  test_blobstore.py  test_dao.py  test_admissibility.py  test_observations.py
```

## 3. Content addressing

### 3.1 Canonical JSON
`canonical_json(obj) -> bytes`:
- `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`,
  encoded UTF-8.
- Allowed types: `dict` (str keys), `list`, `str`, `int`, `float`, `bool`, `None`. Anything
  else (tuple, set, datetime, Decimal, bytes) raises `CanonicalizationError`.
- Floats: NaN/±inf rejected; `-0.0` normalised to `0.0`; otherwise shortest round-trip
  `repr`. `1` and `1.0` hash differently.
- Strings are hashed **exactly as given**. No Unicode normalisation: span text must equal
  the source bytes, so normalising would break the span check (§4.1).

### 3.2 Node ID
```
node_id = sha256(canonical_json({
    "schema_version": NODE_SCHEMA_VERSION,   # int, starts at 1
    "kind": ..., "content": ..., "provenance": ...,
}))
```
`created_at` and observations are not part of identity, so re-ingestion is idempotent.
`NODE_SCHEMA_VERSION` (hash format) is separate from the DB's `PRAGMA user_version`
(table layout). Bumping the former changes every ID by design.

Optional provenance fields that are absent (e.g. no `supersedes`) are omitted from the
hashed object, not hashed as `null`.

### 3.3 Other IDs
- Blob: `sha256(raw bytes)`.
- Source: `sha256(canonical {"blob": sha, "origin": {...}})`.
- Run: `sha256(canonical {"manifest": sha, "results": sha, "seeds": sorted seeds})`.
- Producer: `sha256(canonical producer object)`.

## 4. SQLite schema

`db.connect(path)`:
1. Refuses SQLite < **3.38** (`sqlite3.sqlite_version_info`) with `SQLiteVersionError`
   naming the found and required versions. 3.38 is the first with built-in JSON functions;
   generated columns (3.31) and STRICT tables (3.37) are covered.
2. Opens in autocommit mode (`isolation_level=None`); writes use explicit savepoints via `tx()`.
3. Pragmas: `foreign_keys=ON`, `recursive_triggers=ON`, `journal_mode=WAL`.
4. Empty DB: installs `schema.sql`, sets `user_version = 1`.
   Existing DB: integrity check (§5.2).

```sql
CREATE TABLE blobs (
  sha256      TEXT PRIMARY KEY CHECK (length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
  size_bytes  INTEGER NOT NULL CHECK (size_bytes >= 0),
  media_type  TEXT NOT NULL,
  created_at  TEXT NOT NULL
) STRICT;

CREATE TABLE sources (
  id          TEXT PRIMARY KEY CHECK (<hex64>),
  blob_sha256 TEXT NOT NULL REFERENCES blobs(sha256),
  origin_json TEXT NOT NULL CHECK (json_valid(origin_json) AND json_type(origin_json)='object'),
  created_at  TEXT NOT NULL
) STRICT;

CREATE TABLE runs (
  id            TEXT PRIMARY KEY CHECK (<hex64>),
  manifest_blob TEXT NOT NULL REFERENCES blobs(sha256),
  results_blob  TEXT NOT NULL REFERENCES blobs(sha256),
  seeds_json    TEXT NOT NULL CHECK (json_valid(seeds_json) AND json_type(seeds_json)='array'),
  n_seeds       INTEGER GENERATED ALWAYS AS (json_array_length(seeds_json)) STORED,
  created_at    TEXT NOT NULL
) STRICT;

CREATE TABLE nodes (
  id              TEXT PRIMARY KEY CHECK (<hex64>),
  kind            TEXT NOT NULL CHECK (kind IN ('cited','measured','derived','prior')),
  schema_version  INTEGER NOT NULL CHECK (schema_version >= 1),
  content_json    TEXT NOT NULL CHECK (json_valid(content_json) AND json_type(content_json)='object'),
  provenance_json TEXT NOT NULL CHECK (json_valid(provenance_json) AND json_type(provenance_json)='object'),
  source_id       TEXT REFERENCES sources(id),   -- denormalised from provenance for FK
  run_id          TEXT REFERENCES runs(id),      -- denormalised from provenance for FK
  created_at      TEXT NOT NULL,
  CHECK ((kind = 'cited')    = (source_id IS NOT NULL)),
  CHECK ((kind = 'measured') = (run_id IS NOT NULL))
) STRICT;

-- Filled ONLY by the AFTER INSERT trigger on nodes, from provenance_json.
CREATE TABLE edges (
  src  TEXT NOT NULL REFERENCES nodes(id),   -- newer node
  dst  TEXT NOT NULL REFERENCES nodes(id),   -- node it depends on / replaces
  kind TEXT NOT NULL CHECK (kind IN ('derived_from','supersedes')),
  PRIMARY KEY (src, dst, kind)
) STRICT;
CREATE UNIQUE INDEX one_successor ON edges(dst) WHERE kind = 'supersedes';
CREATE INDEX edges_dst ON edges(dst, kind);

-- Who asserted a node. One row per distinct (node, producer); re-asserting is a no-op.
CREATE TABLE observations (
  node_id         TEXT NOT NULL REFERENCES nodes(id),
  producer_sha256 TEXT NOT NULL CHECK (<hex64>),
  producer_json   TEXT NOT NULL CHECK (json_valid(producer_json) AND json_type(producer_json)='object'
                                       AND json_type(producer_json,'$.role')='text'),
  observed_at     TEXT NOT NULL,   -- first time this producer asserted this node
  PRIMARY KEY (node_id, producer_sha256)
) STRICT;
```

`producer` is an object with a required `role` (e.g. `"extractor"`, `"human"`, `"test"`) and
optional fields such as `model`, `prompt_sha256`, `code_version`. Keyed by
`(node, producer hash)` rather than appended per call, so replaying a cached pipeline
doesn't inflate future agreement counts.

### 4.1 Provenance shapes

| kind | allowed keys (others rejected) |
|---|---|
| `cited` | `source`, `locator`, `supersedes?` |
| `measured` | `run`, `metric`, `supersedes?` |
| `derived` | `parents`, `rule`, `supersedes?` |
| `prior` | `asserted_by`, `supersedes?` |

- `Span = {"type":"span","start":int,"end":int,"text":str}`: byte offsets into the exact
  stored blob, `0 <= start < end <= size_bytes`, and `blob[start:end].decode("utf-8") == text`.
- `TableCell = {"type":"table_cell","table":Span,"row":int,"col":int}`: `table` is the span of
  the whole table environment (with its text); `row`, `col` are 0-based and ≥ 0.
- `derived.rule`: identifier matching `^[a-z][a-z0-9_]*$` (e.g. `pool_mean`).
- `supersedes`: optional on every kind and part of the hash.

### 4.2 Run results format (minimal, M3 may extend)
The results blob is UTF-8 JSON: `{"metrics": {"<name>": {"<seed>": <number>, ...}, ...}}`.

## 5. Validity rules

The DAO raises `ValidationError` with a readable message. The database also rejects the
row when the DAO is bypassed. Trigger messages start with the rule ID (e.g. `V6:`), so a
failure traces back to this table.

| # | Rule | DAO | DB |
|---|---|---|---|
| V1 | `kind` is one of four | pydantic union | CHECK |
| V2 | `id` = content hash | computed by DAO; `get()`/`verify_all()` recompute and detect forgeries | hex format CHECK (no sha256 in SQLite) |
| V3 | provenance has exactly the keys allowed for its kind | `extra="forbid"` | trigger over `json_each` |
| V4 | cited references an existing source | lookup | FK + trigger `source_id = $.source` |
| V5 | cited span in bounds; table row/col ≥ 0 | check | trigger joining sources→blobs |
| V6 | span text length in UTF-8 bytes = `end - start` | — | trigger (`length(CAST(text AS BLOB))`) |
| V7 | `blob[start:end]` decodes to exactly `text` | reads blob, compares | — (blob bytes are not in the DB) |
| V8 | measured references an existing run | lookup | FK + trigger `run_id = $.run` |
| V9 | that run has `n_seeds >= 3` | check | trigger |
| V10 | run seeds are distinct integers | check | trigger on `runs` |
| V11 | `metric` has a value for every seed in the run's results | reads results blob | — |
| V12 | derived has ≥ 2 parents, no duplicates | check | trigger over `json_each` |
| V13 | every parent exists | check | trigger |
| V14 | no parent is superseded at insert time | check | trigger against `edges` |
| V15 | `supersedes` target exists, is not already superseded, and is not one of the node's own parents | check | trigger + `one_successor` index |
| V16 | no UPDATE on any ledger table | no mutator methods | `BEFORE UPDATE` → abort, all 6 tables |
| V17 | no DELETE on any ledger table | no mutator methods | `BEFORE DELETE` → abort, all 6 tables |
| V18 | no overwrite via `INSERT OR REPLACE` | DAO checks existence first | `BEFORE INSERT` aborts if the PK already exists (+ `recursive_triggers=ON`) |
| V19 | edges come only from node provenance | no edge method | edges filled by trigger; `BEFORE INSERT ON edges` rejects rows not backed by `src` provenance |
| V20 | observation producer hash matches producer | computed by DAO | `role` CHECK |

Derived nodes **may** have `prior` parents: hypotheses legitimately start from them.
Recursive admissibility (§5.1) keeps such derivations out of conclusions.

Cycles are impossible by construction: an ID depends on its parents' IDs.

Because V18 makes a raw duplicate insert abort, idempotency is a DAO property: `add_*`
methods look the row up first and return the existing ID (after checking the stored
content is byte-identical).

### 5.1 Admissibility (read-side)
`is_admissible(id)` and `why_inadmissible(id) -> list[str]` (reasons name node IDs).
Admissible iff not `prior`, not superseded, and, if derived, all parents admissible.
V14 blocks new derivations from stale parents; read-time admissibility covers parents
superseded after the derivation was written.

### 5.2 Corrections
A node is superseded at most once, so history is linear: a second correction must
supersede the first correction. `current(id)` follows the chain to its head.
Disagreement between producers isn't a correction. It's two claim nodes linked by a
`contradicts` edge, which M2 adds.

### 5.3 Schema integrity on open
On an existing DB, `connect()` checks `user_version` and compares the `sql` text of every
table, index and trigger in `sqlite_master` with a fresh in-memory install of
`schema.sql`. A dropped or altered trigger means `SchemaIntegrityError` and no connection.

## 6. API

```python
class Ledger:
    def __init__(self, conn, blobs: BlobStore)
    def add_source(self, blob_sha256: str, origin: dict) -> str
    def add_run(self, manifest: bytes, results: bytes, seeds: list[int]) -> str
    def add_node(self, node: NodeIn, producer: dict) -> str     # idempotent; records observation
    def supersede(self, old_id: str, new: NodeIn, producer: dict) -> str
    def get(self, node_id: str) -> Node                          # verifies hash
    def verify_all(self) -> list[str]                            # ids whose hash doesn't match
    def parents(self, node_id) -> list[Node]
    def successor(self, node_id) -> str | None
    def current(self, node_id) -> str
    def observations(self, node_id) -> list[Observation]
    def is_admissible(self, node_id) -> bool
    def why_inadmissible(self, node_id) -> list[str]

class BlobStore:
    def __init__(self, root: Path, conn)
    def put(self, data: bytes, media_type: str) -> str
    def get(self, sha256: str, verify: bool = True) -> bytes
    def path(self, sha256: str) -> Path
```

Blob store:
- Layout: `data/store/<first two hex chars>/<sha256>`, e.g. `data/store/ab/abcdef…`.
- `put`: write to `<root>/.tmp/<random>`, flush + `os.fsync`, `os.replace` to the final path
  (atomic on NTFS within a volume), mark read-only, then insert the `blobs` row. If the
  file already exists, only ensure the row exists.
- `get(verify=True)` re-hashes; mismatch raises `BlobCorruptError`, missing raises `BlobMissingError`.
- All paths via `pathlib`.

## 7. Tests that prove each invariant

"Raw" = bypasses the DAO via `conn.execute`, proving the DB is the enforcer.

### Invariant 1: No claim without provenance
| Test | Asserts |
|---|---|
| `test_kind_must_be_known` (raw) | `kind='opinion'` rejected |
| `test_provenance_keys_match_kind` (raw + DAO) | e.g. cited with `parents` rejected |
| `test_cited_requires_source` (raw + DAO) | missing/unknown source rejected |
| `test_cited_source_column_matches_provenance` (raw) | `source_id ≠ $.source` rejected |
| `test_cited_span_bounds` (raw + DAO) | `end > size`, `start >= end`, `start < 0` rejected |
| `test_span_text_length_checked_in_db` (raw) | text with wrong byte length rejected |
| `test_span_text_must_match_blob` (DAO) | right length, wrong bytes rejected |
| `test_span_multibyte_utf8` | span over `é`/`≥` with byte offsets accepted |
| `test_table_cell_nonnegative` | negative row/col rejected |
| `test_measured_requires_run` (raw + DAO) | missing/unknown run rejected |
| `test_measured_rejects_two_seeds` (raw + DAO) | 2 seeds rejected |
| `test_measured_accepts_three_seeds` | 3 seeds accepted |
| `test_run_seeds_distinct` (raw + DAO) | `[1,1,2]` rejected |
| `test_n_seeds_is_generated` (raw) | explicit `n_seeds` insert fails |
| `test_measured_metric_all_seeds` | metric absent, or missing for a seed, rejected |
| `test_derived_rejects_one_parent` (raw + DAO) | |
| `test_derived_rejects_duplicate_parents` (raw + DAO) | `[a, a]` rejected |
| `test_derived_rejects_missing_parent` (raw) | |
| `test_derived_rejects_superseded_parent` (raw + DAO) | |
| `test_derived_allows_prior_parent` | accepted, but inadmissible |
| `test_derived_edges_materialised` | exactly its `derived_from` edges exist |
| `test_prior_never_admissible` | |
| `test_admissibility_is_transitive` | parent superseded later → derived inadmissible |
| `test_why_inadmissible_lists_reasons` | reasons name the offending ids |

### Invariant 2: Ledger is append-only
| Test | Asserts |
|---|---|
| `test_update_rejected[table]` (raw, ×6) | |
| `test_delete_rejected[table]` (raw, ×6) | |
| `test_insert_or_replace_rejected[table]` (raw) | `INSERT OR REPLACE` can't overwrite |
| `test_dao_has_no_mutators` | no public update/delete/replace/set methods |
| `test_edges_not_insertable_directly` (raw) | unbacked edge rejected |
| `test_supersede_creates_new_node` | old row byte-identical; new node + edge exist |
| `test_supersede_once_only` (raw + DAO) | |
| `test_second_correction_supersedes_first` | A←B←C accepted; `current(A) == C` |
| `test_supersede_unknown_target` | |
| `test_altered_trigger_refuses_open` | drop/replace a trigger → `SchemaIntegrityError` |
| `test_connect_pragmas` | `foreign_keys` and `recursive_triggers` are on |
| `test_sqlite_version_too_old` | patched version 3.37.2 → `SQLiteVersionError` with clear message |

### Invariant 3: IDs are content hashes; re-ingest idempotent
| Test | Asserts |
|---|---|
| `test_canonical_key_order_irrelevant` | |
| `test_canonical_preserves_unicode_form` | NFC and NFD strings hash differently (no normalisation) |
| `test_canonical_rejects_nan_inf` | |
| `test_canonical_negative_zero` | |
| `test_canonical_rejects_non_json_types` | |
| `test_canonical_int_float_distinct` | |
| `test_id_golden_vector` | fixed node → hard-coded digest of hand-written canonical bytes |
| `test_id_includes_schema_version` | |
| `test_id_omits_absent_supersedes` | |
| `test_get_detects_forged_id` (raw insert, DAO read) | `LedgerCorruptError` |
| `test_id_format_check` (raw) | |
| `test_reinsert_idempotent` | one row, same id |
| `test_reingest_source_and_run_idempotent` | |
| `test_blob_put_idempotent` / `test_blob_address_and_shard` | path is `root/ab/ab…`, name = sha256 |
| `test_blob_get_detects_corruption` / `test_blob_missing` | |
| `test_blob_atomic_write` | failure before `os.replace` leaves nothing at final path |

### Observations
| Test | Asserts |
|---|---|
| `test_add_node_records_observation` | |
| `test_same_producer_twice_one_observation` | |
| `test_two_producers_one_node_two_observations` | |
| `test_observation_requires_role` (raw) | |
| `test_observations_append_only` (raw) | covered by the ×6 parametrisation |

## 8. Dependencies (approved)
Runtime: `pydantic>=2`. Dev: `pytest`, `ruff`. Everything else is standard library.

## 9. Decisions (2026-11-18)
1. Observations table added in M0 (table, insert path, tests). No agreement logic yet.
2. One correction per node; corrections form a linear chain. Disagreement is M2's `contradicts`.
3. Reject derivations from superseded parents on write; admissibility covers later supersessions.
4. Byte offsets into the stored blob, plus stored span text verified against the blob.
   As a consequence, canonical JSON does **not** Unicode-normalise strings.
5. Two-character blob sharding.
6. `prior` nodes stored, never admissible; allowed as derived parents.
7. SQLite ≥ 3.38 required, checked on open.

## 10. Implementation notes

- **CHECKs that evaluate to NULL pass.** `json_type(x, '$.role') = 'text'` accepts a
  missing `role`, so type checks use `IS 'text'` / `IS NOT 'text'`. Every primary key is
  declared `NOT NULL` explicitly (STRICT tables imply it, but the hex CHECK alone would not).
- **V15 extended:** a node may not list the node it supersedes among its own parents.
  Otherwise a correction would depend on what it corrects and be inadmissible from the
  moment it is written.
- **V18 means raw duplicate inserts abort**, so `INSERT ... ON CONFLICT DO NOTHING` can't be
  used. The DAO looks rows up first. Re-adding an existing node skips validation, so
  re-ingesting a derivation whose parent has since been superseded still returns its id.
- **DAO-only rules:** V7 (span bytes) and V11 (per-seed metric) need blob contents, which
  aren't in the DB. `test_db_is_the_backstop` shows that the DB-side rules still fire
  with DAO validation disabled.
- **Mutation check:** each `RAISE`, each trigger, and the key CHECKs/index in
  `schema.sql` were disabled one at a time; the suite fails for all 68 mutants. Constraints
  that sit behind a trigger have their own tests that drop the trigger first.
- Tests import shared builders from `tests/ledger/helpers.py` and raw-SQL fixtures from
  `tests/ledger/conftest.py`.
