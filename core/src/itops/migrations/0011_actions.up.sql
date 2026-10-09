-- Tool actions (ADR 0011): runs people start from /tools, and AI requests to run one.
CREATE TABLE itops.action_runs (
    id int IDENTITY(1,1) NOT NULL CONSTRAINT pk_action_runs PRIMARY KEY,
    action nvarchar(100) NOT NULL,                 -- '<module>.<name>'
    started_by nvarchar(200) NOT NULL,
    started_by_name nvarchar(200) NOT NULL,
    started_at datetime2(3) NOT NULL CONSTRAINT df_action_runs_started DEFAULT sysutcdatetime(),
    finished_at datetime2(3) NULL,
    status nvarchar(20) NOT NULL,                  -- running, succeeded, problems, failed, cancelled
    inputs nvarchar(max) NULL,                     -- JSON
    progress nvarchar(1000) NULL,                  -- JSON: step, number, done, total
    counts nvarchar(max) NULL,
    problems nvarchar(max) NULL,
    log nvarchar(max) NULL,
    summary nvarchar(500) NULL,
    request_id int NULL,
    cancel_requested bit NOT NULL CONSTRAINT df_action_runs_cancel DEFAULT 0
);
GO
CREATE INDEX ix_action_runs_action ON itops.action_runs (action, id DESC);
GO
-- One run at a time per action, whichever worker process starts it.
CREATE UNIQUE INDEX ux_action_runs_running ON itops.action_runs (action) WHERE status = 'running';
GO
CREATE TABLE itops.action_requests (
    id int IDENTITY(1,1) NOT NULL CONSTRAINT pk_action_requests PRIMARY KEY,
    action nvarchar(100) NOT NULL,
    inputs nvarchar(max) NULL,
    reason nvarchar(1000) NULL,
    requested_by nvarchar(200) NOT NULL,
    requested_by_name nvarchar(200) NOT NULL,
    requested_at datetime2(3) NOT NULL CONSTRAINT df_action_requests_at DEFAULT sysutcdatetime(),
    via_ai bit NOT NULL,
    status nvarchar(20) NOT NULL,                  -- pending, approved, declined
    decided_by nvarchar(200) NULL,
    decided_at datetime2(3) NULL,
    run_id int NULL
);
