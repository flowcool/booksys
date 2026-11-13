"""Concrete operational domain tables in the Booksys authority database."""
from library.postgres import Database, DeadlineConnection
from library.primitives import LibraryError,digest
from pathlib import Path
from typing import Any, Callable
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


def completion_guards(result: OperationalCompletion) -> set[str]:
    if result.kind == 'admission':
        return {'copy-operation:'+result.kind+':'+result.identity}
    scopes={({'routing':'routing-package','retirement':'retirement-acquisition'}.get(result.kind,result.kind))+':'+result.identity}
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
    if result.kind == 'admission':
        row=db.execute('SELECT payload FROM copy_operations WHERE namespace=%s AND id=%s',(result.kind,result.identity)).fetchone()
        if (row[0] if row else None)!=result.expected:
            raise LibraryError('Copy operation result conflict')
        db.execute('UPDATE copy_operations SET payload=%s WHERE namespace=%s AND id=%s',(encode(result.value),result.kind,result.identity))
        return
    tables={'category':'collection_categories','fiche':'fiche_progress','routing':'routing_packages','retirement':'retirement_acquisitions'}
    if result.kind not in tables:
        raise LibraryError('Unsupported operational completion')
    table=tables[result.kind];column='pid' if result.kind=='routing' else 'source'
    row=db.execute(f'SELECT payload FROM {table} WHERE {column}=%s',(result.identity,)).fetchone()
    if (row[0] if row else None)!=result.expected:
        raise LibraryError('Operational result conflict')
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
    db.execute('CREATE TABLE dependency_readiness(name TEXT PRIMARY KEY,available BOOLEAN NOT NULL,at DOUBLE PRECISION NOT NULL,recovered DOUBLE PRECISION NOT NULL,url TEXT NOT NULL,next_probe DOUBLE PRECISION NOT NULL DEFAULT 0)')
    for table in ('captured_sources','collection_categories','collection_submissions','fiche_progress'):
        db.execute(f'CREATE TABLE {table}(source TEXT COLLATE \"C\" PRIMARY KEY,payload TEXT NOT NULL)')
    db.execute('CREATE TABLE captured_downloads(source TEXT NOT NULL REFERENCES captured_sources(source),target TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(source,target))')
    db.execute('CREATE TABLE captured_download_links(source TEXT NOT NULL,target TEXT NOT NULL,position BIGINT NOT NULL,url TEXT NOT NULL,PRIMARY KEY(source,target,position),FOREIGN KEY(source,target) REFERENCES captured_downloads(source,target))')
    db.execute('CREATE TABLE collection_checkpoints(kind TEXT PRIMARY KEY,payload TEXT NOT NULL)')
    db.execute('CREATE TABLE network_budgets(scope TEXT PRIMARY KEY,requests INTEGER NOT NULL,period DOUBLE PRECISION NOT NULL,spacing DOUBLE PRECISION NOT NULL,review_block BOOLEAN NOT NULL,last DOUBLE PRECISION NOT NULL,not_before DOUBLE PRECISION NOT NULL)')
    db.execute('CREATE TABLE network_starts(scope TEXT NOT NULL REFERENCES network_budgets(scope),position BIGINT GENERATED ALWAYS AS IDENTITY,at DOUBLE PRECISION NOT NULL,PRIMARY KEY(scope,position))')
    db.execute('CREATE TABLE routing_scheduler(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)')
    db.execute('CREATE TABLE cycle_status(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)')
    db.execute('CREATE TABLE retirement_observation(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)')
    db.execute('CREATE TABLE copy_operations(namespace TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(namespace,id))')
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
    db.execute('CREATE TABLE stage_discovery(kind TEXT PRIMARY KEY CHECK(kind IN (\'business\',\'intake\')),after_key TEXT NOT NULL)')


