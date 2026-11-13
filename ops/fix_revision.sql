UPDATE meta SET global_revision = global_revision +
    (SELECT count(*) FROM commit_receipts WHERE catalogue AND state='committed');
