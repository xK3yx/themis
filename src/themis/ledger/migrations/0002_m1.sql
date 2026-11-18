-- Migration 0002: M1 ledger additions. See docs/m1-ingest-extract.md §2-§7.
-- Rule IDs continue from M0: V5b, V6b (table-cell spans), V25 (stage attempts).
-- Every table gets <table>_no_update / _no_delete / _no_overwrite (a test enforces it).

-- ------------------------------------------------------------------ TableCell cell span (§2.3)

CREATE TRIGGER nodes_validate_table_cell BEFORE INSERT ON nodes
WHEN NEW.kind = 'cited'
 AND json_extract(NEW.provenance_json, '$.locator.type') = 'table_cell'
BEGIN
  SELECT RAISE(ABORT, 'V5b: table_cell locator needs a cell span inside its table span')
   WHERE json_type(NEW.provenance_json, '$.locator.cell') IS NOT 'object'
      OR json_type(NEW.provenance_json, '$.locator.cell.start') IS NOT 'integer'
      OR json_type(NEW.provenance_json, '$.locator.cell.end') IS NOT 'integer'
      OR json_type(NEW.provenance_json, '$.locator.cell.text') IS NOT 'text'
      OR json_extract(NEW.provenance_json, '$.locator.cell.start')
         >= json_extract(NEW.provenance_json, '$.locator.cell.end')
      OR json_extract(NEW.provenance_json, '$.locator.cell.start')
         < json_extract(NEW.provenance_json, '$.locator.table.start')
      OR json_extract(NEW.provenance_json, '$.locator.cell.end')
         > json_extract(NEW.provenance_json, '$.locator.table.end');
  SELECT RAISE(ABORT, 'V6b: cell text byte length must equal end - start')
   WHERE length(CAST(json_extract(NEW.provenance_json, '$.locator.cell.text') AS BLOB))
         != json_extract(NEW.provenance_json, '$.locator.cell.end')
            - json_extract(NEW.provenance_json, '$.locator.cell.start');
END;

-- ------------------------------------------------------------------ LLM call cache (§3.5)

CREATE TABLE llm_calls (
  key                   TEXT NOT NULL PRIMARY KEY
                        CHECK (length(key) = 64 AND key NOT GLOB '*[^0-9a-f]*'),
  role                  TEXT NOT NULL
                        CHECK (role IN ('extractor', 'reasoner', 'critic', 'embedder')),
  provider              TEXT NOT NULL,
  model                 TEXT NOT NULL,
  request_blob          TEXT NOT NULL REFERENCES blobs(sha256),
  response_blob         TEXT NOT NULL REFERENCES blobs(sha256),
  prompt_template       TEXT NOT NULL,
  output_schema_version INTEGER NOT NULL CHECK (output_schema_version >= 0),
  prompt_tokens         INTEGER,
  completion_tokens     INTEGER,
  latency_ms            INTEGER,
  created_at            TEXT NOT NULL
) STRICT;

-- ------------------------------------------------------------------ HTTP fetch cache (§4.1)

CREATE TABLE http_fetches (
  key           TEXT NOT NULL PRIMARY KEY
                CHECK (length(key) = 64 AND key NOT GLOB '*[^0-9a-f]*'),
  method        TEXT NOT NULL CHECK (method = 'GET'),
  url           TEXT NOT NULL CHECK (instr(url, '@') = 0),   -- secrets (mailto) are placeholders
  status        INTEGER NOT NULL CHECK (status BETWEEN 100 AND 599),
  response_blob TEXT NOT NULL REFERENCES blobs(sha256),
  content_type  TEXT,
  fetched_at    TEXT NOT NULL
) STRICT;

-- ------------------------------------------------------------------ papers (§4.2)

CREATE TABLE paper_records (
  id           TEXT NOT NULL PRIMARY KEY
               CHECK (length(id) = 64 AND id NOT GLOB '*[^0-9a-f]*'),
  fetch_key    TEXT NOT NULL REFERENCES http_fetches(key),
  source_api   TEXT NOT NULL CHECK (source_api IN ('arxiv', 'openalex')),
  arxiv_id     TEXT,
  doi          TEXT CHECK (doi IS NULL OR doi = lower(doi)),
  openalex_id  TEXT,
  title        TEXT NOT NULL,
  first_author TEXT,
  year         INTEGER,
  record_json  TEXT NOT NULL CHECK (json_valid(record_json) AND json_type(record_json) = 'object'),
  created_at   TEXT NOT NULL,
  CHECK (arxiv_id IS NOT NULL OR doi IS NOT NULL OR openalex_id IS NOT NULL)
) STRICT;
CREATE INDEX paper_records_arxiv ON paper_records(arxiv_id) WHERE arxiv_id IS NOT NULL;
CREATE INDEX paper_records_doi ON paper_records(doi) WHERE doi IS NOT NULL;
CREATE INDEX paper_records_openalex ON paper_records(openalex_id) WHERE openalex_id IS NOT NULL;

CREATE TABLE paper_links (
  a          TEXT NOT NULL REFERENCES paper_records(id),
  b          TEXT NOT NULL REFERENCES paper_records(id),
  kind       TEXT NOT NULL CHECK (kind IN ('same_paper', 'possible_duplicate')),
  reason     TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (a, b, kind),
  CHECK (a < b)
) STRICT;

