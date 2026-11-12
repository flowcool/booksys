"""Concrete operational domain tables in the Booksys authority database."""
from library.postgres import Database, DeadlineConnection
from library.primitives import LibraryError,digest
from pathlib import Path
from typing import Any
from dataclasses import dataclass
import copy
from library.codec import encode, decode
from library.postgres import guards
from library.stage_model import QUEUES, Task


@dataclass(frozen=True)
class OperationalCompletion:
    kind: str
    identity: str
    expected: str | None
    value: dict[str,Any]
    checkpoint: str | None = None


def completion_guards(result: OperationalCompletion) -> set[str]:
    if result.kind=='manual-proof':
        return {'manual-proof:'+result.identity}
    if result.kind in ('publication','admission','provisional'):
        return {'copy-operation:'+result.kind+':'+result.identity}
    scopes={({'backfill':'collection-backfill','routing':'routing-package','retirement':'retirement-acquisition'}.get(result.kind,result.kind))+':'+result.identity}
    if result.kind=='backfill':
        scopes.add('backfill-selection')
    if result.kind=='category':
        scopes.add('collection-checkpoint:collection_checkpoint')
    return scopes


def verify_superseded_native_readback(db: DeadlineConnection, task: Task, proof: str,
                                      completions: list[OperationalCompletion]) -> None:
    """An old attempt can finish only its retained operation and receipt."""
    import hashlib
    binding = task.native_attempt
    if (binding is None or binding.get('task') != task.key or binding.get('generation') != task.generation
            or task.stage not in ('routing', 'retirement') or len(completions) != 1):
        raise LibraryError('Superseded attempt lacks exact native readback')
    source, entry, journal = binding['source'], binding['entry'], binding['journal']
    result = completions[0]
    retained = decode(result.expected) if result.expected is not None else {}
    marker = [task.key, task.generation]
    if (digest(entry) != task.input_revision or result.kind != task.stage
            or any(record.get('task_attempt') != marker or record.get('source') != source
                   for record in (retained, result.value))):
        raise LibraryError('Superseded native operation differs from retained attempt')
    if task.stage == 'routing':
        if (result.identity != str(entry['package_id']) or result.value.get('phase') not in ('stopped','active')
                or any(record.get('pid') != entry['package_id'] or record.get('name') != entry['name']
                       for record in (retained, result.value))):
            raise LibraryError('Superseded routing ownership changed')
    elif (result.identity != source or result.value.get('phase') != 'retired'
          or any(record.get('submission') != entry for record in (retained, result.value))):
        raise LibraryError('Superseded retirement ownership changed')
    row = db.execute('SELECT payload FROM native_effect WHERE singleton=1').fetchone()
    pointer = decode(row[0]) if row else {}
    owner = hashlib.sha256((task.stage+':'+source).encode()).hexdigest()
    if (proof != task.stage+'-readback:'+digest(result.value)
            or pointer.get('state') != 'resolved' or pointer.get('owner') != owner
            or pointer.get('journal') != journal or pointer.get('proof') != proof):
        raise LibraryError('Superseded native receipt is not retained')


def apply_completion(db: DeadlineConnection, result: OperationalCompletion) -> None:
    """Apply the closed terminal collector transitions inside task completion."""
    if result.kind=='manual-proof':
        row=db.execute('SELECT result FROM manual_proofs WHERE reference=%s',(result.identity,)).fetchone()
        if row is None or row[0]!=result.expected:
            raise LibraryError('Manual proof result conflict')
        db.execute('UPDATE manual_proofs SET result=%s WHERE reference=%s',(encode(result.value),result.identity))
        return
    if result.kind in ('publication','admission','provisional'):
        row=db.execute('SELECT payload FROM copy_operations WHERE namespace=%s AND id=%s',(result.kind,result.identity)).fetchone()
        if (row[0] if row else None)!=result.expected:
            raise LibraryError('Copy operation result conflict')
        db.execute('UPDATE copy_operations SET payload=%s WHERE namespace=%s AND id=%s',(encode(result.value),result.kind,result.identity))
        return
    tables={'category':'collection_categories','fiche':'fiche_progress','backfill':'collection_backfill','routing':'routing_packages','retirement':'retirement_acquisitions'}
    if result.kind not in tables:
        raise LibraryError('Unsupported operational completion')
    if result.kind=='backfill' and CollectorState._batch(db)!=result.identity:
        raise LibraryError('Selected backfill changed')
    table=tables[result.kind];column='batch' if result.kind=='backfill' else 'pid' if result.kind=='routing' else 'source'
    row=db.execute(f'SELECT payload FROM {table} WHERE {column}=%s',(result.identity,)).fetchone()
    if (row[0] if row else None)!=result.expected:
        raise LibraryError('Operational result conflict')
    if result.kind=='category':
        checkpoint=CollectorState._checkpoints(db).get('collection_checkpoint')
        if (encode(checkpoint) if checkpoint is not None else None)!=result.checkpoint:
            raise LibraryError('Collection checkpoint conflict')
        db.execute("DELETE FROM collection_checkpoints WHERE kind='collection_checkpoint'")
    if result.kind=='retirement':
        retain_retirement(db,result.identity,result.value)
    else:
        db.execute(f'INSERT INTO {table} VALUES (%s,%s) ON CONFLICT({column}) DO UPDATE SET payload=excluded.payload',
                   (result.identity,encode(result.value)))


def retain_retirement(db: DeadlineConnection, source: str, value: dict[str,Any]) -> None:
    db.execute('INSERT INTO retirement_acquisitions VALUES (%s,%s) ON CONFLICT(source) DO UPDATE SET payload=excluded.payload',
               (source,encode({**value,'packages':{}})))
    db.execute('DELETE FROM retired_links WHERE source=%s',(source,))
    db.execute('DELETE FROM retired_packages WHERE source=%s',(source,))
    for pid,package in value['packages'].items():
        db.execute('INSERT INTO retired_packages VALUES (%s,%s,%s)',(source,pid,encode({**package,'links':[]})))
        for index,link in enumerate(package['links']):
            db.execute('INSERT INTO retired_links VALUES (%s,%s,%s,%s)',(source,pid,index,encode(link)))


