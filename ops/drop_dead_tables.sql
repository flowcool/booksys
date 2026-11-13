-- Deferred cleanup only. Do not run as part of P7a.
-- Existing tables remain intact. Confirm schema and recovery before execution.
BEGIN;
DROP TABLE IF EXISTS booksys.migration_phases;
DROP TABLE IF EXISTS booksys.migration_import;
ROLLBACK;