class OperationalState:
    def __init__(self, database: Database):
        self._database = database

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
        def load(db: DeadlineConnection) -> Any:
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
        from library.versioned import cached
        value: dict[str, Any] = cached(self._database, 'captured', ('captured_sources', 'captured_downloads', 'captured_download_links'), load)
        return value

    def capture(self, source: str, record: dict[str,Any]) -> None:
        # Parsing, browser I/O and serialization finish before this transaction.
        payload=encode({**record,**({'downloads':{}} if 'downloads' in record else {})})
        with self._database.connection(write=True) as db:
            guards(db,{'captured-source:'+source})
            db.execute('INSERT INTO captured_sources VALUES (%s,%s) ON CONFLICT(source) DO UPDATE SET payload=excluded.payload',
                       (source,payload))
            db.execute('DELETE FROM captured_download_links WHERE source=%s',(source,))
            db.execute('DELETE FROM captured_downloads WHERE source=%s',(source,))
            for target,download in record.get('downloads',{}).items():
                db.execute('INSERT INTO captured_downloads VALUES (%s,%s,%s)',(source,target,encode({**download,**({'urls':[]} if 'urls' in download else {})})))
                for index,url in enumerate(download.get('urls',[])):
                    db.execute('INSERT INTO captured_download_links VALUES (%s,%s,%s,%s)',(source,target,index,url))

    def monitor(self) -> dict[str,Any]:
        def load(db: DeadlineConnection) -> Any:
            result={'version':1,'categories':{source:decode(payload) for source,payload in db.execute(
                'SELECT source,payload FROM collection_categories ORDER BY source')},'submissions':{
                source:decode(payload) for source,payload in db.execute('SELECT source,payload FROM collection_submissions ORDER BY source')}}
            result.update(self._checkpoints(db))
            return result
        from library.versioned import cached
        value: dict[str, Any] = cached(self._database, 'monitor', ('collection_categories', 'collection_submissions', 'collection_checkpoints'), load)
        return value

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
        result={kind:decode(payload) for kind,payload in db.execute("SELECT kind,payload FROM collection_checkpoints WHERE kind='schedule'")}
        return result

    @staticmethod
    def _checkpoint(db: DeadlineConnection, kind: str, value: Any) -> None:
        if value is None:
            db.execute('DELETE FROM collection_checkpoints WHERE kind=%s',(kind,))
            return
        metadata=copy.deepcopy(value)
        db.execute('INSERT INTO collection_checkpoints VALUES (%s,%s) ON CONFLICT(kind) DO UPDATE SET payload=excluded.payload',(kind,encode(metadata)))

    def patch_monitor(self, **changes: Any) -> None:
        if set(changes)-{'schedule'}:
            raise LibraryError('Invalid collection checkpoint')
        with self._database.connection(write=True) as db:
            guards(db,{'collection-checkpoint:'+kind for kind in changes})
            for kind,value in changes.items():
                self._checkpoint(db,kind,value)

    def fiches(self) -> dict[str,Any]:
        def load(db: DeadlineConnection) -> Any:
            return {'version':1,'items':{source:decode(payload) for source,payload in
                    db.execute('SELECT source,payload FROM fiche_progress ORDER BY source')}}
        from library.versioned import cached
        value: dict[str, Any] = cached(self._database, 'fiches', ('fiche_progress',), load)
        return value

    def fiche(self, source: str, record: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            guards(db,completion_guards(OperationalCompletion('fiche',source,None,record)))
            db.execute('INSERT INTO fiche_progress VALUES (%s,%s) ON CONFLICT(source) DO UPDATE SET payload=excluded.payload',(source,encode(record)))

    def prepare_completion(self, kind: str, identity: str, value: dict[str,Any], *, expected: tuple[str | None] | None = None) -> OperationalCompletion:
        tables={'category':'collection_categories','fiche':'fiche_progress','routing':'routing_packages','retirement':'retirement_acquisitions'}
        if kind not in tables:
            raise LibraryError('Unsupported operational completion')
        column='pid' if kind=='routing' else 'source'
        with self._database.connection() as db:
            row=db.execute(f'SELECT payload FROM {tables[kind]} WHERE {column}=%s',(identity,)).fetchone()
            return OperationalCompletion(kind,identity,expected[0] if expected is not None else (row[0] if row else None),value)

    def native_view(self, queue: str, kind: str) -> dict[str,Any]:
        from library.native_projection import project
        if kind != 'monitor':
            raise LibraryError('Invalid native source kind')
        def load(db: DeadlineConnection) -> Any:
            monitor={'version':1,'categories':{source:decode(payload) for source,payload in db.execute(
                'SELECT source,payload FROM collection_categories ORDER BY source')},'submissions':{
                source:decode(payload) for source,payload in db.execute('SELECT source,payload FROM collection_submissions ORDER BY source')}}
            monitor.update(self._checkpoints(db))
            fiches={'items':{source:decode(payload) for source,payload in db.execute('SELECT source,payload FROM fiche_progress')}}
            return monitor,fiches
        from library.versioned import cached
        monitor,fiches=cached(self._database,'native-view-inputs',('collection_categories','collection_submissions','collection_checkpoints',
            'fiche_progress'),load)
        view: dict[str,Any]=project(queue,monitor,auxiliary=fiches)
        return view


def native_input_guards(binding: dict[str,Any]) -> set[str]:
    """The same concrete keys guard canonical writers and native task claims."""
    kind,identity=binding['kind'],binding['identity']
    if kind=='category':
        return {'category:'+identity,'collection-checkpoint:schedule'}
    if kind=='fiche':return {'submission:'+identity,'fiche:'+identity,'collection-checkpoint:schedule'}
    if kind=='submission':return {'submission:'+identity}
    raise LibraryError('Invalid native source binding')


def native_input(db: DeadlineConnection, binding: dict[str,Any], queue: str, *, active: bool=False) -> tuple[str,str,str] | None:
    """Read one bound canonical source. Never read the complete monitor."""
    from library.native_projection import project
    kind,identity=binding['kind'],binding['identity']
    if binding.get('registration_kind')!='monitor':
        raise LibraryError('Invalid native registration binding')
    monitor: dict[str,Any]={'version':1,'categories':{},'submissions':{}}
    auxiliary: dict[str,Any]={'items':{}}
    table='collection_categories' if kind=='category' else 'collection_submissions'
    row=db.execute(f'SELECT payload FROM {table} WHERE source=%s',(identity,)).fetchone()
    if row is None:return None
    record=decode(row[0])
    monitor['categories' if kind=='category' else 'submissions'][identity]=record
    if kind in ('category','fiche'):
        schedule=db.execute("SELECT payload FROM collection_checkpoints WHERE kind='schedule'").fetchone()
        if schedule:monitor['schedule']=decode(schedule[0])
    if kind=='category':
        if active and digest(record)!=binding['original']:return None
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


_QUEUE_CACHE: dict[tuple[str,str],tuple[float,list[dict[str,Any]]]]={}

_PUBLISHED: dict[tuple[str,str],str]={}


class RuntimeObservation:
    """One observer publishes a coherent pyLoad readback and its conservative bound."""
    def __init__(self, database: Database):
        self._database=database

    def publish(self, packages: list[dict[str,Any]], *, at: float, remaining: int | None, overshoot: int | None) -> None:
        # The same snapshot every minute rewrote about 2300 rows (delete and insert) for nothing: this process is the only
        # publisher, so an identical snapshot only refreshes the observation time.
        fingerprint=digest(packages)
        unchanged=_PUBLISHED.get((self._database.dsn,self._database.schema))==fingerprint
        with self._database.connection(write=True) as db:
            guards(db,{'native-observation'})
            if not unchanged:
                db.execute('DELETE FROM observed_links')
                db.execute('DELETE FROM observed_packages')
            # Two statements for the whole queue: thousands of single inserts exceeded the SQL deadline.
            if packages and not unchanged:
                db.execute('INSERT INTO observed_packages SELECT * FROM unnest(%s::bigint[],%s::bigint[],%s::text[])',
                           ([p['pid'] for p in packages], list(range(len(packages))),
                            [encode({k:v for k,v in p.items() if k!='links'}) for p in packages]))
                rows=[(p['pid'],i,encode(link)) for p in packages for i,link in enumerate(p.get('links',[]))]
                if rows:
                    db.execute('INSERT INTO observed_links SELECT * FROM unnest(%s::bigint[],%s::bigint[],%s::text[])',
                               ([r[0] for r in rows],[r[1] for r in rows],[r[2] for r in rows]))
            db.execute('INSERT INTO native_observation VALUES (1,%s,%s,%s) ON CONFLICT(singleton) DO UPDATE SET observed_at=excluded.observed_at,remaining_bytes=excluded.remaining_bytes,overshoot_bytes=excluded.overshoot_bytes',(at,remaining,overshoot))
        _PUBLISHED[(self._database.dsn,self._database.schema)]=fingerprint

    def queue(self) -> tuple[list[dict[str,Any]],float]:
        with self._database.connection() as db:
            header=db.execute('SELECT observed_at FROM native_observation WHERE singleton=1').fetchone()
            if header is None:
                raise LibraryError('Native observation unavailable')
            key=(self._database.dsn,self._database.schema)
            cached=_QUEUE_CACHE.get(key)
            if cached is not None and cached[0]==header[0]:
                # The snapshot is replaced only by a newer publish; decoding it again for
                # every readiness check cost half of a retirement step.
                return copy.deepcopy(cached[1]),header[0]
            links: dict[int,list[dict[str,Any]]]={}
            for pid,payload in db.execute('SELECT pid,payload FROM observed_links ORDER BY pid,position'):
                links.setdefault(pid,[]).append(decode(payload))
            value=[{**decode(payload),'links':links.get(pid,[])} for pid,payload in db.execute('SELECT pid,payload FROM observed_packages ORDER BY position')]
            _QUEUE_CACHE[key]=(header[0],copy.deepcopy(value))
            return value,header[0]

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
        def load(db: DeadlineConnection) -> Any:
            return {'schema':1,'packages':{key:decode(payload) for key,payload in db.execute('SELECT pid,payload FROM routing_packages ORDER BY pid')}}
        from library.versioned import cached
        value: dict[str, Any] = cached(self._database, 'routing', ('routing_packages',), load)
        return value

    def route(self, pid: str, value: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            guards(db,{'routing-package:'+pid})
            db.execute('INSERT INTO routing_packages VALUES (%s,%s) ON CONFLICT(pid) DO UPDATE SET payload=excluded.payload',(pid,encode(value)))

    def retirements(self) -> dict[str,Any]:
        def load(db: DeadlineConnection) -> Any:
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
        from library.versioned import cached
        value: dict[str, Any] = cached(self._database, 'retirements', ('retirement_acquisitions', 'retired_packages', 'retired_links', 'retirement_cursor'), load)
        return value

    def retire(self, source: str, value: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            guards(db,{'retirement-acquisition:'+source})
            retain_retirement(db,source,value)

    def retirement_cursor(self, source: str) -> None:
        with self._database.connection(write=True) as db:
            db.execute('INSERT INTO retirement_cursor VALUES (1,%s) ON CONFLICT(singleton) DO UPDATE SET source=excluded.source',(source,))


class CopyOperations:
    def __init__(self, database: Database, namespace: str):
        if namespace != 'admission':
            raise LibraryError('Invalid copy operation scope')
        self._database=database;self.namespace=namespace

    def get(self, identity: str) -> dict[str,Any] | None:
        with self._database.connection() as db:
            row=db.execute('SELECT payload FROM copy_operations WHERE namespace=%s AND id=%s',(self.namespace,identity)).fetchone()
            result: dict[str,Any] | None=decode(row[0]) if row else None
            return result

    def records(self) -> list[dict[str,Any]]:
        def load(db: DeadlineConnection) -> Any:
            return [decode(row[0]) for row in db.execute('SELECT payload FROM copy_operations WHERE namespace=%s ORDER BY id',(self.namespace,))]
        from library.versioned import cached
        records: list[dict[str, Any]] = cached(self._database, 'copy-records:' + self.namespace, ('copy_operations',), load)
        return records

    def for_asset(self, asset: str, applies: Callable[[dict[str,Any],str],bool]) -> list[dict[str,Any]]:
        """Validate every journal's scope; transfer full payloads only for this asset."""
        with self._database.connection() as db:
            rows = db.execute("SELECT json_build_object('asset',payload::json->'asset',"
                "'kind',payload::json->'kind','target',payload::json->'target')::text,"
                "CASE WHEN payload::json->>'asset'=%s THEN payload END "
                "FROM copy_operations WHERE namespace=%s ORDER BY id", (asset,self.namespace))
            records = []
            for scope, payload in rows:
                if applies(decode(scope),asset):
                    records.append(decode(payload))
            return records

    def prepare(self, value: dict[str,Any]) -> OperationalCompletion:
        with self._database.connection() as db:
            row=db.execute('SELECT payload FROM copy_operations WHERE namespace=%s AND id=%s',(self.namespace,value['id'])).fetchone()
            return OperationalCompletion(self.namespace,value['id'],row[0] if row else None,value)

    def phase(self, value: dict[str,Any]) -> None:
        with self._database.connection(write=True) as db:
            guards(db,{'copy-operation:'+self.namespace+':'+value['id']})
            db.execute('INSERT INTO copy_operations VALUES (%s,%s,%s) ON CONFLICT(namespace,id) DO UPDATE SET payload=excluded.payload',(self.namespace,value['id'],encode(value)))


class StatusObservations:
    def __init__(self, database: Database):self._database=database
    def cycle(self) -> dict[str,Any]:return self._read('cycle_status')
    def retirement(self) -> dict[str,Any]:return self._read('retirement_observation')
    def routing(self) -> dict[str,Any]:return self._read('routing_scheduler')
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
