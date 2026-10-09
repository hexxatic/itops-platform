-- Document types (ADR 0008). The type itself is defined in the core type builder; Docs
-- stores which type a document has, and its field values per version (versioned with the
-- content, so field edits are drafts too).

ALTER TABLE docs.documents ADD type_key nvarchar(60) NULL;
GO
ALTER TABLE docs.versions ADD fields nvarchar(max) NULL
    CONSTRAINT ck_versions_fields_json CHECK (fields IS NULL OR ISJSON(fields) = 1);
GO

-- The PUBLISHED values, one row per value, so they can be queried ("contracts expiring in
-- 30 days", "every document with a 1Password reference"). Rebuilt on publish.
CREATE TABLE docs.document_fields (
    document_id uniqueidentifier NOT NULL CONSTRAINT fk_document_fields_document
        REFERENCES docs.documents (id) ON DELETE CASCADE,
    field_key nvarchar(40) NOT NULL,
    position int NOT NULL,                  -- order of values in a multi-value field
    kind nvarchar(20) NOT NULL,
    value_text nvarchar(450) NULL,          -- display form, truncated (the version holds the full value)
    value_date date NULL,
    value_number float NULL,
    CONSTRAINT pk_document_fields PRIMARY KEY (document_id, field_key, position)
);
GO
CREATE INDEX ix_document_fields_date ON docs.document_fields (field_key, value_date)
    WHERE value_date IS NOT NULL;
GO
CREATE INDEX ix_document_fields_text ON docs.document_fields (field_key, value_text);
GO
CREATE INDEX ix_documents_type ON docs.documents (type_key) WHERE type_key IS NOT NULL;
