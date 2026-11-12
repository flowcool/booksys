"""Closed reader SQL capabilities. Provisioning assigns a non-login function owner."""
from psycopg import sql
from library.postgres import Database


def install(database: Database) -> None:
    namespace=sql.Identifier(database.schema)
    with database.connection(write=True) as db:
        db.execute(sql.SQL('''CREATE FUNCTION reader_accept(reference text) RETURNS jsonb
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $body$
          SELECT jsonb_build_object('grant',r.grant_key,'manifest',r.manifest,'manifest_raw',r.manifest_raw)
          FROM {s}.reader_requests r JOIN {s}.stage_tasks t ON t.key=split_part(r.grant_key,':',1)
          JOIN {s}.resource_grants g ON g.pool='stage-resources.yaml' AND g.key=r.grant_key
          JOIN {s}.decoder_children c ON c.owner=r.grant_key||'.yaml'
            AND c.name='booksys-reader-'||r.job AND NOT c.stopped
          WHERE r.job=reference AND t.status='active' AND t.stage IN ('intake','ocr','copies')
            AND (t.payload::jsonb->>'generation')::bigint=split_part(r.grant_key,':',2)::bigint
            AND (g.demand::jsonb->>'memory_mib')::bigint>=1792
            AND (g.demand::jsonb->>'cpu_millis')::bigint>=1250
            AND (g.demand::jsonb->>'disk')::bigint=1
            AND (g.demand::jsonb->>'output_bytes')::bigint>=4294967296
          $body$''').format(s=namespace))
        db.execute(sql.SQL('''CREATE FUNCTION reader_state(reference text, phase text) RETURNS text
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $body$
          SELECT j.payload FROM {s}.reader_job_state j WHERE j.job=reference AND j.kind=phase
            AND EXISTS(SELECT 1 FROM {s}.reader_requests r WHERE r.job=reference)
          $body$''').format(s=namespace))
        db.execute(sql.SQL('''CREATE FUNCTION reader_retain(reference text, phase text, value text) RETURNS void
          LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $body$
          BEGIN
            IF phase NOT IN ('intent','container','receipt','reconciled','cleanup') OR
              NOT EXISTS(SELECT 1 FROM {s}.reader_requests WHERE job=reference) THEN
              RAISE EXCEPTION 'Unknown retained reader job';
            END IF;
            IF phase='intent' AND ({s}.reader_accept(reference) IS NULL OR NOT EXISTS(SELECT 1 FROM {s}.reader_requests r WHERE r.job=reference AND value::jsonb->>'manifest_sha256'=encode(sha256(convert_to(r.manifest_raw,'UTF8')),'hex'))) THEN
              RAISE EXCEPTION 'Reader grant unavailable';
            END IF;
            IF value::jsonb->>'name' IS DISTINCT FROM 'booksys-reader-'||reference THEN
              RAISE EXCEPTION 'Reader identity mismatch';
            END IF;
            IF phase IN ('container','receipt','reconciled','cleanup') AND NOT EXISTS(
              SELECT 1 FROM {s}.reader_job_state i WHERE i.job=reference AND i.kind='intent'
              AND i.payload::jsonb->>'image'=value::jsonb->>'image'
              AND i.payload::jsonb->>'manifest_sha256'=value::jsonb->>'manifest_sha256') THEN
              RAISE EXCEPTION 'Reader intent binding differs';
            END IF;
            IF phase IN ('receipt','reconciled','cleanup') AND NOT EXISTS(
              SELECT 1 FROM {s}.reader_job_state c WHERE c.job=reference AND c.kind='container'
              AND c.payload::jsonb->>'container'=value::jsonb->>'container') THEN
              RAISE EXCEPTION 'Reader container binding differs';
            END IF;
            IF phase='cleanup' AND jsonb_typeof(value::jsonb->'removed') IS DISTINCT FROM 'boolean' THEN
              RAISE EXCEPTION 'Invalid cleanup result';
            END IF;
            INSERT INTO {s}.reader_job_state VALUES(reference,phase,value) ON CONFLICT(job,kind) DO UPDATE SET payload=
              CASE WHEN phase='cleanup' AND reader_job_state.payload::jsonb->>'container'=value::jsonb->>'container'
                AND coalesce(reader_job_state.payload::jsonb->>'removed','false')='false'
              THEN excluded.payload ELSE reader_job_state.payload END;
            IF NOT EXISTS(SELECT 1 FROM {s}.reader_job_state WHERE job=reference AND kind=phase AND payload=value) THEN
              RAISE EXCEPTION 'Retained reader phase differs';
            END IF;
          END $body$''').format(s=namespace))
        db.execute(sql.SQL("""CREATE FUNCTION diagnostic_request(reference text) RETURNS text
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $body$
          SELECT payload FROM {s}.diagnostic_requests WHERE diagnostic_requests.reference=$1
          $body$""").format(s=namespace))
        db.execute(sql.SQL("""CREATE FUNCTION diagnostic_publish(value text) RETURNS void
          LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $body$
          BEGIN
            IF jsonb_typeof(value::jsonb->'sources') IS DISTINCT FROM 'object'
              OR jsonb_typeof(value::jsonb->'observed_at') IS DISTINCT FROM 'number'
              OR EXISTS(SELECT 1 FROM jsonb_object_keys(diagnostic_publish.value::jsonb->'sources') AS role(name) WHERE name NOT IN ('controller','discovery','collection','intake','processing','copies','routing','retirement','byparr','pyload','reader-launcher')) THEN
              RAISE EXCEPTION 'Invalid diagnostic source observation';
            END IF;
            INSERT INTO {s}.compose_sources SELECT key,(diagnostic_publish.value::jsonb->>'observed_at')::double precision,entry.value FROM jsonb_each_text(diagnostic_publish.value::jsonb->'sources') AS entry(key,value) ON CONFLICT(role) DO UPDATE SET observed_at=excluded.observed_at,generation=excluded.generation;
          END $body$""").format(s=namespace))
        for name,signature in [('reader_accept','text'),('reader_state','text,text'),('reader_retain','text,text,text'),('diagnostic_request','text'),('diagnostic_publish','text')]:
            db.execute(sql.SQL('REVOKE ALL ON FUNCTION {}.{}('+signature+') FROM PUBLIC').format(namespace,sql.Identifier(name)))


