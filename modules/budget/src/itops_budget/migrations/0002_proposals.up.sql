-- Changes AI proposes to the price list or the assumptions (ADR 0016). Nothing is applied
-- until a person holding budget.approve approves it; `data` holds the proposed values.
CREATE TABLE budget.proposals (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_proposals PRIMARY KEY,
    kind nvarchar(20) NOT NULL
        CONSTRAINT ck_proposals_kind
        CHECK (kind IN ('price_add', 'price_change', 'price_remove', 'assumptions')),
    price_id int NULL,
    data nvarchar(max) NOT NULL CONSTRAINT ck_proposals_data_json CHECK (ISJSON(data) = 1),
    note nvarchar(1000) NOT NULL CONSTRAINT df_proposals_note DEFAULT '',
    status nvarchar(20) NOT NULL CONSTRAINT df_proposals_status DEFAULT 'pending'
        CONSTRAINT ck_proposals_status CHECK (status IN ('pending', 'approved', 'rejected')),
    proposed_by nvarchar(200) NOT NULL,
    proposed_at datetime2(3) NOT NULL CONSTRAINT df_proposals_proposed DEFAULT sysutcdatetime(),
    decided_by nvarchar(200) NULL,
    decided_at datetime2(3) NULL
);
