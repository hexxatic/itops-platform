-- Usage rates on a term (ADR 0014, amended 2026-10-28): what the vendor charges for each one
-- used (a B&W page, a GB), to five decimal places, and how many a month the term includes.
-- Money, so seen and changed only with contracts.costs, like the term's cost.
CREATE TABLE contracts.rates (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_rates PRIMARY KEY,
    term_id uniqueidentifier NOT NULL
        CONSTRAINT fk_rates_term REFERENCES contracts.terms (id),
    position int NOT NULL,
    item nvarchar(200) NOT NULL,
    price decimal(15, 5) NOT NULL CONSTRAINT ck_rates_price CHECK (price >= 0),
    included_per_month int NULL
        CONSTRAINT ck_rates_included CHECK (included_per_month IS NULL OR included_per_month >= 0)
);
GO
CREATE INDEX ix_rates_term ON contracts.rates (term_id, position);
GO
-- A term's cost and rates proposed by an AI connection (which never sees costs) or by someone
-- who can't see them. Nothing changes until a person holding contracts.costs applies it.
-- `data` holds what was proposed: {cost, cost_period, rates: [{item, price, included}]}.
CREATE TABLE contracts.proposals (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_proposals PRIMARY KEY,
    term_id uniqueidentifier NOT NULL
        CONSTRAINT fk_proposals_term REFERENCES contracts.terms (id),
    data nvarchar(max) NOT NULL CONSTRAINT ck_proposals_data_json CHECK (ISJSON(data) = 1),
    note nvarchar(1000) NOT NULL CONSTRAINT df_proposals_note DEFAULT '',
    status nvarchar(20) NOT NULL CONSTRAINT df_proposals_status DEFAULT 'pending'
        CONSTRAINT ck_proposals_status CHECK (status IN ('pending', 'applied', 'rejected')),
    proposed_by nvarchar(200) NOT NULL,
    proposed_via_ai bit NOT NULL CONSTRAINT df_proposals_via_ai DEFAULT 0,
    proposed_at datetime2(3) NOT NULL CONSTRAINT df_proposals_proposed DEFAULT sysutcdatetime(),
    decided_by nvarchar(200) NULL,
    decided_at datetime2(3) NULL
);
GO
CREATE INDEX ix_proposals_status ON contracts.proposals (status, term_id);
