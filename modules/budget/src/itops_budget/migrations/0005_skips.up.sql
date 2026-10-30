-- Computers on the replacement list a person skipped to the next year, with why: kept for
-- one budget year (``year``), so next year they're back on the list, marked as skipped.
CREATE TABLE budget.skips (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_skips PRIMARY KEY,
    asset_id nvarchar(100) NOT NULL,
    name nvarchar(200) NOT NULL,
    year int NOT NULL,
    reason nvarchar(500) NOT NULL,
    due nvarchar(500) NOT NULL, -- why it was on the list when it was skipped
    skipped_by nvarchar(200) NOT NULL,
    skipped_at datetime2(3) NOT NULL CONSTRAINT df_skips_at DEFAULT sysutcdatetime(),
    CONSTRAINT uq_skips_asset_year UNIQUE (asset_id, year)
);
