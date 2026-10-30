-- Rows AI proposes for a typed-in dataset (ADR 0016). Nothing is added until a person
-- approves the batch; `rows` holds the proposed rows, already checked against the columns.
CREATE TABLE reports.proposals (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_proposals PRIMARY KEY,
    dataset_id int NOT NULL CONSTRAINT fk_proposals_dataset REFERENCES reports.datasets (id) ON DELETE CASCADE,
    [rows] nvarchar(max) NOT NULL CONSTRAINT ck_proposals_rows_json CHECK (ISJSON([rows]) = 1),
    row_count int NOT NULL,
    note nvarchar(1000) NOT NULL CONSTRAINT df_proposals_note DEFAULT N'',
    status nvarchar(20) NOT NULL CONSTRAINT df_proposals_status DEFAULT N'pending'
        CONSTRAINT ck_proposals_status CHECK (status IN (N'pending', N'approved', N'rejected')),
    proposed_by nvarchar(200) NOT NULL,
    proposed_at datetime2 NOT NULL CONSTRAINT df_proposals_proposed_at DEFAULT sysutcdatetime(),
    decided_by nvarchar(200) NULL,
    decided_at datetime2 NULL
);
GO
CREATE INDEX ix_proposals_dataset ON reports.proposals (dataset_id, status);
