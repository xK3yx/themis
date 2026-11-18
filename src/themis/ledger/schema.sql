-- Themis ledger schema, user_version 1. See docs/m0-ledger.md.
-- Trigger messages start with the rule ID from the spec (V1..V20).
-- This file is the single source of truth: db.connect() compares the live schema
-- against a fresh install of it and refuses to open on any difference.

BEGIN;

-- ------------------------------------------------------------------ tables

CREATE TABLE blobs (
  sha256      TEXT NOT NULL PRIMARY KEY CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
  size_bytes  INTEGER NOT NULL CHECK (size_bytes >= 0),
  media_type  TEXT NOT NULL,
  created_at  TEXT NOT NULL
) STRICT;

CREATE TABLE sources (
  id          TEXT NOT NULL PRIMARY KEY CHECK (length(id) = 64 AND id NOT GLOB '*[^0-9a-f]*'),
  blob_sha256 TEXT NOT NULL REFERENCES blobs(sha256),
  origin_json TEXT NOT NULL CHECK (json_valid(origin_json) AND json_type(origin_json) = 'object'),
  created_at  TEXT NOT NULL
) STRICT;

CREATE TABLE runs (
  id            TEXT NOT NULL PRIMARY KEY CHECK (length(id) = 64 AND id NOT GLOB '*[^0-9a-f]*'),
  manifest_blob TEXT NOT NULL REFERENCES blobs(sha256),
  results_blob  TEXT NOT NULL REFERENCES blobs(sha256),
  seeds_json    TEXT NOT NULL CHECK (json_valid(seeds_json) AND json_type(seeds_json) = 'array'),
  n_seeds       INTEGER GENERATED ALWAYS AS (json_array_length(seeds_json)) STORED,
  created_at    TEXT NOT NULL
) STRICT;

CREATE TABLE nodes (
  id              TEXT NOT NULL PRIMARY KEY CHECK (length(id) = 64 AND id NOT GLOB '*[^0-9a-f]*'),
  kind            TEXT NOT NULL CHECK (kind IN ('cited', 'measured', 'derived', 'prior')),
  schema_version  INTEGER NOT NULL CHECK (schema_version >= 1),
  content_json    TEXT NOT NULL
                  CHECK (json_valid(content_json) AND json_type(content_json) = 'object'),
  provenance_json TEXT NOT NULL
                  CHECK (json_valid(provenance_json) AND json_type(provenance_json) = 'object'),
  source_id       TEXT REFERENCES sources(id),
  run_id          TEXT REFERENCES runs(id),
  created_at      TEXT NOT NULL,
  CHECK ((kind = 'cited') = (source_id IS NOT NULL)),
  CHECK ((kind = 'measured') = (run_id IS NOT NULL))
) STRICT;

CREATE TABLE edges (
  src  TEXT NOT NULL REFERENCES nodes(id),
  dst  TEXT NOT NULL REFERENCES nodes(id),
  kind TEXT NOT NULL CHECK (kind IN ('derived_from', 'supersedes')),
  PRIMARY KEY (src, dst, kind)
) STRICT;

CREATE UNIQUE INDEX one_successor ON edges(dst) WHERE kind = 'supersedes';
CREATE INDEX edges_dst ON edges(dst, kind);

CREATE TABLE observations (
  node_id         TEXT NOT NULL REFERENCES nodes(id),
  producer_sha256 TEXT NOT NULL
                  CHECK (length(producer_sha256) = 64 AND producer_sha256 NOT GLOB '*[^0-9a-f]*'),
  producer_json   TEXT NOT NULL CHECK (
                    json_valid(producer_json)
                    AND json_type(producer_json) = 'object'
                    AND json_type(producer_json, '$.role') IS 'text'),
  observed_at     TEXT NOT NULL,
  PRIMARY KEY (node_id, producer_sha256)
) STRICT;

-- ------------------------------------------------------------------ append-only (V16-V18)

CREATE TRIGGER blobs_no_update BEFORE UPDATE ON blobs
BEGIN SELECT RAISE(ABORT, 'V16: blobs is append-only (UPDATE rejected)'); END;
CREATE TRIGGER blobs_no_delete BEFORE DELETE ON blobs
BEGIN SELECT RAISE(ABORT, 'V17: blobs is append-only (DELETE rejected)'); END;
CREATE TRIGGER blobs_no_overwrite BEFORE INSERT ON blobs
WHEN EXISTS (SELECT 1 FROM blobs WHERE sha256 = NEW.sha256)
BEGIN SELECT RAISE(ABORT, 'V18: blob already exists'); END;

