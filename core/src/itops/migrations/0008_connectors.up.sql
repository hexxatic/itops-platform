-- Connectors (ADR 0010): every flow of data in or out runs through these tables.
CREATE TABLE itops.connector_state (
    connector nvarchar(100) NOT NULL CONSTRAINT pk_connector_state PRIMARY KEY,  -- '<module>.<name>'
    enabled bit NOT NULL CONSTRAINT df_connector_state_enabled DEFAULT 0,
    changed_by nvarchar(200) NOT NULL,
    changed_at datetime2(3) NOT NULL CONSTRAINT df_connector_state_changed DEFAULT sysutcdatetime()
);
GO
CREATE TABLE itops.connector_runs (
    id int IDENTITY(1,1) NOT NULL CONSTRAINT pk_connector_runs PRIMARY KEY,
    connector nvarchar(100) NOT NULL,
    [trigger] nvarchar(20) NOT NULL,          -- 'manual' | 'schedule'
    dry_run bit NOT NULL,
    started_by nvarchar(200) NOT NULL,
    started_at datetime2(3) NOT NULL CONSTRAINT df_connector_runs_started DEFAULT sysutcdatetime(),
    finished_at datetime2(3) NULL,
    status nvarchar(20) NOT NULL,             -- 'running' | 'succeeded' | 'problems' | 'failed'
    counts nvarchar(max) NULL,                -- JSON {created, updated, unchanged, ...}
    problems nvarchar(max) NULL,              -- JSON [{item, text}]
    summary nvarchar(500) NULL
);
GO
CREATE INDEX ix_connector_runs_connector ON itops.connector_runs (connector, id DESC);
GO
-- Where a record came from: one row per (connector, source record, module).
CREATE TABLE itops.source_refs (
    id int IDENTITY(1,1) NOT NULL CONSTRAINT pk_source_refs PRIMARY KEY,
    connector nvarchar(100) NOT NULL,
    source_type nvarchar(100) NOT NULL,       -- e.g. 'configuration', 'flexible_asset:Vendors'
    source_id nvarchar(200) NOT NULL,
    module nvarchar(50) NOT NULL,
    entity_kind nvarchar(60) NOT NULL,
    entity_id nvarchar(100) NOT NULL,
    fingerprint nvarchar(64) NULL,            -- hash of the source values last applied
    first_seen datetime2(3) NOT NULL CONSTRAINT df_source_refs_first DEFAULT sysutcdatetime(),
    last_seen datetime2(3) NOT NULL CONSTRAINT df_source_refs_last DEFAULT sysutcdatetime(),
    last_run_id int NULL,
    missing_since datetime2(3) NULL,          -- gone from the source (flagged for review)
    CONSTRAINT uq_source_refs UNIQUE (connector, source_type, source_id, module)
);
GO
CREATE INDEX ix_source_refs_entity ON itops.source_refs (module, entity_kind, entity_id);
