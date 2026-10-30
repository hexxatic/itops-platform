ALTER TABLE budget.prices DROP CONSTRAINT ck_prices_category;
GO
DELETE FROM budget.prices WHERE category IN (N'engineering_laptop', N'engineering_desktop');
UPDATE budget.prices SET category = N'laptop' WHERE category = N'standard_laptop';
UPDATE budget.prices SET category = N'desktop' WHERE category = N'standard_desktop';
GO
ALTER TABLE budget.prices ADD CONSTRAINT ck_prices_category CHECK (category IN ('laptop', 'desktop', 'kit'));