CREATE TRIGGER sources_no_update BEFORE UPDATE ON sources
BEGIN SELECT RAISE(ABORT, 'V16: sources is append-only (UPDATE rejected)'); END;
CREATE TRIGGER sources_no_delete BEFORE DELETE ON sources
BEGIN SELECT RAISE(ABORT, 'V17: sources is append-only (DELETE rejected)'); END;
CREATE TRIGGER sources_no_overwrite BEFORE INSERT ON sources
WHEN EXISTS (SELECT 1 FROM sources WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'V18: source already exists'); END;

CREATE TRIGGER runs_no_update BEFORE UPDATE ON runs
BEGIN SELECT RAISE(ABORT, 'V16: runs is append-only (UPDATE rejected)'); END;
CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs
BEGIN SELECT RAISE(ABORT, 'V17: runs is append-only (DELETE rejected)'); END;
CREATE TRIGGER runs_no_overwrite BEFORE INSERT ON runs
WHEN EXISTS (SELECT 1 FROM runs WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'V18: run already exists'); END;

CREATE TRIGGER nodes_no_update BEFORE UPDATE ON nodes
BEGIN SELECT RAISE(ABORT, 'V16: nodes is append-only (UPDATE rejected)'); END;
CREATE TRIGGER nodes_no_delete BEFORE DELETE ON nodes
BEGIN SELECT RAISE(ABORT, 'V17: nodes is append-only (DELETE rejected)'); END;
CREATE TRIGGER nodes_no_overwrite BEFORE INSERT ON nodes
WHEN EXISTS (SELECT 1 FROM nodes WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'V18: node already exists'); END;

CREATE TRIGGER edges_no_update BEFORE UPDATE ON edges
BEGIN SELECT RAISE(ABORT, 'V16: edges is append-only (UPDATE rejected)'); END;
CREATE TRIGGER edges_no_delete BEFORE DELETE ON edges
BEGIN SELECT RAISE(ABORT, 'V17: edges is append-only (DELETE rejected)'); END;
CREATE TRIGGER edges_no_overwrite BEFORE INSERT ON edges
WHEN EXISTS (SELECT 1 FROM edges WHERE src = NEW.src AND dst = NEW.dst AND kind = NEW.kind)
BEGIN SELECT RAISE(ABORT, 'V18: edge already exists'); END;

CREATE TRIGGER observations_no_update BEFORE UPDATE ON observations
BEGIN SELECT RAISE(ABORT, 'V16: observations is append-only (UPDATE rejected)'); END;
CREATE TRIGGER observations_no_delete BEFORE DELETE ON observations
BEGIN SELECT RAISE(ABORT, 'V17: observations is append-only (DELETE rejected)'); END;
CREATE TRIGGER observations_no_overwrite BEFORE INSERT ON observations
WHEN EXISTS (SELECT 1 FROM observations
             WHERE node_id = NEW.node_id AND producer_sha256 = NEW.producer_sha256)
BEGIN SELECT RAISE(ABORT, 'V18: observation already exists'); END;

-- ------------------------------------------------------------------ runs (V10)

CREATE TRIGGER runs_validate BEFORE INSERT ON runs
BEGIN
  SELECT RAISE(ABORT, 'V10: run needs at least one seed')
   WHERE json_array_length(NEW.seeds_json) < 1;
  SELECT RAISE(ABORT, 'V10: run seeds must be integers')
   WHERE EXISTS (SELECT 1 FROM json_each(NEW.seeds_json) WHERE type != 'integer');
  SELECT RAISE(ABORT, 'V10: run seeds must be distinct')
   WHERE (SELECT count(DISTINCT value) FROM json_each(NEW.seeds_json))
         != json_array_length(NEW.seeds_json);
END;

-- ------------------------------------------------------------------ nodes (V3-V15)

