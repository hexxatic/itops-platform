-- Datasets AI proposes (ADR 0016). Nothing is created until a person with reports.publish
-- approves; `spec` holds the proposed title, key, column type, time column and retention.
-- Rows can be proposed for a proposed dataset: they wait (dataset_id NULL) and move to the
-- dataset when it is created.
CREATE TABLE reports.dataset_proposals (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_dataset_proposals PRIMARY KEY,
    [key] nvarchar(60) NOT NULL,
    spec nvarchar(max) NOT NULL CONSTRAINT ck_dataset_proposals_spec_json CHECK (ISJSON(spec) = 1),
    note nvarchar(1000) NOT NULL CONSTRAINT df_dataset_proposals_note DEFAULT N'',
    status nvarchar(20) NOT NULL CONSTRAINT df_dataset_proposals_status DEFAULT N'pending'
        CONSTRAINT ck_dataset_proposals_status CHECK (status IN (N'pending', N'approved', N'rejected')),
    proposed_by nvarchar(200) NOT NULL,
    proposed_at datetime2 NOT NULL CONSTRAINT df_dataset_proposals_proposed_at DEFAULT sysutcdatetime(),
    decided_by nvarchar(200) NULL,
    decided_at datetime2 NULL
);
GO
DROP INDEX ix_proposals_dataset ON reports.proposals;
GO
ALTER TABLE reports.proposals ALTER COLUMN dataset_id int NULL;
GO
ALTER TABLE reports.proposals ADD dataset_proposal_id int NULL
    CONSTRAINT fk_proposals_dataset_proposal REFERENCES reports.dataset_proposals (id);
GO
CREATE INDEX ix_proposals_dataset ON reports.proposals (dataset_id, status);
