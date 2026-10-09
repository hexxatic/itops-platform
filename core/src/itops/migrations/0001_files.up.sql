-- Core file store (ADR 0003): metadata here, bytes on disk, content-addressed by SHA-256.
CREATE TABLE itops.files (
    id uniqueidentifier NOT NULL CONSTRAINT pk_files PRIMARY KEY
        CONSTRAINT df_files_id DEFAULT newsequentialid(),
    module nvarchar(64) NOT NULL,           -- owning module
    read_permission nvarchar(200) NOT NULL, -- checked on every download
    sha256 char(64) NOT NULL,
    storage_path nvarchar(400) NOT NULL,    -- relative to the files root
    mime_type nvarchar(100) NOT NULL,
    size_bytes bigint NOT NULL,
    filename nvarchar(260) NOT NULL,
    entity_type nvarchar(64) NULL,          -- what claimed it (e.g. 'document'); NULL = unclaimed
    entity_id nvarchar(64) NULL,
    created_by nvarchar(200) NOT NULL,
    created_at datetime2 NOT NULL CONSTRAINT df_files_created_at DEFAULT sysutcdatetime()
);
GO
CREATE INDEX ix_files_entity ON itops.files (module, entity_type, entity_id);