CREATE TRIGGER nodes_validate BEFORE INSERT ON nodes
BEGIN
  -- V3: provenance has exactly the keys allowed for its kind, with the right types.
  SELECT RAISE(ABORT, 'V3: provenance has a key not allowed for this kind')
   WHERE EXISTS (
     SELECT 1 FROM json_each(NEW.provenance_json) j
      WHERE NOT (
        j.key = 'supersedes'
        OR (NEW.kind = 'cited'    AND j.key IN ('source', 'locator'))
        OR (NEW.kind = 'measured' AND j.key IN ('run', 'metric'))
        OR (NEW.kind = 'derived'  AND j.key IN ('parents', 'rule'))
        OR (NEW.kind = 'prior'    AND j.key = 'asserted_by')));
  SELECT RAISE(ABORT, 'V3: provenance is missing a required key or has a wrong type')
   WHERE (NEW.kind = 'cited'
          AND (json_type(NEW.provenance_json, '$.source') IS NOT 'text'
               OR json_type(NEW.provenance_json, '$.locator') IS NOT 'object'))
      OR (NEW.kind = 'measured'
          AND (json_type(NEW.provenance_json, '$.run') IS NOT 'text'
               OR json_type(NEW.provenance_json, '$.metric') IS NOT 'text'))
      OR (NEW.kind = 'derived'
          AND (json_type(NEW.provenance_json, '$.parents') IS NOT 'array'
               OR json_type(NEW.provenance_json, '$.rule') IS NOT 'text'))
      OR (NEW.kind = 'prior'
          AND json_type(NEW.provenance_json, '$.asserted_by') IS NOT 'text')
      OR (json_type(NEW.provenance_json, '$.supersedes') IS NOT NULL
          AND json_type(NEW.provenance_json, '$.supersedes') IS NOT 'text');
  SELECT RAISE(ABORT, 'V3: derived rule must match ^[a-z][a-z0-9_]*$')
   WHERE NEW.kind = 'derived'
     AND (json_extract(NEW.provenance_json, '$.rule') NOT GLOB '[a-z]*'
          OR json_extract(NEW.provenance_json, '$.rule') GLOB '*[^a-z0-9_]*');

  -- V4: cited references an existing source; column agrees with provenance.
  SELECT RAISE(ABORT, 'V4: source_id must equal provenance.source')
   WHERE NEW.source_id IS NOT json_extract(NEW.provenance_json, '$.source');
  SELECT RAISE(ABORT, 'V4: cited node references an unknown source')
   WHERE NEW.kind = 'cited' AND NOT EXISTS (SELECT 1 FROM sources WHERE id = NEW.source_id);

  -- V5: locator well-formed and within the source blob.
  SELECT RAISE(ABORT, 'V5: cited locator is malformed or out of bounds')
    FROM (SELECT
            json_extract(NEW.provenance_json, '$.locator.type') AS t,
            CASE json_extract(NEW.provenance_json, '$.locator.type')
              WHEN 'span' THEN json_extract(NEW.provenance_json, '$.locator')
              WHEN 'table_cell' THEN json_extract(NEW.provenance_json, '$.locator.table')
            END AS sp,
            (SELECT b.size_bytes FROM sources s JOIN blobs b ON b.sha256 = s.blob_sha256
              WHERE s.id = NEW.source_id) AS size) AS l
   WHERE NEW.kind = 'cited' AND (
         l.sp IS NULL
      OR json_type(l.sp, '$.start') IS NOT 'integer'
      OR json_type(l.sp, '$.end') IS NOT 'integer'
      OR json_type(l.sp, '$.text') IS NOT 'text'
      OR json_extract(l.sp, '$.start') < 0
      OR json_extract(l.sp, '$.start') >= json_extract(l.sp, '$.end')
      OR json_extract(l.sp, '$.end') > l.size
      OR (l.t = 'table_cell' AND (
            json_type(NEW.provenance_json, '$.locator.row') IS NOT 'integer'
         OR json_type(NEW.provenance_json, '$.locator.col') IS NOT 'integer'
         OR json_extract(NEW.provenance_json, '$.locator.row') < 0
         OR json_extract(NEW.provenance_json, '$.locator.col') < 0)));

  -- V6: span text byte length equals end - start (V7, exact bytes, is checked by the DAO).
  SELECT RAISE(ABORT, 'V6: span text byte length must equal end - start')
    FROM (SELECT CASE json_extract(NEW.provenance_json, '$.locator.type')
                   WHEN 'span' THEN json_extract(NEW.provenance_json, '$.locator')
                   WHEN 'table_cell' THEN json_extract(NEW.provenance_json, '$.locator.table')
                 END AS sp) AS l
   WHERE NEW.kind = 'cited'
     AND length(CAST(json_extract(l.sp, '$.text') AS BLOB))
         != json_extract(l.sp, '$.end') - json_extract(l.sp, '$.start');

  -- V8, V9: measured references an existing run with >= 3 seeds.
  SELECT RAISE(ABORT, 'V8: run_id must equal provenance.run')
   WHERE NEW.run_id IS NOT json_extract(NEW.provenance_json, '$.run');
  SELECT RAISE(ABORT, 'V8: measured node references an unknown run')
   WHERE NEW.kind = 'measured' AND NOT EXISTS (SELECT 1 FROM runs WHERE id = NEW.run_id);
  SELECT RAISE(ABORT, 'V9: measured node requires a run with n_seeds >= 3')
   WHERE NEW.kind = 'measured' AND (SELECT n_seeds FROM runs WHERE id = NEW.run_id) < 3;

  -- V12-V14: derived parents.
  SELECT RAISE(ABORT, 'V12: derived node needs at least 2 parents')
   WHERE NEW.kind = 'derived' AND json_array_length(NEW.provenance_json, '$.parents') < 2;
  SELECT RAISE(ABORT, 'V12: derived parents must be distinct node ids')
   WHERE NEW.kind = 'derived' AND (
         EXISTS (SELECT 1 FROM json_each(NEW.provenance_json, '$.parents') WHERE type != 'text')
      OR (SELECT count(DISTINCT value) FROM json_each(NEW.provenance_json, '$.parents'))
         != json_array_length(NEW.provenance_json, '$.parents'));
  SELECT RAISE(ABORT, 'V13: derived parent does not exist')
   WHERE NEW.kind = 'derived' AND EXISTS (
     SELECT 1 FROM json_each(NEW.provenance_json, '$.parents') j
      WHERE NOT EXISTS (SELECT 1 FROM nodes n WHERE n.id = j.value));
  SELECT RAISE(ABORT, 'V14: derived parent is superseded')
   WHERE NEW.kind = 'derived' AND EXISTS (
     SELECT 1 FROM json_each(NEW.provenance_json, '$.parents') j
       JOIN edges e ON e.dst = j.value AND e.kind = 'supersedes');

  -- V15: supersedes target exists, is not already superseded, and is not also a parent.
  SELECT RAISE(ABORT, 'V15: supersedes target does not exist')
   WHERE json_type(NEW.provenance_json, '$.supersedes') = 'text'
     AND NOT EXISTS (SELECT 1 FROM nodes
                      WHERE id = json_extract(NEW.provenance_json, '$.supersedes'));
  SELECT RAISE(ABORT, 'V15: supersedes target is already superseded')
   WHERE json_type(NEW.provenance_json, '$.supersedes') = 'text'
     AND EXISTS (SELECT 1 FROM edges
                  WHERE dst = json_extract(NEW.provenance_json, '$.supersedes')
                    AND kind = 'supersedes');
  SELECT RAISE(ABORT, 'V15: a node cannot derive from the node it supersedes')
   WHERE json_type(NEW.provenance_json, '$.supersedes') = 'text'
     AND EXISTS (SELECT 1 FROM json_each(NEW.provenance_json, '$.parents') j
                  WHERE j.value = json_extract(NEW.provenance_json, '$.supersedes'));
