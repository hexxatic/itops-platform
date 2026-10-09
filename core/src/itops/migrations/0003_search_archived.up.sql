-- Archived content stays searchable on request ("Include archived"), hidden by default.
ALTER TABLE itops.search_passages
    ADD archived bit NOT NULL CONSTRAINT df_search_archived DEFAULT 0;
