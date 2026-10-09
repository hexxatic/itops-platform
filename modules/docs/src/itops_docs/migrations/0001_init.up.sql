-- Docs data model. Core rule (carried over from Loretome): documents hold identity,
-- versions hold content; versions are immutable; publishing is a pointer move.

CREATE TABLE docs.collections (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_collections PRIMARY KEY,
    slug nvarchar(100) NOT NULL CONSTRAINT uq_collections_slug UNIQUE,
    name nvarchar(200) NOT NULL,
    description nvarchar(1000) NOT NULL CONSTRAINT df_collections_description DEFAULT N'',
    sort_order int NOT NULL CONSTRAINT df_collections_sort DEFAULT 0
);
GO

CREATE TABLE docs.documents (
    id uniqueidentifier NOT NULL CONSTRAINT pk_documents PRIMARY KEY
        CONSTRAINT df_documents_id DEFAULT newsequentialid(),
    collection_id int NOT NULL CONSTRAINT fk_documents_collection
        REFERENCES docs.collections (id),
    title nvarchar(300) NOT NULL,
    slug nvarchar(300) NOT NULL CONSTRAINT uq_documents_slug UNIQUE,
    status nvarchar(20) NOT NULL CONSTRAINT df_documents_status DEFAULT N'draft'
        CONSTRAINT ck_documents_status CHECK (status IN (N'draft', N'published', N'archived')),
    current_version_id uniqueidentifier NULL,  -- the published version (FK added below)
    review_interval_days int NOT NULL CONSTRAINT df_documents_review DEFAULT 365,
    next_review_at date NULL,
    created_by nvarchar(200) NOT NULL,
    created_at datetime2 NOT NULL CONSTRAINT df_documents_created DEFAULT sysutcdatetime(),
    updated_at datetime2 NOT NULL CONSTRAINT df_documents_updated DEFAULT sysutcdatetime()
);
GO

-- Immutable. Every save is a new row; publish stamps published_at and repoints
-- documents.current_version_id. published_at NULL = draft.
CREATE TABLE docs.versions (
    id uniqueidentifier NOT NULL CONSTRAINT pk_versions PRIMARY KEY
        CONSTRAINT df_versions_id DEFAULT newsequentialid(),
    document_id uniqueidentifier NOT NULL CONSTRAINT fk_versions_document
        REFERENCES docs.documents (id) ON DELETE CASCADE,
    version_number int NOT NULL,
    content nvarchar(max) NOT NULL  -- sanitized block JSON (itops.sdk.content)
        CONSTRAINT ck_versions_content_json CHECK (ISJSON(content) = 1),
    change_note nvarchar(500) NOT NULL CONSTRAINT df_versions_note DEFAULT N'',
    author_user nvarchar(200) NOT NULL,
    author_via_ai bit NOT NULL CONSTRAINT df_versions_ai DEFAULT 0,
    author_client nvarchar(50) NOT NULL,  -- 'web' | 'mcp' | 'import'
    created_at datetime2 NOT NULL CONSTRAINT df_versions_created DEFAULT sysutcdatetime(),
    published_at datetime2 NULL,
    CONSTRAINT uq_versions_number UNIQUE (document_id, version_number)
);
GO

ALTER TABLE docs.documents ADD CONSTRAINT fk_documents_current_version
    FOREIGN KEY (current_version_id) REFERENCES docs.versions (id);
GO

CREATE TABLE docs.document_tags (
    document_id uniqueidentifier NOT NULL CONSTRAINT fk_tags_document
        REFERENCES docs.documents (id) ON DELETE CASCADE,
    tag nvarchar(100) NOT NULL,
    CONSTRAINT pk_document_tags PRIMARY KEY (document_id, tag)
);
GO

CREATE INDEX ix_documents_collection ON docs.documents (collection_id, status);
GO
CREATE INDEX ix_document_tags_tag ON docs.document_tags (tag);
GO

-- Neutral starter collections (deployment-specific ones are added in the UI).
INSERT INTO docs.collections (slug, name, description, sort_order) VALUES
    (N'general', N'General', N'Anything that does not fit elsewhere yet.', 100),
    (N'procedures', N'Procedures', N'Step-by-step runbooks and how-tos.', 10),
    (N'infrastructure', N'Infrastructure', N'Servers, storage, virtualization.', 20),
    (N'networking', N'Networking', N'Network design, VLANs, firewalls, VPN.', 30),
    (N'applications', N'Applications', N'Line-of-business apps and how they are run.', 40),
    (N'vendors', N'Vendors', N'Vendor contacts, contracts, portals.', 50);
