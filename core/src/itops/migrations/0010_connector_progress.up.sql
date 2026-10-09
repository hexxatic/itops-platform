-- A running connector's current step and how far it got (JSON: step, number, done, total),
-- saved every few seconds so its page can show progress.
ALTER TABLE itops.connector_runs ADD progress nvarchar(1000) NULL;
