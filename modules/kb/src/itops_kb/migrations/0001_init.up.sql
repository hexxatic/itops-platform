-- How-tos (ADR 0027). The how-tos themselves are Docs' (read through its howto services);
-- kb keeps the feedback people leave, and which version of each how-to its search
-- entries were built from.
CREATE TABLE kb.indexed (
    slug nvarchar(300) NOT NULL CONSTRAINT pk_indexed PRIMARY KEY,
    version int NOT NULL,                    -- Docs' published version number
    title nvarchar(300) NOT NULL,
    collection_slug nvarchar(100) NOT NULL,
    indexed_at datetime2 NOT NULL CONSTRAINT df_indexed_at DEFAULT sysutcdatetime()
);
GO
CREATE TABLE kb.feedback (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_feedback PRIMARY KEY,
    article_slug nvarchar(300) NOT NULL,     -- kept when the how-to is withdrawn
    helpful bit NOT NULL,
    comment nvarchar(1000) NULL,
    upn nvarchar(256) NOT NULL,              -- one answer per person per how-to
    created_at datetime2 NOT NULL CONSTRAINT df_feedback_created DEFAULT sysutcdatetime(),
    CONSTRAINT uq_feedback_person UNIQUE (article_slug, upn)
);
GO
CREATE INDEX ix_feedback_created ON kb.feedback (created_at DESC);
