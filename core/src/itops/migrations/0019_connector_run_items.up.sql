-- What each connector run did, item by item (unchanged items are only counted): shown
-- under each outcome on the run's page, kept 30 days.
CREATE TABLE itops.connector_run_items (
    id bigint IDENTITY(1,1) NOT NULL CONSTRAINT pk_connector_run_items PRIMARY KEY,
    run_id int NOT NULL CONSTRAINT fk_connector_run_items_run
        REFERENCES itops.connector_runs (id) ON DELETE CASCADE,
    outcome nvarchar(60) NOT NULL,
    item nvarchar(300) NOT NULL,
    url nvarchar(500) NULL,                   -- the record in IT-Ops
    detail nvarchar(500) NULL                 -- e.g. what changed
);
GO
CREATE INDEX ix_connector_run_items_run ON itops.connector_run_items (run_id, outcome);
