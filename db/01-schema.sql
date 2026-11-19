-- Memory database (docs/ARCHITECTURE.md, section 5). Run once by the Postgres container.
CREATE EXTENSION IF NOT EXISTS vector;

-- Episodic memory: the append-only decision journal.
CREATE TABLE IF NOT EXISTS journal_entries (
    id           BIGSERIAL PRIMARY KEY,
    run_id       TEXT        NOT NULL,
    known_at     DATE        NOT NULL,   -- point in time: a replay of date D reads only known_at <= D
    stage        TEXT        NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload      JSONB       NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_stage_known_at ON journal_entries (stage, known_at DESC);
CREATE INDEX IF NOT EXISTS journal_run ON journal_entries (run_id);

-- Semantic memory: research knowledge base for retrieval (filings, transcripts, news).
-- Created now so the schema is fixed; ingestion and retrieval come in a later change.
CREATE TABLE IF NOT EXISTS kb_chunks (
    id               BIGSERIAL PRIMARY KEY,
    ticker           TEXT,
    doc_type         TEXT        NOT NULL,
    fiscal_period    TEXT,
    known_at         TIMESTAMPTZ NOT NULL,
    source_url       TEXT,
    content          TEXT        NOT NULL,
    embedding_model  TEXT        NOT NULL,
    embedding        vector(1536),
    content_tsv      tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);
CREATE INDEX IF NOT EXISTS kb_embedding_hnsw ON kb_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS kb_tsv ON kb_chunks USING gin (content_tsv);
CREATE INDEX IF NOT EXISTS kb_ticker_known_at ON kb_chunks (ticker, known_at);
