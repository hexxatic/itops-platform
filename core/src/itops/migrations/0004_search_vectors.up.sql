-- Semantic search (ADR 0004): one meaning vector per passage from the local model.
-- 384 dimensions = BAAI/bge-small-en-v1.5. A different-size model needs a new
-- migration (and `itops search rebuild`); search stays keyword-only until then.
ALTER TABLE itops.search_passages ADD
    embedding VECTOR(384) NULL,
    embedding_model nvarchar(100) NULL;
