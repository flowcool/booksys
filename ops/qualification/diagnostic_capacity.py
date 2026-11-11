"""HTTP diagnostic capacity with isolated retained files and real pipe parsing.

The command shim supplies only owned fixture bytes. It does not measure Docker
or journald daemon latency. Live source availability remains a separate lab gate.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.parse
import urllib.request
from unittest.mock import patch


def feed(path: Path, start: float, end: float) -> None:
    meta = json.loads(path.with_suffix('.json').read_text())
    index = max(0, int(start - meta['start']))
    with path.open('rb') as source:
        source.seek(min(index, meta['count']) * meta['width'])
        for number in range(index, meta['count']):
            if meta['start'] + number > end:
                break
            sys.stdout.buffer.write(source.read(meta['width']))


def run(root: Path, destination: Path) -> dict[str, Any]:
    from booksys_core.diagnostics import event
    from library.api import PublicAPI
    from library.diagnostic_reader import DiagnosticReader
    from library.http_runtime import HTTPRuntime
    from library.log_sources import LiveSource, UNIT, timestamp
    from library.migration import manifest
    from library.authority import code_digest
    descriptor = json.loads((root / 'dataset.json').read_text())
    state = root / 'candidate/state'
    if state.resolve().is_relative_to('/data/booksys'):
        raise ValueError('No live state is allowed')
    manifest(state, descriptor['authority'], 'sqlite')
    destination.mkdir(mode=0o700)
    start = int(time.time()) - 86000
    sources, files, inventories = [], {}, []
    for name in ('worker', 'byparr', 'library'):
        path = destination / (name + '.log')
        value = event(service='library' if name == 'library' else 'byparr' if name == 'byparr' else 'collector',
            stage='local', code='progress', reason='none', refs=[{'kind':'source','id':'a' * 64}])
        # 24 hours of retained lines. Each daemon fixture has at least3x10MiB.
        count = 86000
        def encoded(number: int) -> bytes:
            value['event_id'] = f'00000000-0000-4000-8000-{number:012d}'
            value['at'] = datetime.fromtimestamp(start + number, timezone.utc).isoformat()
            message = json.dumps(value, separators=(',', ':'))
            if name == 'library':
                raw = json.dumps({'_SYSTEMD_USER_UNIT': UNIT, '_UID': str(os.getuid()),
                    '_TRANSPORT': 'stdout', '__REALTIME_TIMESTAMP': str((start + number) * 1000000), 'MESSAGE': message})
            else:
                raw = datetime.fromtimestamp(start + number, timezone.utc).isoformat() + ' ' + message
            return raw.encode().ljust(768, b' ') + b'\n'
        with path.open('xb') as output:
            for number in range(count):
                output.write(encoded(number))
        path.with_suffix('.json').write_text(json.dumps({'start':start,'count':count,'width':769}))
        source = LiveSource(name); source.available = True
        source.generation = source.container = 'fixture-' + name
        sources.append(source); files[source.container] = path
        inventories.append({'source':name,'bytes':path.stat().st_size,'lines':count})
    reader = DiagnosticReader(sources, destination / 'reader.lock')
    api = PublicAPI(root / 'candidate', host='', reception=root / 'no-downloads', diagnostic_reader=reader)
    original = subprocess.Popen
    def popen(command: list[str], *args: Any, **kwargs: Any) -> Any:
        if command[:2] == ['docker', 'logs']:
            path = files[command[-1]]
            since = timestamp(command[command.index('--since') + 1]); until = timestamp(command[command.index('--until') + 1])
        elif command and command[0] == 'journalctl':
            path = files['fixture-library']
            since = float(next(v.split('@')[1] for v in command if v.startswith('--since=')))
            until = float(next(v.split('@')[1] for v in command if v.startswith('--until=')))
        else:
            return original(command, *args, **kwargs)
        script = r'import json,sys; from pathlib import Path; p=Path(sys.argv[1]); m=json.loads(p.with_suffix(".json").read_text()); i=max(0,int(float(sys.argv[2])-m["start"])); n=max(0,min(m["count"]-i,int(float(sys.argv[3])-m["start"])-i+1)); f=p.open("rb"); f.seek(i*m["width"]); remaining=n*m["width"]; exec("while remaining>0:\n b=f.read(min(65536,remaining))\n if not b: break\n sys.stdout.buffer.write(b); remaining-=len(b)")'
        return original([sys.executable, '-c', script, str(path), str(since), str(until)], *args, **kwargs)
    runtime = HTTPRuntime(('127.0.0.1',0), api)
    def query(first: float, cursor: str | None = None, **extra: str) -> str:
        values = {'from':datetime.fromtimestamp(first,timezone.utc).isoformat(),
                  'to':datetime.fromtimestamp(start + 85999,timezone.utc).isoformat(), **extra}
        if cursor:
            values['cursor'] = cursor
        return '/api/diagnostics?' + urllib.parse.urlencode(values)
    def request(path: str) -> dict[str, Any]:
        began = time.perf_counter()
        with urllib.request.urlopen('http://127.0.0.1:' + str(runtime.address[1]) + path, timeout=2) as response:
            raw = response.read()
        return {'ms':(time.perf_counter()-began)*1000,'bytes':len(raw),'page':json.loads(raw)}
    result: dict[str, Any] = {'code_sha256':code_digest(), 'retained':inventories,
        'coverage':'Actual HTTP/IPC/LiveSource parsing with isolated daemon-command shim; excludes live daemon latency', 'loads':{}}
    with patch('subprocess.Popen', new=popen):
        runtime.start()
        try:
            complete = query(start + 85990)
            continued = query(start, reason='browser_busy')
            for viewers in (1,4):
                result['loads'][str(viewers)] = {}
                for name, route in [('complete',complete),('continued',continued)]:
                    for _ in range(5): request(route)
                    with ThreadPoolExecutor(max_workers=viewers) as clients:
                        rows = list(clients.map(lambda _:request(route),range(100)))
                    result['loads'][str(viewers)][name] = {'max_ms':max(r['ms'] for r in rows),
                        'max_bytes':max(r['bytes'] for r in rows), 'states':sorted({r['page']['state'] for r in rows}), 'sample_gaps':rows[0]['page'].get('gaps',[]),
                        'outliers_ms':[r['ms'] for r in rows if r['ms']>=2000]}
            first = request(continued)['page']
            if first.get('cursor'):
                next_page = request(query(start,cursor=first['cursor'],reason='browser_busy'))
                result['continuation'] = {'ms':next_page['ms'],'state':next_page['page']['state']}
                sources[0].generation = 'recreated-fixture'
                gap = request(query(start,cursor=first['cursor'],reason='browser_busy'))
                result['rotation'] = {'ms':gap['ms'],'state':gap['page']['state'],'gaps':gap['page']['gaps']}
            result['peak_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            result['under_2000ms'] = all(v['max_ms']<2000 for load in result['loads'].values() for v in load.values()) and all(load['complete']['states'] == ['complete'] and load['continued']['states'] == ['continued'] for load in result['loads'].values()) and bool(result.get('rotation', {}).get('gaps'))
        finally:
            runtime.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--feed',type=Path);parser.add_argument('--start',type=float);parser.add_argument('--end',type=float)
    parser.add_argument('--dataset',type=Path);parser.add_argument('--destination',type=Path);parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.feed:
        feed(args.feed,args.start,args.end);return
    result=run(args.dataset,args.destination)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__': main()