def grant(database: Database, *, launcher: str, definer: str) -> None:
    """Explicit deployment prerequisite; role creation and credentials stay with migration."""
    from library.primitives import LibraryError
    if launcher==definer:
        raise LibraryError('Launcher and function owner must differ')
    with database.connection(write=True) as db:
        roles={name:(login,superuser,createrole,createdb,bypassrls) for name,login,superuser,createrole,createdb,bypassrls in db.execute(
            'SELECT rolname,rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolbypassrls FROM pg_catalog.pg_roles WHERE rolname IN (%s,%s)',(launcher,definer))}
        if launcher not in roles or definer not in roles or any(roles[definer]) or any(roles[launcher][1:]):
            raise LibraryError('Restricted launcher roles required')
        if db.execute('SELECT 1 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r ON r.oid=m.member WHERE r.rolname IN (%s,%s)',(launcher,definer)).fetchone():
            raise LibraryError('Launcher role membership forbidden')
        if db.execute("SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace JOIN pg_catalog.pg_roles r ON r.oid=c.relowner WHERE n.nspname=%s AND r.rolname IN (%s,%s)",(database.schema,launcher,definer)).fetchone():
            raise LibraryError('Reader roles must not own authority tables')
        namespace=sql.Identifier(database.schema)
        db.execute(sql.SQL('REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}').format(namespace,sql.Identifier(launcher)))
        db.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM {}').format(namespace,sql.Identifier(launcher)))
        db.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {},{}').format(namespace,sql.Identifier(launcher),sql.Identifier(definer)))
        for table in ('reader_requests','reader_job_state','stage_tasks','resource_grants','decoder_children','diagnostic_requests','compose_sources'):
            db.execute(sql.SQL('GRANT SELECT ON {}.{} TO {}').format(namespace,sql.Identifier(table),sql.Identifier(definer)))
        db.execute(sql.SQL('GRANT INSERT,UPDATE ON {}.reader_job_state TO {}').format(namespace,sql.Identifier(definer)))
        db.execute(sql.SQL('GRANT INSERT,UPDATE ON {}.compose_sources TO {}').format(namespace,sql.Identifier(definer)))
        for name,signature in [('reader_accept','text'),('reader_state','text,text'),('reader_retain','text,text,text'),('diagnostic_request','text'),('diagnostic_publish','text')]:
            db.execute(sql.SQL('ALTER FUNCTION {}.{}('+signature+') OWNER TO {}').format(namespace,sql.Identifier(name),sql.Identifier(definer)))
            db.execute(sql.SQL('GRANT EXECUTE ON FUNCTION {}.{}('+signature+') TO {}').format(namespace,sql.Identifier(name),sql.Identifier(launcher)))

        if db.execute("SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind IN ('r','p') AND (pg_catalog.has_table_privilege(%s,c.oid,'SELECT') OR pg_catalog.has_table_privilege(%s,c.oid,'INSERT') OR pg_catalog.has_table_privilege(%s,c.oid,'UPDATE') OR pg_catalog.has_table_privilege(%s,c.oid,'DELETE')) LIMIT 1",(database.schema,launcher,launcher,launcher,launcher)).fetchone():
            raise LibraryError('Launcher has forbidden effective table privileges')
        schema_privilege=db.execute("SELECT pg_catalog.has_schema_privilege(%s,%s,'CREATE')",(launcher,database.schema)).fetchone()
        if schema_privilege is None or schema_privilege[0]:
            raise LibraryError('Launcher can create schema objects')
