BEGIN;
DROP TABLE IF EXISTS edition_work, variant_edition, variant_asset, selected_variant,
    artifact_asset, artifact_edition, operation_edition, edition_publication_asset,
    identity_work, identity_edition, identity_variant;
DROP TABLE IF EXISTS works, editions, variants, artifacts, operations CASCADE;
COMMIT;
