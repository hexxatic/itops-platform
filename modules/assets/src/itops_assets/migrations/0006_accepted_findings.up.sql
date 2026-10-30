-- Disagreements a person accepted as expected (with why): hidden while the facts they were
-- judged on stay the same.
CREATE TABLE assets.accepted_findings (
    record_id uniqueidentifier NOT NULL
        CONSTRAINT fk_accepted_findings_record REFERENCES assets.records (id) ON DELETE CASCADE,
    code nvarchar(40) NOT NULL,
    facts nvarchar(400) NOT NULL,
    reason nvarchar(500) NOT NULL,
    accepted_by nvarchar(200) NOT NULL,
    accepted_at datetime2 NOT NULL CONSTRAINT df_accepted_findings_at DEFAULT sysutcdatetime(),
    CONSTRAINT pk_accepted_findings PRIMARY KEY (record_id, code)
);
