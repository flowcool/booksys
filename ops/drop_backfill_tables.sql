BEGIN;
SET LOCAL search_path TO booksys;
DROP TABLE IF EXISTS backfill_page_items, backfill_items, backfill_pages, backfill_selection, collection_backfill;
DROP TABLE IF EXISTS checkpoint_links, checkpoint_downloads, checkpoint_targets, checkpoint_members;
COMMIT;
