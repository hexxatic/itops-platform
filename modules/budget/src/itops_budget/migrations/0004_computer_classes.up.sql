-- Computer classes: prices are per class (Standard or Engineering, laptop or desktop). A
-- laptop or desktop price from before is the standard one.
ALTER TABLE budget.prices DROP CONSTRAINT ck_prices_category;
GO
UPDATE budget.prices SET category = N'standard_laptop' WHERE category = N'laptop';
UPDATE budget.prices SET category = N'standard_desktop' WHERE category = N'desktop';
GO
ALTER TABLE budget.prices ADD CONSTRAINT ck_prices_category CHECK (category IN (
    'standard_laptop', 'standard_desktop', 'engineering_laptop', 'engineering_desktop', 'kit'));
