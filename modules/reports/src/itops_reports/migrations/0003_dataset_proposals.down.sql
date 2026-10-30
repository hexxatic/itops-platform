DELETE FROM reports.proposals WHERE dataset_id IS NULL;
GO
ALTER TABLE reports.proposals DROP CONSTRAINT fk_proposals_dataset_proposal;
GO
ALTER TABLE reports.proposals DROP COLUMN dataset_proposal_id;
GO
DROP INDEX ix_proposals_dataset ON reports.proposals;
GO
ALTER TABLE reports.proposals ALTER COLUMN dataset_id int NOT NULL;
GO
CREATE INDEX ix_proposals_dataset ON reports.proposals (dataset_id, status);
GO
DROP TABLE reports.dataset_proposals;
