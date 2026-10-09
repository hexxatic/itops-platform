-- Record types (ADR 0008): definitions only. Modules keep their records' field values.
CREATE TABLE itops.record_types (
    id int IDENTITY(1,1) NOT NULL CONSTRAINT pk_record_types PRIMARY KEY,
    module nvarchar(50) NOT NULL,
    type_key nvarchar(60) NOT NULL,
    current_version_id int NULL,            -- the published version (NULL until first publish)
    retired bit NOT NULL CONSTRAINT df_record_types_retired DEFAULT 0,
    created_at datetime2(3) NOT NULL CONSTRAINT df_record_types_created DEFAULT sysutcdatetime(),
    created_by nvarchar(200) NOT NULL,
    CONSTRAINT uq_record_types UNIQUE (module, type_key)
);
GO
CREATE TABLE itops.record_type_versions (
    id int IDENTITY(1,1) NOT NULL CONSTRAINT pk_record_type_versions PRIMARY KEY,
    type_id int NOT NULL CONSTRAINT fk_record_type_versions_type
        REFERENCES itops.record_types (id) ON DELETE CASCADE,
    version int NOT NULL,
    definition nvarchar(max) NOT NULL,      -- JSON: name, description, fields
    note nvarchar(300) NULL,
    author_user nvarchar(200) NOT NULL,
    author_via_ai bit NOT NULL,
    author_client nvarchar(20) NOT NULL,
    created_at datetime2(3) NOT NULL CONSTRAINT df_record_type_versions_created DEFAULT sysutcdatetime(),
    published_at datetime2(3) NULL,
    published_by nvarchar(200) NULL,
    CONSTRAINT uq_record_type_versions UNIQUE (type_id, version)
);
