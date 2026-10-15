-- Reports (ADR 0016): datasets (columns = a record type), their rows, and report
-- definitions (a dataset + filters + what to show).
CREATE TABLE reports.datasets (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_datasets PRIMARY KEY,
    [key] nvarchar(60) NOT NULL CONSTRAINT uq_datasets_key UNIQUE,
    title nvarchar(200) NOT NULL,
    description nvarchar(1000) NOT NULL CONSTRAINT df_datasets_description DEFAULT N'',
    type_key nvarchar(40) NOT NULL,
    mode nvarchar(10) NOT NULL CONSTRAINT ck_datasets_mode CHECK (mode IN (N'synced', N'manual')),
    time_field nvarchar(40) NULL,
    retention_days int NULL,
    source nvarchar(200) NOT NULL CONSTRAINT df_datasets_source DEFAULT N'',
    created_by nvarchar(200) NOT NULL,
    created_at datetime2 NOT NULL CONSTRAINT df_datasets_created_at DEFAULT sysutcdatetime(),
    updated_by nvarchar(200) NOT NULL,
    updated_at datetime2 NOT NULL CONSTRAINT df_datasets_updated_at DEFAULT sysutcdatetime()
);
GO
CREATE TABLE reports.rows (
    id bigint IDENTITY(1, 1) NOT NULL CONSTRAINT pk_rows PRIMARY KEY,
    dataset_id int NOT NULL CONSTRAINT fk_rows_dataset REFERENCES reports.datasets (id) ON DELETE CASCADE,
    source_key nvarchar(200) NULL,
    fingerprint char(64) NULL,
    row_time datetime2 NULL,
    [values] nvarchar(max) NOT NULL,
    created_by nvarchar(200) NOT NULL,
    created_at datetime2 NOT NULL CONSTRAINT df_rows_created_at DEFAULT sysutcdatetime(),
    updated_by nvarchar(200) NOT NULL,
    updated_at datetime2 NOT NULL CONSTRAINT df_rows_updated_at DEFAULT sysutcdatetime()
);
GO
CREATE UNIQUE INDEX ux_rows_source ON reports.rows (dataset_id, source_key) WHERE source_key IS NOT NULL;
GO
CREATE INDEX ix_rows_time ON reports.rows (dataset_id, row_time);
GO
CREATE TABLE reports.definitions (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_definitions PRIMARY KEY,
    [key] nvarchar(60) NOT NULL CONSTRAINT uq_definitions_key UNIQUE,
    title nvarchar(200) NOT NULL,
    description nvarchar(1000) NOT NULL CONSTRAINT df_definitions_description DEFAULT N'',
    dataset_id int NOT NULL CONSTRAINT fk_definitions_dataset REFERENCES reports.datasets (id),
    config nvarchar(max) NULL,
    draft_config nvarchar(max) NULL,
    draft_by nvarchar(200) NULL,
    draft_via_ai bit NOT NULL CONSTRAINT df_definitions_draft_via_ai DEFAULT 0,
    created_by nvarchar(200) NOT NULL,
    created_at datetime2 NOT NULL CONSTRAINT df_definitions_created_at DEFAULT sysutcdatetime(),
    updated_by nvarchar(200) NOT NULL,
    updated_at datetime2 NOT NULL CONSTRAINT df_definitions_updated_at DEFAULT sysutcdatetime(),
    published_by nvarchar(200) NULL,
    published_at datetime2 NULL
);