def schema(db: DeadlineConnection) -> None:
    db.execute('CREATE TABLE resource_pools(pool TEXT PRIMARY KEY,capacity TEXT NOT NULL,reserve_bytes BIGINT NOT NULL,policy TEXT NOT NULL)')
    for table in ('resource_grants', 'resource_waiters'):
        db.execute(f'CREATE TABLE {table}(pool TEXT NOT NULL REFERENCES resource_pools(pool),key TEXT NOT NULL, '
                   'age TEXT NOT NULL,queue TEXT NOT NULL,demand TEXT NOT NULL,PRIMARY KEY(pool,key))')
    db.execute('CREATE TABLE decoder_owners(owner TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE decoder_children(owner TEXT NOT NULL REFERENCES decoder_owners(owner),name TEXT NOT NULL,position BIGINT GENERATED ALWAYS AS IDENTITY,stopped BOOLEAN NOT NULL DEFAULT false,PRIMARY KEY(owner,name))')
    db.execute('CREATE TABLE dependency_readiness(name TEXT PRIMARY KEY,available BOOLEAN NOT NULL,at DOUBLE PRECISION NOT NULL,recovered DOUBLE PRECISION NOT NULL,url TEXT NOT NULL,next_probe DOUBLE PRECISION NOT NULL DEFAULT 0)')
    for table in ('captured_sources','collection_categories','collection_submissions','fiche_progress'):
        db.execute(f'CREATE TABLE {table}(source TEXT COLLATE \"C\" PRIMARY KEY,payload TEXT NOT NULL)')
    db.execute('CREATE TABLE captured_downloads(source TEXT NOT NULL REFERENCES captured_sources(source),target TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(source,target))')
    db.execute('CREATE TABLE captured_download_links(source TEXT NOT NULL,target TEXT NOT NULL,position BIGINT NOT NULL,url TEXT NOT NULL,PRIMARY KEY(source,target,position),FOREIGN KEY(source,target) REFERENCES captured_downloads(source,target))')
    db.execute('CREATE TABLE collection_checkpoints(kind TEXT PRIMARY KEY,payload TEXT NOT NULL)')
    db.execute('CREATE TABLE checkpoint_members(kind TEXT NOT NULL REFERENCES collection_checkpoints(kind) ON DELETE CASCADE,field TEXT NOT NULL,position BIGINT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(kind,field,position))')
    db.execute('CREATE TABLE checkpoint_targets(kind TEXT NOT NULL REFERENCES collection_checkpoints(kind) ON DELETE CASCADE,target TEXT NOT NULL,position BIGINT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(kind,target))')
    db.execute('CREATE TABLE checkpoint_downloads(kind TEXT NOT NULL REFERENCES collection_checkpoints(kind) ON DELETE CASCADE,target TEXT NOT NULL,position BIGINT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(kind,target))')
    db.execute('CREATE TABLE checkpoint_links(kind TEXT NOT NULL,target TEXT NOT NULL,position BIGINT NOT NULL,url TEXT NOT NULL,PRIMARY KEY(kind,target,position),FOREIGN KEY(kind,target) REFERENCES checkpoint_downloads(kind,target) ON DELETE CASCADE)')
    db.execute('CREATE TABLE collection_backfill(batch TEXT PRIMARY KEY,payload TEXT NOT NULL)')
    db.execute('CREATE TABLE backfill_selection(singleton INTEGER PRIMARY KEY CHECK(singleton=1),batch TEXT NOT NULL REFERENCES collection_backfill(batch))')
    db.execute('CREATE TABLE backfill_pages(batch TEXT NOT NULL REFERENCES collection_backfill(batch),url TEXT NOT NULL,position BIGINT NOT NULL,PRIMARY KEY(batch,url))')
    db.execute('CREATE TABLE backfill_items(batch TEXT NOT NULL REFERENCES collection_backfill(batch),source TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(batch,source))')
    db.execute('CREATE TABLE backfill_page_items(batch TEXT NOT NULL,url TEXT NOT NULL,source TEXT NOT NULL,position BIGINT NOT NULL,PRIMARY KEY(batch,url,source),FOREIGN KEY(batch,url) REFERENCES backfill_pages(batch,url))')
    db.execute('CREATE TABLE network_budgets(scope TEXT PRIMARY KEY,requests INTEGER NOT NULL,period DOUBLE PRECISION NOT NULL,spacing DOUBLE PRECISION NOT NULL,review_block BOOLEAN NOT NULL,last DOUBLE PRECISION NOT NULL,not_before DOUBLE PRECISION NOT NULL)')
    db.execute('CREATE TABLE network_starts(scope TEXT NOT NULL REFERENCES network_budgets(scope),position BIGINT GENERATED ALWAYS AS IDENTITY,at DOUBLE PRECISION NOT NULL,PRIMARY KEY(scope,position))')
    db.execute('CREATE TABLE activity_observation(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)')
    db.execute('CREATE TABLE browser_observation(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)')
    db.execute('CREATE TABLE routing_scheduler(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)')
    db.execute('CREATE TABLE cycle_status(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)')
    db.execute('CREATE TABLE retirement_observation(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)')
    db.execute('CREATE TABLE compose_sources(role TEXT PRIMARY KEY,observed_at DOUBLE PRECISION NOT NULL,generation TEXT NOT NULL)')
    db.execute('CREATE TABLE diagnostic_requests(reference TEXT PRIMARY KEY,payload TEXT NOT NULL)')
    db.execute('CREATE TABLE manual_proofs(reference TEXT PRIMARY KEY,request TEXT NOT NULL,result TEXT)')
    db.execute('CREATE TABLE copy_operations(namespace TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(namespace,id))')
    db.execute('CREATE TABLE resolution_previews(asset TEXT PRIMARY KEY,id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL)')
    db.execute('CREATE TABLE reader_requests(job TEXT PRIMARY KEY,grant_key TEXT NOT NULL,operation TEXT NOT NULL,manifest TEXT NOT NULL,manifest_raw TEXT NOT NULL)')
    db.execute("CREATE TABLE reader_job_state(job TEXT NOT NULL REFERENCES reader_requests(job),kind TEXT NOT NULL CHECK(kind IN ('intent','container','receipt','reconciled','cleanup')),payload TEXT NOT NULL,PRIMARY KEY(job,kind))")
    db.execute('CREATE TABLE provider_cache(namespace TEXT NOT NULL,url TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(namespace,url))')
    db.execute('CREATE TABLE routing_packages(pid TEXT PRIMARY KEY,payload TEXT NOT NULL)')
    db.execute('CREATE TABLE retirement_cursor(singleton INTEGER PRIMARY KEY CHECK(singleton=1),source TEXT NOT NULL)')
    db.execute('CREATE TABLE retirement_acquisitions(source TEXT PRIMARY KEY,payload TEXT NOT NULL)')
    db.execute('CREATE TABLE retired_packages(source TEXT NOT NULL REFERENCES retirement_acquisitions(source),pid TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(source,pid))')
    db.execute('CREATE TABLE retired_links(source TEXT NOT NULL,pid TEXT NOT NULL,position BIGINT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(source,pid,position),FOREIGN KEY(source,pid) REFERENCES retired_packages(source,pid))')
    db.execute('CREATE TABLE native_effect(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)')
    db.execute('CREATE TABLE native_observation(singleton INTEGER PRIMARY KEY CHECK(singleton=1),observed_at DOUBLE PRECISION NOT NULL,remaining_bytes BIGINT,overshoot_bytes BIGINT)')
    db.execute('CREATE TABLE observed_packages(pid BIGINT PRIMARY KEY,position BIGINT NOT NULL,payload TEXT NOT NULL)')
    db.execute('CREATE TABLE observed_links(pid BIGINT NOT NULL REFERENCES observed_packages(pid),position BIGINT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(pid,position))')
    db.execute('CREATE TABLE actor_heartbeats(queue TEXT PRIMARY KEY,observed_at DOUBLE PRECISION NOT NULL)')
    db.execute("CREATE TABLE runtime_activation(role TEXT PRIMARY KEY CHECK(role IN ('stage','library')),payload TEXT NOT NULL)")
    db.execute('CREATE TABLE stage_selection(queue TEXT PRIMARY KEY,after_age TEXT,after_key TEXT,lookback_done BOOLEAN NOT NULL)')
    db.execute('CREATE TABLE stage_discovery(kind TEXT PRIMARY KEY CHECK(kind IN (\'business\',\'intake\')),after_key TEXT NOT NULL)')


