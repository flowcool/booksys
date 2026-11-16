"""Explicit schema-1 to schema-2 conversion with all Booksys writers stopped.

No network/native/media calls. One transaction retains all original columns and
raw evidence; the final marker is selected only after a complete audit.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from psycopg import sql

from library.codec import decode, encode
from library.operational_fields import FIELDS, KEYS, values, schema, sync, records, entity_columns, entity_record, at
from library.postgres import Database, DeadlineConnection
from library.primitives import LibraryError
from library.versioned import TABLES, install_in

CHILDREN = ('asset_destinations','origin_destinations','asset_metadata','asset_admission',
    'origin_fingerprint','submission_links','routing_members','copy_operation_files')


def original_columns(db: DeadlineConnection, namespace: str) -> dict[str,list[str]]:
    result: dict[str,list[str]] = {}
    for table,column in db.execute('SELECT table_name,column_name FROM information_schema.columns '
            'WHERE table_schema=%s ORDER BY table_name,ordinal_position',(namespace,)):
        if table != 'table_changes' and not (table=='meta' and column=='schema_version'):
            result.setdefault(table,[]).append(column)
    return result


def fingerprints(db: DeadlineConnection, columns: dict[str,list[str]]) -> dict[str,dict[str,Any]]:
    result = {}
    for table,names in columns.items():
        key_names=[row[0] for row in db.execute(
            'SELECT a.attname FROM pg_index i CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,position) '
            'JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum '
            'WHERE i.indrelid=%s::regclass AND i.indisprimary ORDER BY k.position',(table,))]
        if not key_names:
            raise LibraryError(f'Conversion proof requires a stable primary key: {table}')
        sha=hashlib.sha256(); count=0
        with db.cursor(name='conversion_fingerprint') as cursor:
            cursor.execute(sql.SQL('SELECT {} FROM {} ORDER BY {}').format(
                sql.SQL(',').join(map(sql.Identifier,names)),sql.Identifier(table),
                sql.SQL(',').join(map(sql.Identifier,key_names))))
            for row in cursor:
                data=encode(list(row)).encode(); sha.update(len(data).to_bytes(8,'big')); sha.update(data); count+=1
        result[table]={'rows':count,'sha256':sha.hexdigest()}
    return result


def audit(db: DeadlineConnection) -> dict[str,int]:
    """Verify every promoted field against the retained compatibility record."""
    counts: dict[str,int] = {}
    for table,columns in FIELDS.items():
        raw_column='binding' if table=='source_inputs' else 'payload'
        names=(*KEYS[table],raw_column,*(name for name,_,_ in columns))
        count=0
        for row in db.execute(f'SELECT '+','.join(names)+f' FROM {table}'):
            raw=row[len(KEYS[table])]
            value=decode(raw) if raw is not None else {}
            if table in ('assets','origins'):
                value=value['record']
            expected=values(table,value)
            actual=row[len(KEYS[table])+1:]
            for (_,path,kind),left,right in zip(columns,expected,actual):
                if left is not None and kind=='TIMESTAMPTZ':
                    left=datetime.fromisoformat(left)
                if left != right:
                    raise LibraryError(f'Operational consistency mismatch: {table}.{path}')
            count+=1
        counts[table]=count
    for table in ('collection_submissions','routing_packages','copy_operations'):
        records(db,table)
    for kind,owner in (('assets','asset'),('origins','origin')):
        for row in db.execute(f'SELECT id,{entity_columns(kind)} FROM {kind}'):
            record=decode(row[1])['record']
            entity_record(kind,record,row[2:])
            expected_destinations=sorted((slot,at(record,slot)) for slot in ('target','destination','publication.target','admission.target','provisional.target') if at(record,slot) is not None)
            actual=db.execute(f'SELECT slot,target FROM {owner}_destinations WHERE {owner}=%s ORDER BY slot',(row[0],)).fetchall()
            if expected_destinations!=actual:
                raise LibraryError(f'Operational consistency mismatch: {owner}_destinations')
            if kind=='origins':
                fingerprint=record.get('fingerprint')
                expected_fingerprint=[tuple(fingerprint)] if isinstance(fingerprint,list) else []
                actual_fingerprint=db.execute('SELECT device,inode,size,mtime_ns,ctime_ns FROM origin_fingerprint WHERE origin=%s',(row[0],)).fetchall()
                if expected_fingerprint!=actual_fingerprint:
                    raise LibraryError('Operational consistency mismatch: origin_fingerprint')
    for child in CHILDREN:
        count_row=db.execute(f'SELECT count(*) FROM {child}').fetchone()
        assert count_row is not None
        counts[child]=count_row[0]
    # A current admission manifest is the exact referenced operation manifest.
    for asset,raw in db.execute('SELECT a.id,a.payload FROM assets a JOIN asset_admission r ON r.asset=a.id WHERE r.operation IS NOT NULL'):
        admission=decode(raw)['record']['admission']
        row=db.execute("SELECT payload FROM copy_operations WHERE namespace='admission' AND id=%s",(admission['operation'],)).fetchone()
        if row is None or admission.get('manifest') != decode(row[0]).get('manifest'):
            raise LibraryError(f'Admission manifest differs from retained operation: {asset}')
    return counts


def convert(database: Database, *, authority: str, seconds: int = 600) -> dict[str,Any]:
    with database.connection(write=True,seconds=seconds) as db:
        row=db.execute('SELECT authority_id,schema_version,global_revision FROM meta WHERE singleton=1 FOR UPDATE').fetchone()
        if row is None or row[:2] != (authority,1):
            raise LibraryError('Conversion requires the exact schema-1 authority')
        columns=original_columns(db,database.schema)
        # Lock all old tables before reading them. No observer or queue writer may
        # invalidate the copied source while conversion constructs its relations.
        for table in sorted(columns):
            db.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE').format(sql.Identifier(table)))
        before=fingerprints(db,columns)
        schema(db)
        for table in FIELDS:
            raw_column='binding' if table=='source_inputs' else 'payload'
            rows=db.execute(f'SELECT '+','.join((*KEYS[table],raw_column))+f' FROM {table}').fetchall()
            for *key,raw in rows:
                record=decode(raw) if raw is not None else {}
                if table in ('assets','origins'):
                    record=record['record']
                try:
                    sync(db,table,tuple(key),record)
                except (LibraryError,ValueError) as exc:
                    raise LibraryError(f'Conversion stopped at {table} key={key!r}: {exc}') from exc
        counts=audit(db)
        after=fingerprints(db,columns)
        if before != after:
            raise LibraryError('Conversion changed original rows or raw/hash evidence')
        install_in(db,database.schema,TABLES)
        trigger_row=db.execute("SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND t.tgname=ANY(%s) AND t.tgenabled='O'",
            (database.schema,['changes_'+name for name in TABLES])).fetchone()
        assert trigger_row is not None
        triggers=trigger_row[0]
        if triggers != len(TABLES):
            raise LibraryError('Incomplete operational change-token triggers')
        db.execute('UPDATE meta SET schema_version=2 WHERE singleton=1')
        result={'authority':authority,'schema':database.schema,'from_version':1,'to_version':2,
            'global_revision':row[2],'unchanged_original_tables':before,'operational_rows':counts,
            'change_triggers':triggers,'proof':'all original column values and raw payload hashes unchanged'}
    return result


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn-file',type=Path,required=True)
    parser.add_argument('--schema',required=True)
    parser.add_argument('--authority',required=True)
    parser.add_argument('--writers-stopped',action='store_true',required=True)
    parser.add_argument('--seconds',type=int,default=600)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    if not Database._private(args.dsn_file) or args.seconds<=0:
        raise LibraryError('Use an owned private credential file and a positive deadline')
    if args.report.exists():
        raise LibraryError('Conversion report already exists; retain its proof')
    # Reserve and test the proof destination before any database change. Keep the
    # descriptor open so a parent-directory permission change cannot lose the result.
    descriptor=os.open(args.report,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as output:
        json.dump({'status':'conversion_started','schema':args.schema,'authority':args.authority},output)
        output.write('\n'); output.flush(); os.fsync(output.fileno())
        result=convert(Database(args.dsn_file.read_text().strip(),args.schema),authority=args.authority,seconds=args.seconds)
        output.seek(0); output.truncate()
        json.dump(result,output,indent=2); output.write('\n'); output.flush(); os.fsync(output.fileno())
    print('Schema 2 committed; original values and evidence unchanged; forward recovery only after new writes')


if __name__=='__main__':
    main()