END;

-- Edges are derived from provenance, never written directly (V19).
CREATE TRIGGER nodes_materialise_edges AFTER INSERT ON nodes
BEGIN
  INSERT INTO edges(src, dst, kind)
    SELECT NEW.id, value, 'derived_from' FROM json_each(NEW.provenance_json, '$.parents');
  INSERT INTO edges(src, dst, kind)
    SELECT NEW.id, json_extract(NEW.provenance_json, '$.supersedes'), 'supersedes'
     WHERE json_type(NEW.provenance_json, '$.supersedes') = 'text';
END;

CREATE TRIGGER edges_backed_by_provenance BEFORE INSERT ON edges
BEGIN
  SELECT RAISE(ABORT, 'V19: edge is not backed by node provenance')
   WHERE NOT (
     (NEW.kind = 'derived_from' AND EXISTS (
        SELECT 1 FROM nodes n, json_each(n.provenance_json, '$.parents') j
         WHERE n.id = NEW.src AND j.value = NEW.dst))
     OR (NEW.kind = 'supersedes' AND EXISTS (
        SELECT 1 FROM nodes n
         WHERE n.id = NEW.src AND json_extract(n.provenance_json, '$.supersedes') = NEW.dst)));
END;

PRAGMA user_version = 1;

COMMIT;