class OperationalState:
    def __init__(self, database: Database):
        self._database = database

    def selection(self, queue: str) -> tuple[tuple[str,str] | None,bool]:
        if queue not in QUEUES:
            raise LibraryError('Invalid selection queue')
        with self._database.connection() as db:
            row = db.execute('SELECT after_age,after_key,lookback_done FROM stage_selection WHERE queue=%s',(queue,)).fetchone()
            if row is None:
                return None,False
            return ((row[0],row[1]) if row[0] is not None else None),row[2]

    def save_selection(self, queue: str, after: tuple[str,str] | None, lookback_done: bool) -> None:
        if queue not in QUEUES or type(lookback_done) is not bool or (after is not None and
                (len(after)!=2 or any(not isinstance(value,str) for value in after))):
            raise LibraryError('Invalid selection continuation')
        with self._database.connection(write=True) as db:
            db.execute('INSERT INTO stage_selection VALUES (%s,%s,%s,%s) ON CONFLICT(queue) DO UPDATE '
                       'SET after_age=excluded.after_age,after_key=excluded.after_key,lookback_done=excluded.lookback_done',
                       (queue,after[0] if after else None,after[1] if after else None,lookback_done))

    def discovery(self, kind: str = 'business') -> str:
        with self._database.connection() as db:
            row = db.execute('SELECT after_key FROM stage_discovery WHERE kind=%s',(kind,)).fetchone()
            return row[0] if row else ''

    def save_discovery(self, after: str, kind: str = 'business') -> None:
        if not isinstance(after,str) or kind not in ('business','intake'):
            raise LibraryError('Invalid discovery continuation')
        with self._database.connection(write=True) as db:
            db.execute('INSERT INTO stage_discovery VALUES (%s,%s) ON CONFLICT(kind) DO UPDATE SET after_key=excluded.after_key',(kind,after))


