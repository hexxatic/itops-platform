-- Scheduled module jobs (ADR 0014): module housekeeping on a schedule (e.g. noticing that
-- a contract's renewal came due). One row per run.
CREATE TABLE itops.job_runs (
    id int IDENTITY(1,1) NOT NULL CONSTRAINT pk_job_runs PRIMARY KEY,
    job nvarchar(100) NOT NULL,               -- '<module>.<name>'
    started_at datetime2(3) NOT NULL CONSTRAINT df_job_runs_started DEFAULT sysutcdatetime(),
    finished_at datetime2(3) NULL,
    status nvarchar(20) NOT NULL,             -- 'running' | 'succeeded' | 'failed'
    summary nvarchar(500) NULL
);
GO
CREATE INDEX ix_job_runs_job ON itops.job_runs (job, id DESC);
