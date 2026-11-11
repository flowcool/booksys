"""Read-only public diagnostic pagination on the prepared isolated authority."""
import argparse
import json
import hashlib
import subprocess
from datetime import datetime,timezone
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from library.compose_diagnostics import RemoteSource,refresh_sources
from library.diagnostic_reader import DiagnosticReader,Filter
from library.log_sources import COMPOSE_ROLES
from library.postgres import Database
from library.pg_import import target_identity

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root',type=Path,required=True)
parser.add_argument('--expected-logs',type=Path,nargs=2,required=True)
parser.add_argument('--evidence',type=Path,required=True)
args=parser.parse_args()
root=args.root
if not root.is_absolute() or not root.is_relative_to('/data/tmp') or any(p.is_symlink() for p in (root,*root.parents)):
    raise SystemExit('Only an exact prepared isolated root is allowed')
preparation=json.loads((root/'preparation.json').read_text())
if preparation['root']!=str(root) or not preparation['project'].startswith('booksys-prfpw-qualification-'):
    raise SystemExit('Isolated fixture binding differs')
database=Database.configured(root/'library/state')
with database.connection() as db:
    authority=db.execute('SELECT authority_id FROM meta').fetchone()
if authority!=('t5-isolated-reader',):
    raise SystemExit('Only the isolated reader authority is allowed')
inputs={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in args.expected_logs}
rows_by_role={}
for path in args.expected_logs:
    role=next((name for name in ('pyload','byparr') if path.name in (name+'-logs.json',name+'-resume-logs.json')),None)
    if role is None or role in rows_by_role:raise SystemExit('Bind one exact log input for each retained dependency')
    rows_by_role[role]=json.loads(path.read_text())['rows']
expected_by_role={role:{row[1]['event_id'] for row in rows} for role,rows in rows_by_role.items()}
if (set(expected_by_role)!={'pyload','byparr'} or any(not ids for ids in expected_by_role.values())
        or expected_by_role['pyload'] & expected_by_role['byparr']):
    raise SystemExit('Distinct valid event identities are required for both retained dependencies')
expected_rows=[row for rows in rows_by_role.values() for row in rows]
expected=set().union(*expected_by_role.values())
os.environ['BOOKSYS_READER_SOCKET']=str(root/'reader-ipc/reader.sock')
sources=[RemoteSource(name,root/'library/state') for name in COMPOSE_ROLES]
refresh_sources(sources)
reader=DiagnosticReader(sources,root/'diagnostic-pages.lock')
now=time.time()
query=Filter(min(row[0] for row in expected_rows)-1,min(now,max(row[0] for row in expected_rows)+1))
args.evidence.mkdir(parents=True,exist_ok=False)
seen:set[str]=set();cursor=None;pages=[]
for index in range(32):
    started=time.monotonic()
    page=reader.query(query,cursor=cursor,older=True)
    elapsed=time.monotonic()-started
    (args.evidence/f'page-{index:02}.json').write_text(json.dumps(page,indent=2)+'\n')
    if elapsed>.5:raise RuntimeError('Operator page exceeded the 500 ms budget')
    if any(gap.get('source') in expected_by_role and gap.get('reason')=='unavailable' for gap in page['gaps']):
        raise RuntimeError('A required retained dependency was unavailable during pagination')
    pages.append({'index':index,'elapsed_seconds':elapsed,'state':page['state'],'events':len(page['events'])})
    seen.update(row['event_id'] for row in page['events'])
    if expected<=seen:break
    cursor=page['cursor']
    if cursor is None:raise RuntimeError('Public pagination did not return retained expected logs')
else:raise RuntimeError('Public continuation made insufficient progress')
(args.evidence/'result.json').write_text(json.dumps({'result':'PASS','target':target_identity(database),
    'candidate':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
    'application_sha256':__import__('library.authority',fromlist=['code_digest']).code_digest(),
    'runtime':sys.version,'at':datetime.now(timezone.utc).isoformat(),'inputs':inputs,
    'query':{'start':query.start,'end':query.end,'older':True},
    'generations':{s.name:s.generation for s in sources},'expected_event_ids':sorted(expected),
    'expected_by_role':{role:sorted(ids) for role,ids in expected_by_role.items()},
    'seen_event_ids':sorted(seen),'pages':pages},indent=2)+'\n')
print('PASS public diagnostic pages return exact retained events within the operator budget')