class CollectorState:
    """Existing collector records, split by source and checkpoint ownership."""
    def __init__(self, directory: Path, *, database: Database | None = None):
        self.directory=directory
        self._database=database or Database.configured(directory)

    def captured(self) -> dict[str,Any]:
        with self._database.connection() as db:
            records={source:decode(payload) for source,payload in
                     db.execute('SELECT source,payload FROM captured_sources ORDER BY source')}
            links: dict[tuple[str,str],list[str]]={}
            for source,target,url in db.execute('SELECT source,target,url FROM captured_download_links ORDER BY source,target,position'):
                links.setdefault((source,target),[]).append(url)
            for source,target,payload in db.execute('SELECT source,target,payload FROM captured_downloads ORDER BY source,target'):
                record=decode(payload)
                if 'urls' in record:
                    record['urls']=links.get((source,target),[])
                records[source]['downloads'][target]=record
            return {'version':1,'items':records}

    def capture(self, source: str, record: dict[str,Any], **checkpoints: Any) -> None:
        # Parsing, browser I/O and serialization finish before this transaction.
        payload=encode({**record,**({'downloads':{}} if 'downloads' in record else {})})
        if set(checkpoints)-{'source_checkpoint','collection_checkpoint'}:
            raise LibraryError('Invalid capture checkpoint')
        with self._database.connection(write=True) as db:
            guards(db,{'captured-source:'+source,*('collection-checkpoint:'+k for k in checkpoints)})
            db.execute('INSERT INTO captured_sources VALUES (%s,%s) ON CONFLICT(source) DO UPDATE SET payload=excluded.payload',
                       (source,payload))
            db.execute('DELETE FROM captured_download_links WHERE source=%s',(source,))
            db.execute('DELETE FROM captured_downloads WHERE source=%s',(source,))
            for target,download in record.get('downloads',{}).items():
                db.execute('INSERT INTO captured_downloads VALUES (%s,%s,%s)',(source,target,encode({**download,**({'urls':[]} if 'urls' in download else {})})))
                for index,url in enumerate(download.get('urls',[])):
                    db.execute('INSERT INTO captured_download_links VALUES (%s,%s,%s,%s)',(source,target,index,url))
            for kind,value in checkpoints.items():
                self._checkpoint(db,kind,value)

    def monitor(self) -> dict[str,Any]:
        with self._database.connection() as db:
            result={'version':1,'categories':{source:decode(payload) for source,payload in db.execute(
                'SELECT source,payload FROM collection_categories ORDER BY source')},'submissions':{
                source:decode(payload) for source,payload in db.execute('SELECT source,payload FROM collection_submissions ORDER BY source')}}
            result.update(self._checkpoints(db))
            return result

    def category(self, source: str, record: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            guards(db,completion_guards(OperationalCompletion('category',source,None,record)))
            db.execute('INSERT INTO collection_categories VALUES (%s,%s) ON CONFLICT(source) DO UPDATE SET payload=excluded.payload',(source,encode(record)))

    def submission(self, source: str, record: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            guards(db,{'submission:'+source})
            db.execute('INSERT INTO collection_submissions VALUES (%s,%s) ON CONFLICT(source) DO UPDATE SET payload=excluded.payload',(source,encode(record)))

    @staticmethod
    def _checkpoints(db: DeadlineConnection) -> dict[str,Any]:
        result={kind:decode(payload) for kind,payload in db.execute('SELECT kind,payload FROM collection_checkpoints')}
        for kind,field,payload in db.execute('SELECT kind,field,payload FROM checkpoint_members ORDER BY kind,field,position'):
            target=result[kind]
            if field in ('scope.categories','scope.hosters'):
                target['scope'][field.split('.')[1]].append(decode(payload))
            elif field in ('visited','items','seen_ids','hosters'):
                target[field].append(decode(payload))
            else:raise LibraryError('Unknown checkpoint member field')
        for kind,target,payload in db.execute('SELECT kind,target,payload FROM checkpoint_targets ORDER BY kind,position'):
            result[kind]['plan']['targets'][target]=decode(payload)
        for kind,target,payload in db.execute('SELECT kind,target,payload FROM checkpoint_downloads ORDER BY kind,position'):
            result[kind]['downloads'][target]=decode(payload)
        for kind,target,url in db.execute('SELECT kind,target,url FROM checkpoint_links ORDER BY kind,target,position'):
            result[kind]['downloads'][target]['urls'].append(url)
        return result

    @staticmethod
    def _checkpoint(db: DeadlineConnection, kind: str, value: Any) -> None:
        if value is None:
            db.execute('DELETE FROM collection_checkpoints WHERE kind=%s',(kind,))
            return
        metadata=copy.deepcopy(value)
        members: list[tuple[str,int,Any]]=[]
        if kind in ('collection_checkpoint','source_checkpoint'):
            for field in ('visited','items','seen_ids','hosters','scope.categories','scope.hosters'):
                target=metadata
                if field.startswith('scope.'):
                    if 'scope' not in target:continue
                    target=target['scope']
                name=field.split('.')[-1]
                if name not in target or target[name] is None:continue
                if not isinstance(target[name],list):raise LibraryError('Invalid ordered checkpoint members')
                members.extend((field,index,item) for index,item in enumerate(target[name]))
                target[name]=[]
        targets=metadata.get('plan',{}).get('targets',{}) if kind=='source_checkpoint' else {}
        downloads=metadata.get('downloads',{}) if kind=='source_checkpoint' else {}
        if kind=='source_checkpoint':
            if 'targets' in metadata.get('plan',{}):metadata['plan']['targets']={}
            if 'downloads' in metadata:metadata['downloads']={}
        db.execute('INSERT INTO collection_checkpoints VALUES (%s,%s) ON CONFLICT(kind) DO UPDATE SET payload=excluded.payload',(kind,encode(metadata)))
        for table in ('checkpoint_members','checkpoint_targets','checkpoint_downloads'):
            db.execute(f'DELETE FROM {table} WHERE kind=%s',(kind,))
        for field,index,item in members:
            db.execute('INSERT INTO checkpoint_members VALUES (%s,%s,%s,%s)',(kind,field,index,encode(item)))
        for index,(target,payload) in enumerate(targets.items()):
            db.execute('INSERT INTO checkpoint_targets VALUES (%s,%s,%s,%s)',(kind,target,index,encode(payload)))
        for index,(target,payload) in enumerate(downloads.items()):
            record=copy.deepcopy(payload)
            links=record.get('urls',[])
            if 'urls' in record:record['urls']=[]
            db.execute('INSERT INTO checkpoint_downloads VALUES (%s,%s,%s,%s)',(kind,target,index,encode(record)))
            for position,url in enumerate(links):db.execute('INSERT INTO checkpoint_links VALUES (%s,%s,%s,%s)',(kind,target,position,url))

    def patch_monitor(self, **changes: Any) -> None:
        if set(changes)-{'schedule','collection_checkpoint','source_checkpoint'}:
            raise LibraryError('Invalid collection checkpoint')
        with self._database.connection(write=True) as db:
            guards(db,{'collection-checkpoint:'+kind for kind in changes})
            for kind,value in changes.items():
                self._checkpoint(db,kind,value)

    def fiches(self) -> dict[str,Any]:
        with self._database.connection() as db:
            return {'version':1,'items':{source:decode(payload) for source,payload in
                    db.execute('SELECT source,payload FROM fiche_progress ORDER BY source')}}

    def fiche(self, source: str, record: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            guards(db,completion_guards(OperationalCompletion('fiche',source,None,record)))
            db.execute('INSERT INTO fiche_progress VALUES (%s,%s) ON CONFLICT(source) DO UPDATE SET payload=excluded.payload',(source,encode(record)))

    @staticmethod
    def _batch(db: DeadlineConnection) -> str | None:
        row=db.execute('SELECT batch FROM backfill_selection WHERE singleton=1').fetchone()
        return row[0] if row else None

    def backfill(self) -> dict[str,Any] | None:
        with self._database.connection() as db:
            batch=self._batch(db)
            if batch is None:return None
            row=db.execute('SELECT payload FROM collection_backfill WHERE batch=%s',(batch,)).fetchone()
            if row is None:raise LibraryError('Selected backfill unavailable')
            result: dict[str,Any]=decode(row[0]);result['items']={source:decode(payload) for source,payload in db.execute(
                'SELECT source,payload FROM backfill_items WHERE batch=%s ORDER BY source',(batch,))}
            members: dict[str,list[str]]={}
            for url,source in db.execute('SELECT url,source FROM backfill_page_items WHERE batch=%s ORDER BY url,position',(batch,)):
                members.setdefault(url,[]).append(source)
            result['pages']={url:members.get(url,[]) for (url,) in db.execute('SELECT url FROM backfill_pages WHERE batch=%s ORDER BY position',(batch,))}
            return result

    def start_backfill(self,value: dict[str,Any],*,replace_completed: bool=False) -> str:
        """Select a new bounded request while retaining every prior batch row."""
        from library.primitives import digest
        batch=str(value.get('id') or digest(value))
        with self._database.connection(write=True) as db:
            guards(db,{'backfill-selection'})
            previous=self._batch(db)
            if previous is not None:
                row=db.execute('SELECT payload FROM collection_backfill WHERE batch=%s',(previous,)).fetchone()
                if row is None or not replace_completed or decode(row[0]).get('status')!='complete':
                    raise LibraryError('Existing backfill requires inspection')
            guards(db,{'collection-backfill:'+batch})
            db.execute('INSERT INTO collection_backfill VALUES (%s,%s)',(batch,encode({k:v for k,v in value.items() if k not in ('pages','items')})))
            db.execute('INSERT INTO backfill_selection VALUES(1,%s) ON CONFLICT(singleton) DO UPDATE SET batch=excluded.batch',(batch,))
        return batch

    def save_backfill(self, value: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            guards(db,{'backfill-selection'})
            batch=self._batch(db)
            if batch is None:raise LibraryError('Selected backfill unavailable')
            guards(db,{'collection-backfill:'+batch})
            db.execute('UPDATE collection_backfill SET payload=%s WHERE batch=%s',(encode({k:v for k,v in value.items() if k not in ('pages','items')}),batch))

    def backfill_page(self, value: dict[str,Any], url: str, items: dict[str,Any]) -> None:
        """Accept one parsed page and its continuation in one transaction."""
        with self._database.connection(write=True) as db:
            guards(db,{'backfill-selection'})
            batch=self._batch(db)
            if batch is None:raise LibraryError('Selected backfill unavailable')
            guards(db,{'collection-backfill:'+batch})
            db.execute('UPDATE collection_backfill SET payload=%s WHERE batch=%s',(encode({k:v for k,v in value.items() if k not in ('pages','items')}),batch))
            position=len(value['pages'])-1
            db.execute('INSERT INTO backfill_pages VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',(batch,url,position))
            for index,(source,payload) in enumerate(items.items()):
                db.execute('INSERT INTO backfill_page_items VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING',(batch,url,source,index))
                db.execute('INSERT INTO backfill_items VALUES (%s,%s,%s) ON CONFLICT(batch,source) DO UPDATE SET payload=excluded.payload',(batch,source,encode(payload)))

    def prepare_completion(self, kind: str, identity: str, value: dict[str,Any], *, expected: tuple[str | None] | None = None) -> OperationalCompletion:
        tables={'category':'collection_categories','fiche':'fiche_progress','backfill':'collection_backfill','routing':'routing_packages','retirement':'retirement_acquisitions'}
        if kind not in tables:
            raise LibraryError('Unsupported operational completion')
        column='batch' if kind=='backfill' else 'pid' if kind=='routing' else 'source'
        with self._database.connection() as db:
            if kind=='backfill' and identity=='current':
                selected=self._batch(db)
                if selected is None:raise LibraryError('Selected backfill unavailable')
                identity=selected
            row=db.execute(f'SELECT payload FROM {tables[kind]} WHERE {column}=%s',(identity,)).fetchone()
            checkpoint=self._checkpoints(db).get('collection_checkpoint') if kind=='category' else None
            return OperationalCompletion(kind,identity,expected[0] if expected is not None else (row[0] if row else None),value,encode(checkpoint) if checkpoint is not None else None)

    def native_view(self, queue: str, kind: str) -> dict[str,Any]:
        from library.native_projection import project
        if kind == 'backfill':
            value=self.backfill()
            return project(queue,value) if value else {'items':{}}
        if kind != 'monitor':
            raise LibraryError('Invalid native source kind')
        with self._database.connection() as db:
            monitor={'version':1,'categories':{source:decode(payload) for source,payload in db.execute(
                'SELECT source,payload FROM collection_categories ORDER BY source')},'submissions':{
                source:decode(payload) for source,payload in db.execute('SELECT source,payload FROM collection_submissions ORDER BY source')}}
            monitor.update(self._checkpoints(db))
            fiches={'items':{source:decode(payload) for source,payload in db.execute('SELECT source,payload FROM fiche_progress')}}
            return project(queue,monitor,auxiliary=fiches)


def native_input_guards(binding: dict[str,Any]) -> set[str]:
    """The same concrete keys guard canonical writers and native task claims."""
    kind,identity=binding['kind'],binding['identity']
    if kind=='category':
        return {'category:'+identity,'collection-checkpoint:schedule',
                'collection-checkpoint:collection_checkpoint','collection-checkpoint:source_checkpoint'}
    if kind=='fiche':return {'submission:'+identity,'fiche:'+identity,'collection-checkpoint:schedule'}
    if kind=='submission':return {'submission:'+identity}
    if kind=='backfill':return {'backfill-selection','collection-backfill:'+identity}
    raise LibraryError('Invalid native source binding')


def native_input(db: DeadlineConnection, binding: dict[str,Any], queue: str, *, active: bool=False) -> tuple[str,str,str] | None:
    """Read one bound canonical source. Never read the complete monitor."""
    from library.native_projection import project
    kind,identity=binding['kind'],binding['identity']
    if binding.get('registration_kind')!=('backfill' if kind=='backfill' else 'monitor'):
        raise LibraryError('Invalid native registration binding')
    monitor: dict[str,Any]={'version':1,'categories':{},'submissions':{}}
    auxiliary: dict[str,Any]={'items':{}}
    if kind=='backfill':
        selected=CollectorState._batch(db)
        if selected!=identity:return None
        row=db.execute('SELECT payload FROM collection_backfill WHERE batch=%s',(identity,)).fetchone()
        if row is None:return None
        value=decode(row[0])
        if active:
            original=digest({k:v for k,v in value.items() if k not in ('pages','items','next_page','submitted','status','completed_at')})
            return (original,'active-backfill','')
        value['items']={source:decode(payload) for source,payload in db.execute('SELECT source,payload FROM backfill_items WHERE batch=%s ORDER BY source',(identity,))}
        members: dict[str,list[str]]={}
        for url,source in db.execute('SELECT url,source FROM backfill_page_items WHERE batch=%s ORDER BY url,position',(identity,)):
            members.setdefault(url,[]).append(source)
        value['pages']={url:members.get(url,[]) for (url,) in db.execute('SELECT url FROM backfill_pages WHERE batch=%s ORDER BY position',(identity,))}
        view=project(queue,value);entity='backfill'
    else:
        table='collection_categories' if kind=='category' else 'collection_submissions'
        row=db.execute(f'SELECT payload FROM {table} WHERE source=%s',(identity,)).fetchone()
        if row is None:return None
        record=decode(row[0])
        monitor['categories' if kind=='category' else 'submissions'][identity]=record
        if kind in ('category','fiche'):
            schedule=db.execute("SELECT payload FROM collection_checkpoints WHERE kind='schedule'").fetchone()
            if schedule:monitor['schedule']=decode(schedule[0])
        if kind=='category':
            if not active:monitor.update(CollectorState._checkpoints(db))
            elif digest(record)!=binding['original']:return None
            entity='category-'+digest(identity)
        elif kind=='fiche':
            fiche=db.execute('SELECT payload FROM fiche_progress WHERE source=%s',(identity,)).fetchone()
            if fiche:auxiliary['items'][identity]=decode(fiche[0])
            entity='fiche-'+digest(identity)
        elif kind=='submission':entity='source-'+digest(identity)
        else:raise LibraryError('Invalid native source binding')
        view=project(queue,monitor,auxiliary=auxiliary)
    value=view['items'].get(entity)
    if value is None:return None
    revision=binding['original'] if active and kind=='category' else value['input_revision']
    return revision,value['method'],digest(view['_native']['guards'][entity])


class RuntimeObservation:
    """One observer publishes a coherent pyLoad readback and its conservative bound."""
    def __init__(self, database: Database):
        self._database=database

    def publish(self, packages: list[dict[str,Any]], *, at: float, remaining: int | None, overshoot: int | None) -> None:
        with self._database.connection(write=True) as db:
            guards(db,{'native-observation'})
            db.execute('DELETE FROM observed_links')
            db.execute('DELETE FROM observed_packages')
            for position,package in enumerate(packages):
                db.execute('INSERT INTO observed_packages VALUES (%s,%s,%s)',(package['pid'],position,encode({k:v for k,v in package.items() if k!='links'})))
                for index,link in enumerate(package.get('links',[])):
                    db.execute('INSERT INTO observed_links VALUES (%s,%s,%s)',(package['pid'],index,encode(link)))
            db.execute('INSERT INTO native_observation VALUES (1,%s,%s,%s) ON CONFLICT(singleton) DO UPDATE SET observed_at=excluded.observed_at,remaining_bytes=excluded.remaining_bytes,overshoot_bytes=excluded.overshoot_bytes',(at,remaining,overshoot))

    def queue(self) -> tuple[list[dict[str,Any]],float]:
        with self._database.connection() as db:
            header=db.execute('SELECT observed_at FROM native_observation WHERE singleton=1').fetchone()
            if header is None:
                raise LibraryError('Native observation unavailable')
            links: dict[int,list[dict[str,Any]]]={}
            for pid,payload in db.execute('SELECT pid,payload FROM observed_links ORDER BY pid,position'):
                links.setdefault(pid,[]).append(decode(payload))
            return [{**decode(payload),'links':links.get(pid,[])} for pid,payload in db.execute('SELECT pid,payload FROM observed_packages ORDER BY position')],header[0]

    def capacity(self) -> dict[str,Any]:
        with self._database.connection() as db:
            row=db.execute('SELECT observed_at,remaining_bytes,overshoot_bytes FROM native_observation WHERE singleton=1').fetchone()
            if row is None:
                raise LibraryError('Native observation unavailable')
            return dict(schema=1,observed_at=row[0],remaining_bytes=row[1],overshoot_bytes=row[2])

    def heartbeat(self, queue: str, *, at: float) -> None:
        if queue not in (*QUEUES,'observer','discovery'):
            raise LibraryError('Invalid actor heartbeat')
        with self._database.connection(write=True) as db:
            db.execute('INSERT INTO actor_heartbeats VALUES (%s,%s) ON CONFLICT(queue) DO UPDATE SET observed_at=excluded.observed_at',(queue,at))

    def heartbeats(self) -> dict[str,float]:
        with self._database.connection() as db:
            return dict(db.execute('SELECT queue,observed_at FROM actor_heartbeats'))

    def activation(self, *, role: str="stage") -> dict[str,Any]:
        with self._database.connection() as db:
            row=db.execute('SELECT payload FROM runtime_activation WHERE role=%s',(role,)).fetchone()
            if row is None:
                raise LibraryError('Qualified runtime activation unavailable')
            result: dict[str,Any]=decode(row[0])
            return result

    def activate(self, value: dict[str,Any], *, role: str="stage") -> None:
        with self._database.connection(write=True) as db:
            guards(db,{'runtime-activation:'+role})
            db.execute('INSERT INTO runtime_activation VALUES (%s,%s) ON CONFLICT(role) DO UPDATE SET payload=excluded.payload',(role,encode(value)))


class ProviderCache:
    def __init__(self, directory: Path, namespace: str):
        self._database=Database.configured(directory)
        self.namespace=namespace

    def get(self, url: str) -> dict[str,Any] | None:
        with self._database.connection() as db:
            row=db.execute('SELECT payload FROM provider_cache WHERE namespace=%s AND url=%s',(self.namespace,url)).fetchone()
            value: dict[str,Any] | None=decode(row[0]) if row else None
            return value

    def retain(self, url: str, value: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            db.execute('INSERT INTO provider_cache VALUES (%s,%s,%s) ON CONFLICT(namespace,url) DO UPDATE SET payload=excluded.payload',(self.namespace,url,encode(value)))


class NativeHistory:
    """Keyed native effect phases. The path records provenance, never authority."""
    def __init__(self, directory: Path, *, database: Database | None = None):
        self._database = database if database is not None else Database.configured(directory)

    def routing(self) -> dict[str,Any]:
        with self._database.connection() as db:
            return {'schema':1,'packages':{key:decode(payload) for key,payload in db.execute('SELECT pid,payload FROM routing_packages ORDER BY pid')}}

    def route(self, pid: str, value: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            guards(db,{'routing-package:'+pid})
            db.execute('INSERT INTO routing_packages VALUES (%s,%s) ON CONFLICT(pid) DO UPDATE SET payload=excluded.payload',(pid,encode(value)))

    def retirements(self) -> dict[str,Any]:
        with self._database.connection() as db:
            result: dict[str,Any]={'schema':1,'acquisitions':{key:decode(payload) for key,payload in db.execute('SELECT source,payload FROM retirement_acquisitions ORDER BY source')}}
            packages={(source,pid):decode(payload) for source,pid,payload in db.execute('SELECT source,pid,payload FROM retired_packages')}
            for source,pid,payload in db.execute('SELECT source,pid,payload FROM retired_links ORDER BY source,pid,position'):
                packages[source,pid]['links'].append(decode(payload))
            for (source,pid),package in packages.items():
                result['acquisitions'][source]['packages'][pid]=package
            cursor=db.execute('SELECT source FROM retirement_cursor WHERE singleton=1').fetchone()
            if cursor:
                result['cursor']=cursor[0]
            return result

    def retire(self, source: str, value: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            guards(db,{'retirement-acquisition:'+source})
            retain_retirement(db,source,value)

    def retirement_cursor(self, source: str) -> None:
        with self._database.connection(write=True) as db:
            db.execute('INSERT INTO retirement_cursor VALUES (1,%s) ON CONFLICT(singleton) DO UPDATE SET source=excluded.source',(source,))


class ReaderJobs:
    def __init__(self, database: Database):
        self._database=database

    def request(self, job: str, grant: str, manifest: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            import json
            db.execute('INSERT INTO reader_requests VALUES (%s,%s,%s,%s,%s)',(job,grant,manifest['operation'],encode(manifest),json.dumps(manifest)))

    def accepted(self, job: str) -> dict[str,Any]:
        with self._database.connection() as db:
            row=db.execute('SELECT reader_accept(%s)',(job,)).fetchone()
            if row is None or row[0] is None:
                raise LibraryError('Reader requires exact active grant and ownership')
            result: dict[str,Any]=row[0]
            return result

    def read(self, job: str, kind: str) -> dict[str,Any] | None:
        with self._database.connection() as db:
            row=db.execute('SELECT reader_state(%s,%s)',(job,kind)).fetchone()
            value: dict[str,Any] | None=decode(row[0]) if row and row[0] else None
            return value

    def retain(self, job: str, kind: str, value: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            db.execute('SELECT reader_retain(%s,%s,%s)',(job,kind,encode(value)))


class CopyOperations:
    def __init__(self, database: Database, namespace: str):
        if namespace not in ('publication','admission','provisional'):
            raise LibraryError('Invalid copy operation scope')
        self._database=database;self.namespace=namespace

    def get(self, identity: str) -> dict[str,Any] | None:
        with self._database.connection() as db:
            row=db.execute('SELECT payload FROM copy_operations WHERE namespace=%s AND id=%s',(self.namespace,identity)).fetchone()
            result: dict[str,Any] | None=decode(row[0]) if row else None
            return result

    def records(self) -> list[dict[str,Any]]:
        with self._database.connection() as db:
            return [decode(row[0]) for row in db.execute('SELECT payload FROM copy_operations WHERE namespace=%s ORDER BY id',(self.namespace,))]

    def prepare(self, value: dict[str,Any]) -> OperationalCompletion:
        with self._database.connection() as db:
            row=db.execute('SELECT payload FROM copy_operations WHERE namespace=%s AND id=%s',(self.namespace,value['id'])).fetchone()
            return OperationalCompletion(self.namespace,value['id'],row[0] if row else None,value)

    def phase(self, value: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            guards(db,{'copy-operation:'+self.namespace+':'+value['id']})
            db.execute('INSERT INTO copy_operations VALUES (%s,%s,%s) ON CONFLICT(namespace,id) DO UPDATE SET payload=excluded.payload',(self.namespace,value['id'],encode(value)))


class ResolutionPreviews:
    def __init__(self, database: Database):self._database=database
    def retain(self, value: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            guards(db,{'resolution-preview:'+value['asset']})
            db.execute('INSERT INTO resolution_previews VALUES (%s,%s,%s) ON CONFLICT(asset) DO UPDATE SET id=excluded.id,payload=excluded.payload',(value['asset'],value['id'],encode(value)))
            db.execute('INSERT INTO preview_bindings VALUES (%s,%s) ON CONFLICT(id) DO UPDATE SET asset=excluded.asset',(value['id'],value['asset']))
    def get(self, identity: str) -> dict[str,Any] | None:
        with self._database.connection() as db:
            row=db.execute('SELECT payload FROM resolution_previews WHERE id=%s',(identity,)).fetchone()
            result: dict[str,Any] | None=decode(row[0]) if row else None
            return result


class StatusObservations:
    def __init__(self, database: Database):self._database=database
    def activity(self) -> dict[str,Any]:return self._read('activity_observation')
    def browser(self) -> dict[str,Any]:return self._read('browser_observation')
    def cycle(self) -> dict[str,Any]:return self._read('cycle_status')
    def retirement(self) -> dict[str,Any]:return self._read('retirement_observation')
    def routing(self) -> dict[str,Any]:return self._read('routing_scheduler')
    def save_activity(self,value: dict[str,Any]) -> None:self._write('activity_observation',value)
    def save_browser(self,value: dict[str,Any]) -> None:self._write('browser_observation',value)
    def save_cycle(self,value: dict[str,Any]) -> None:self._write('cycle_status',value)
    def save_retirement(self,value: dict[str,Any]) -> None:self._write('retirement_observation',value)
    def save_routing(self,value: dict[str,Any]) -> None:self._write('routing_scheduler',value)
    def _read(self, table: str) -> dict[str,Any]:
        with self._database.connection() as db:
            row=db.execute(f'SELECT payload FROM {table} WHERE singleton=1').fetchone()
            result: dict[str,Any]=decode(row[0]) if row else {}
            return result
    def _write(self, table: str, value: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            db.execute(f'INSERT INTO {table} VALUES (1,%s) ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload',(encode(value),))


class ManualProofs:
    """Explicit immutable requests and atomically selected proof results."""
    def __init__(self,database: Database):self._database=database
    def retain(self,reference: str,request: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            guards(db,{'manual-proof:'+reference})
            row=db.execute('SELECT request FROM manual_proofs WHERE reference=%s',(reference,)).fetchone()
            if row is not None and row[0]!=encode(request):
                raise LibraryError('Manual proof request conflict')
            db.execute('INSERT INTO manual_proofs VALUES (%s,%s,NULL) ON CONFLICT DO NOTHING',(reference,encode(request)))
    def request(self,reference: str) -> dict[str,Any]:
        with self._database.connection() as db:
            row=db.execute('SELECT request FROM manual_proofs WHERE reference=%s',(reference,)).fetchone()
            if row is None:raise LibraryError('Manual proof request unavailable')
            result: dict[str,Any]=decode(row[0]);return result
    def result(self,reference: str) -> dict[str,Any] | None:
        with self._database.connection() as db:
            row=db.execute('SELECT result FROM manual_proofs WHERE reference=%s',(reference,)).fetchone()
            result: dict[str,Any] | None=decode(row[0]) if row and row[0] is not None else None
            return result
    def prepare(self,reference: str,result: dict[str,Any]) -> OperationalCompletion:
        with self._database.connection() as db:
            row=db.execute('SELECT result FROM manual_proofs WHERE reference=%s',(reference,)).fetchone()
            if row is None:raise LibraryError('Manual proof request unavailable')
            return OperationalCompletion('manual-proof',reference,row[0],result)


class ComposeDiagnostics:
    def __init__(self,database: Database):self._database=database
    def sources(self) -> dict[str,Any]:
        with self._database.connection() as db:
            rows=db.execute('SELECT role,observed_at,generation FROM compose_sources ORDER BY role').fetchall()
            return {'schema':1,'observed_at':min((row[1] for row in rows),default=0),'sources':{row[0]:row[2] for row in rows}}
    def retain(self,reference: str,value: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            db.execute('INSERT INTO diagnostic_requests VALUES (%s,%s)',(reference,encode(value)))
    def request(self,reference: str) -> dict[str,Any]:
        with self._database.connection() as db:
            row=db.execute('SELECT diagnostic_request(%s)',(reference,)).fetchone()
            if row is None or row[0] is None:raise LibraryError('Diagnostic request unavailable')
            result: dict[str,Any]=decode(row[0]);return result
    def publish(self,value: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:db.execute('SELECT diagnostic_publish(%s)',(encode(value),))


def source_binding(db: DeadlineConnection, task: Task) -> dict[str,Any] | None:
    if not task.source:return None
    from library.codec import decode
    row=db.execute('SELECT i.binding,r.payload FROM source_inputs i JOIN source_registry r ON r.id=i.source '
        'WHERE i.source=%s AND i.entity=%s',(task.source,task.entity)).fetchone()
    if row is None:return None
    registration=decode(row[1])
    if registration['queue']!=task.stage:raise LibraryError('Invalid native registration queue')
    if row[0] is None:
        if registration.get('native'):raise LibraryError('Native source binding missing')
        return None
    binding: dict[str,Any]=decode(row[0])
    if binding.get('registration_kind')!=registration.get('kind','monitor'):
        raise LibraryError('Invalid native registration binding')
    return binding


def native_validation(*,tasks: list[Task],db: DeadlineConnection) -> tuple[str,set[str]]:
    values=[];keys:set[str]=set()
    for task in tasks:
        if not task.source:continue
        binding=source_binding(db,task)
        if binding is not None:
            keys.update(native_input_guards(binding))
            values.append([task.key,binding,native_input(db,binding,task.stage)])
        else:values.append([task.key,None])
    return digest(values),keys
