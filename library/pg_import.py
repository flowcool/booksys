
from __future__ import annotations

from typing import Any, Callable


from library.primitives import LibraryError
from library.postgres import Database






def target_identity(database: Database) -> dict[str,Any]:
    with database.connection() as db:
        row=db.execute('SELECT system_identifier::text,current_database(),'
            '(SELECT oid::bigint FROM pg_database WHERE datname=current_database()),'
            'current_setting(\'server_version_num\')::int FROM pg_control_system()').fetchone()
    if row is None or row[3]<180006:raise LibraryError('Qualified PostgreSQL target required')
    return {'system_identifier':row[0],'database':row[1],'database_oid':row[2],
            'schema':database.schema,'server_version':row[3]}
