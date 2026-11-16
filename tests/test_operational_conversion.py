"""Bounded offline conversion, selection, deadline and result-fence checks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql

from library.codec import encode
from library.operational_conversion import convert, audit
from library.operational_state import CollectorState, CopyOperations, OperationalCompletion, apply_completion, schema as operational_schema
from library.postgres import Database, guard_schema
from library.primitives import LibraryError
from library.store import schema as catalogue_schema


@unittest.skipUnless(os.environ.get('BOOKSYS_TEST_PG_DSN_FILE'), 'isolated PostgreSQL required')
class OperationalConversionTest(unittest.TestCase):
    def setUp(self):
        self.dsn=Path(os.environ['BOOKSYS_TEST_PG_DSN_FILE']).read_text().strip()
        self.database=Database(self.dsn,'test_'+uuid4().hex)
        self.authority='isolated-conversion'
        with self.database.connection(write=True) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.database.schema)))
            catalogue_schema(db)
            db.execute('CREATE TABLE conflict_guards(key TEXT PRIMARY KEY)')
            guard_schema(db,self.database.schema)
            db.execute('CREATE TABLE source_registry(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE source_inputs(source TEXT REFERENCES source_registry(id),entity TEXT,input_revision TEXT,method TEXT,guard TEXT,progress TEXT,binding TEXT,PRIMARY KEY(source,entity))')
            operational_schema(db)
            db.execute("INSERT INTO meta VALUES(1,%s,1,7,'source','[]',0,1,'semantic','postgresql>=18.6','2026-11-16T00:00:00+00:00')",(self.authority,))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        with psycopg.connect(self.dsn,autocommit=True) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.database.schema)))

    def test_conversion_preserves_proof_and_selects_scoped_journals(self):
        asset='a'*64
        raw=encode({'record':{'format':'cbz','size':4,'pages':1,'contested':False,
            'target':'shared','admission':{'target':'shared'},'extra':{'$booksys':True,'zero':0,'null':None,'infinity':float('inf')},
            'page_manifest':[{'name':'one','sha256':'b'*64,'size':4}]}})
        with self.database.connection(write=True) as db:
            db.execute('INSERT INTO assets(id,revision,payload) VALUES(%s,7,%s)',(asset,raw))
            for identity,owner in [('ours',asset),('other','b'*64),('invalid',None)]:
                db.execute('INSERT INTO copy_operations VALUES(%s,%s,%s)',('admission',identity,encode({'id':identity,'asset':owner,'kind':'legacy','manifest':{'book.cbz':''}})))
            db.execute('INSERT INTO collection_submissions VALUES(%s,%s)',('source',encode({'status':'queued','package_id':0,'links':['u','u'],'primary_links':[]})))
        report=convert(self.database,authority=self.authority)
        self.assertEqual(report['unchanged_original_tables']['assets']['rows'],1)
        with self.database.connection() as db:
            self.assertEqual(db.execute('SELECT payload FROM assets').fetchone()[0],raw)
            self.assertEqual(db.execute('SELECT contested,size FROM assets').fetchone(),(False,4))
            self.assertEqual(db.execute("SELECT position,url FROM submission_links WHERE role='links' ORDER BY position").fetchall(),[(0,'u'),(1,'u')])
            self.assertEqual(db.execute("SELECT DISTINCT asset FROM asset_destinations WHERE target='shared'").fetchall(),[(asset,)])
            self.assertEqual(audit(db)['copy_operations'],3)
        selected=CopyOperations(self.database,'admission').for_asset(asset,lambda record,key:True)
        self.assertEqual([record['id'] for record in selected],['invalid','ours'])

    def test_bad_type_rolls_back_schema_and_keeps_original(self):
        raw=encode({'deep_page':'1'})
        with self.database.connection(write=True) as db:
            db.execute('INSERT INTO collection_categories VALUES(%s,%s)',('bad',raw))
        with self.assertRaisesRegex(LibraryError,'collection_categories.*deep_page'):
            convert(self.database,authority=self.authority)
        with self.database.connection() as db:
            self.assertEqual(db.execute('SELECT schema_version FROM meta').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT payload FROM collection_categories').fetchone()[0],raw)
            self.assertIsNone(db.execute("SELECT to_regclass('submission_links')").fetchone()[0])

    def test_deadline_and_uncertain_browser_limit_survive_write(self):
        from library.native_projection import project
        schedule={'not_before':'2026-11-16T12:00:00+00:00','remote_deadline':'unrepresentable','review_block':True}
        category={'deep_page':0,'last_cycle':'2026-11-16T11:59:00+00:00'}
        with self.database.connection(write=True) as db:
            db.execute('INSERT INTO collection_checkpoints VALUES(%s,%s)',('schedule',encode(schedule)))
            db.execute('INSERT INTO collection_categories VALUES(%s,%s)',('category',encode(category)))
        convert(self.database,authority=self.authority)
        state=CollectorState(Path('unused'),database=self.database)
        before=project('collection',state.monitor())
        state.category('category',category)
        after=state.native_view('collection','monitor')
        self.assertEqual(before,after)
        item=next(iter(after['items'].values()))
        self.assertEqual(datetime.fromisoformat(item['due_at']),datetime(2026,11,16,12,4,tzinfo=timezone.utc))
        self.assertEqual(item['blocked'],'remote_review_block')
        with self.database.connection() as db:
            self.assertEqual(db.execute('SELECT remote_deadline_kind,remote_deadline_at FROM collection_checkpoints').fetchone(),('unrepresentable',None))

    def test_stale_completion_cannot_replace_newer_phase(self):
        old={'id':'copy','kind':'legacy','asset':'a'*64,'phase':'intent','manifest':{}}
        with self.database.connection(write=True) as db:
            db.execute('INSERT INTO copy_operations VALUES(%s,%s,%s)',('admission','copy',encode(old)))
        convert(self.database,authority=self.authority)
        owner=CopyOperations(self.database,'admission')
        completion=owner.prepare({**old,'phase':'committed'})
        owner.phase({**old,'phase':'prepared'})
        with self.assertRaisesRegex(LibraryError,'Copy operation result conflict'):
            with self.database.connection(write=True) as db:
                apply_completion(db,completion)
        with self.database.connection() as db:
            self.assertEqual(db.execute('SELECT phase FROM copy_operations').fetchone()[0],'prepared')
            audit(db)


if __name__=='__main__':
    unittest.main()
