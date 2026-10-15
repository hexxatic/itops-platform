-- Contracts (ADR 0014). A contract holds what stays the same from term to term; each term
-- (a period: dates, cost, seats, files) is its own row, and renewing adds the next one.
CREATE TABLE contracts.contracts (
    id uniqueidentifier NOT NULL CONSTRAINT pk_contracts PRIMARY KEY,
    name nvarchar(300) NOT NULL,
    vendor nvarchar(200) NOT NULL CONSTRAINT df_contracts_vendor DEFAULT '',
    category nvarchar(40) NOT NULL,
    owner_upn nvarchar(200) NULL,
    description nvarchar(max) NULL,
    auto_renews bit NOT NULL CONSTRAINT df_contracts_auto_renews DEFAULT 0,
    notice_days int NOT NULL CONSTRAINT df_contracts_notice_days DEFAULT 0,
    links nvarchar(max) NULL                  -- JSON {vendor: link, covers: [links]}
        CONSTRAINT ck_contracts_links_json CHECK (links IS NULL OR ISJSON(links) = 1),
    status nvarchar(20) NOT NULL              -- 'draft' (AI-made, waiting) | 'active' | 'archived'
        CONSTRAINT ck_contracts_status CHECK (status IN ('draft', 'active', 'archived')),
    created_by nvarchar(200) NOT NULL,
    created_via_ai bit NOT NULL CONSTRAINT df_contracts_via_ai DEFAULT 0,
    created_at datetime2(3) NOT NULL CONSTRAINT df_contracts_created DEFAULT sysutcdatetime(),
    updated_by nvarchar(200) NOT NULL,
    updated_at datetime2(3) NOT NULL CONSTRAINT df_contracts_updated DEFAULT sysutcdatetime(),
    CONSTRAINT ck_contracts_notice CHECK (notice_days BETWEEN 0 AND 730)
);
GO
CREATE TABLE contracts.terms (
    id uniqueidentifier NOT NULL CONSTRAINT pk_terms PRIMARY KEY,
    contract_id uniqueidentifier NOT NULL
        CONSTRAINT fk_terms_contract REFERENCES contracts.contracts (id),
    starts_on date NOT NULL,
    ends_on date NOT NULL,
    cost decimal(12, 2) NULL,
    cost_period nvarchar(10) NOT NULL CONSTRAINT df_terms_cost_period DEFAULT 'year'
        CONSTRAINT ck_terms_cost_period CHECK (cost_period IN ('month', 'year', 'once')),
    po_number nvarchar(100) NOT NULL CONSTRAINT df_terms_po DEFAULT '',
    seats_bought int NULL,
    seats_in_use int NULL,
    -- 'active' -> 'due' (notice window opened) -> 'negotiating' -> 'renewed' | 'cancelled' | 'lapsed'
    state nvarchar(20) NOT NULL CONSTRAINT ck_terms_state
        CHECK (state IN ('active', 'due', 'negotiating', 'renewed', 'cancelled', 'lapsed')),
    attachments nvarchar(max) NULL            -- JSON [{id, filename, size, mime}] (core file store)
        CONSTRAINT ck_terms_attachments_json CHECK (attachments IS NULL OR ISJSON(attachments) = 1),
    decided_by nvarchar(200) NULL,
    decided_at datetime2(3) NULL,
    decision_note nvarchar(500) NOT NULL CONSTRAINT df_terms_note DEFAULT '',
    notified nvarchar(100) NOT NULL CONSTRAINT df_terms_notified DEFAULT '',  -- reminders sent: '120,90'
    overdue_notified_on date NULL,
    created_by nvarchar(200) NOT NULL,
    created_at datetime2(3) NOT NULL CONSTRAINT df_terms_created DEFAULT sysutcdatetime(),
    CONSTRAINT ck_terms_dates CHECK (ends_on >= starts_on)
);
GO
CREATE INDEX ix_terms_contract ON contracts.terms (contract_id, starts_on DESC);
GO
CREATE INDEX ix_terms_state ON contracts.terms (state, ends_on);