-- ------------------------------------------------------------------ pipeline progress (§7.1)

CREATE TABLE stage_records (
  stage       TEXT NOT NULL,
  input_key   TEXT NOT NULL,
  version     TEXT NOT NULL,
  attempt     INTEGER NOT NULL CHECK (attempt >= 1),
  status      TEXT NOT NULL CHECK (status IN ('done', 'skipped', 'failed')),
  output_ref  TEXT,
  detail_json TEXT NOT NULL CHECK (json_valid(detail_json) AND json_type(detail_json) = 'object'),
  created_at  TEXT NOT NULL,
  PRIMARY KEY (stage, input_key, version, attempt)
) STRICT;

CREATE TRIGGER stage_records_attempts BEFORE INSERT ON stage_records
BEGIN
  SELECT RAISE(ABORT, 'V25: stage attempts must be consecutive, starting at 1')
   WHERE NEW.attempt != 1 + coalesce(
     (SELECT max(attempt) FROM stage_records
       WHERE stage = NEW.stage AND input_key = NEW.input_key AND version = NEW.version), 0);
  SELECT RAISE(ABORT, 'V25: a new attempt is only allowed after a failed one')
   WHERE EXISTS (SELECT 1 FROM stage_records
                  WHERE stage = NEW.stage AND input_key = NEW.input_key
                    AND version = NEW.version AND attempt = NEW.attempt - 1
                    AND status != 'failed');
END;

-- ------------------------------------------------------------------ append-only (V16-V18)

CREATE TRIGGER llm_calls_no_update BEFORE UPDATE ON llm_calls
BEGIN SELECT RAISE(ABORT, 'V16: llm_calls is append-only (UPDATE rejected)'); END;
CREATE TRIGGER llm_calls_no_delete BEFORE DELETE ON llm_calls
BEGIN SELECT RAISE(ABORT, 'V17: llm_calls is append-only (DELETE rejected)'); END;
CREATE TRIGGER llm_calls_no_overwrite BEFORE INSERT ON llm_calls
WHEN EXISTS (SELECT 1 FROM llm_calls WHERE key = NEW.key)
BEGIN SELECT RAISE(ABORT, 'V18: llm call already exists'); END;

CREATE TRIGGER http_fetches_no_update BEFORE UPDATE ON http_fetches
BEGIN SELECT RAISE(ABORT, 'V16: http_fetches is append-only (UPDATE rejected)'); END;
CREATE TRIGGER http_fetches_no_delete BEFORE DELETE ON http_fetches
BEGIN SELECT RAISE(ABORT, 'V17: http_fetches is append-only (DELETE rejected)'); END;
CREATE TRIGGER http_fetches_no_overwrite BEFORE INSERT ON http_fetches
WHEN EXISTS (SELECT 1 FROM http_fetches WHERE key = NEW.key)
BEGIN SELECT RAISE(ABORT, 'V18: http fetch already exists'); END;

CREATE TRIGGER paper_records_no_update BEFORE UPDATE ON paper_records
BEGIN SELECT RAISE(ABORT, 'V16: paper_records is append-only (UPDATE rejected)'); END;
CREATE TRIGGER paper_records_no_delete BEFORE DELETE ON paper_records
BEGIN SELECT RAISE(ABORT, 'V17: paper_records is append-only (DELETE rejected)'); END;
CREATE TRIGGER paper_records_no_overwrite BEFORE INSERT ON paper_records
WHEN EXISTS (SELECT 1 FROM paper_records WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'V18: paper record already exists'); END;

CREATE TRIGGER paper_links_no_update BEFORE UPDATE ON paper_links
BEGIN SELECT RAISE(ABORT, 'V16: paper_links is append-only (UPDATE rejected)'); END;
CREATE TRIGGER paper_links_no_delete BEFORE DELETE ON paper_links
BEGIN SELECT RAISE(ABORT, 'V17: paper_links is append-only (DELETE rejected)'); END;
CREATE TRIGGER paper_links_no_overwrite BEFORE INSERT ON paper_links
WHEN EXISTS (SELECT 1 FROM paper_links WHERE a = NEW.a AND b = NEW.b AND kind = NEW.kind)
BEGIN SELECT RAISE(ABORT, 'V18: paper link already exists'); END;

CREATE TRIGGER stage_records_no_update BEFORE UPDATE ON stage_records
BEGIN SELECT RAISE(ABORT, 'V16: stage_records is append-only (UPDATE rejected)'); END;
CREATE TRIGGER stage_records_no_delete BEFORE DELETE ON stage_records
BEGIN SELECT RAISE(ABORT, 'V17: stage_records is append-only (DELETE rejected)'); END;
CREATE TRIGGER stage_records_no_overwrite BEFORE INSERT ON stage_records
WHEN EXISTS (SELECT 1 FROM stage_records
             WHERE stage = NEW.stage AND input_key = NEW.input_key
               AND version = NEW.version AND attempt = NEW.attempt)
BEGIN SELECT RAISE(ABORT, 'V18: stage record already exists'); END;
