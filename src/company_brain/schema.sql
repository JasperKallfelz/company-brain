CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS brain_users (
  subject text PRIMARY KEY, active boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS collections (
  id text PRIMARY KEY, active boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS grants (
  subject text REFERENCES brain_users(subject), collection text REFERENCES collections(id),
  PRIMARY KEY(subject, collection)
);
CREATE TABLE IF NOT EXISTS sources (
  source text NOT NULL, collection text REFERENCES collections(id),
  state text NOT NULL DEFAULT 'quarantined' CHECK(state IN ('ready','quarantined','revoked')),
  last_success_at timestamptz, error_code text,
  PRIMARY KEY(source, collection)
);
CREATE TABLE IF NOT EXISTS documents (
  id uuid PRIMARY KEY, source text NOT NULL, external_id text NOT NULL,
  collection text NOT NULL REFERENCES collections(id), title text NOT NULL,
  body text NOT NULL, url text NOT NULL, modified_at timestamptz NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}', content_hash text NOT NULL,
  embedding_model text NOT NULL, quarantined boolean NOT NULL DEFAULT false,
  UNIQUE(source, external_id),
  FOREIGN KEY(source, collection) REFERENCES sources(source, collection)
);
CREATE TABLE IF NOT EXISTS chunks (
  document_id uuid REFERENCES documents(id) ON DELETE CASCADE, ordinal integer NOT NULL,
  body text NOT NULL, embedding vector(384) NOT NULL,
  fts tsvector GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED,
  PRIMARY KEY(document_id, ordinal)
);
CREATE INDEX IF NOT EXISTS chunks_fts ON chunks USING gin(fts);
CREATE INDEX IF NOT EXISTS documents_collection ON documents(collection);
CREATE TABLE IF NOT EXISTS proposals (
  id uuid PRIMARY KEY, subject text NOT NULL REFERENCES brain_users(subject),
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  base_hash text NOT NULL, proposed_text text NOT NULL, proposal_hash text NOT NULL, rationale text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), state text NOT NULL DEFAULT 'pending',
  UNIQUE(subject, document_id, base_hash, proposal_hash)
);
