-- Computers a person left out of the budget, with why (ADR 0018): their own equipment,
-- leaving soon, paid for elsewhere. Keyed by the asset's id so a re-sync keeps them.
CREATE TABLE budget.exclusions (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_exclusions PRIMARY KEY,
    asset_id nvarchar(100) NOT NULL CONSTRAINT uq_exclusions_asset UNIQUE,
    name nvarchar(200) NOT NULL,
    reason nvarchar(500) NOT NULL,
    excluded_by nvarchar(200) NOT NULL,
    excluded_at datetime2(3) NOT NULL CONSTRAINT df_exclusions_at DEFAULT sysutcdatetime()
);
