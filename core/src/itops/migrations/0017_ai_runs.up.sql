-- AI suggestion runs (ADR 0015): the embedded LLM reads one file for one entity and
-- suggests field values, each checked against its quote; a person reviews them.
CREATE TABLE itops.ai_runs (
    id int IDENTITY(1,1) NOT NULL CONSTRAINT pk_ai_runs PRIMARY KEY,
    module nvarchar(64) NOT NULL,
    feature nvarchar(64) NOT NULL,            -- '<module>.<feature>' is what's enabled
    entity_type nvarchar(64) NOT NULL,
    entity_id nvarchar(64) NOT NULL,
    file_id uniqueidentifier NOT NULL,
    filename nvarchar(260) NOT NULL,
    status nvarchar(20) NOT NULL,             -- 'queued' | 'running' | 'done' | 'failed' | 'reviewed'
    fields nvarchar(max) NOT NULL             -- JSON: what was asked for
        CONSTRAINT ck_ai_runs_fields_json CHECK (ISJSON(fields) = 1),
    instructions nvarchar(4000) NOT NULL CONSTRAINT df_ai_runs_instructions DEFAULT '',
    suggestions nvarchar(max) NULL            -- JSON [{key, value, quote, page}] (checked ones)
        CONSTRAINT ck_ai_runs_suggestions_json CHECK (suggestions IS NULL OR ISJSON(suggestions) = 1),
    dropped int NULL,                         -- suggestions that failed the checks
    error nvarchar(1000) NULL,
    model nvarchar(200) NULL,
    accepted nvarchar(max) NULL               -- JSON {key: saved value}: what a person kept
        CONSTRAINT ck_ai_runs_accepted_json CHECK (accepted IS NULL OR ISJSON(accepted) = 1),
    created_by nvarchar(200) NOT NULL,
    created_at datetime2(3) NOT NULL CONSTRAINT df_ai_runs_created DEFAULT sysutcdatetime(),
    started_at datetime2(3) NULL,
    finished_at datetime2(3) NULL,
    reviewed_by nvarchar(200) NULL,
    reviewed_at datetime2(3) NULL
);
GO
CREATE INDEX ix_ai_runs_entity ON itops.ai_runs (module, entity_type, entity_id, id DESC);
