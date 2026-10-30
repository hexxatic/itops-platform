-- A proposed change holds only what it changes ({name?, fields}; a null value clears the
-- field), applied onto the record as it is when approved. NULL on a change proposed before:
-- those hold the whole record as it was then.
ALTER TABLE assets.proposals ADD changes nvarchar(max) NULL;
