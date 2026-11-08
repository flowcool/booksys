"""Allowlisted Activity projection and shared observation. HTTP only calls project()."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
import re
import time
from typing import Any

from activity import Activity, STAGES, STATES, WINDOW, read_activity, read_yaml, reference, text, write_yaml, prune

LABELS = dict(zip(STAGES, ('Collecte Bookys', 'Byparr', 'Téléchargements pyLoad',
    'Traitement local', 'Identification, classement et publication', 'Retrait des paquets')))
# Only pinned pyLoad enum labels, never arbitrary plugin error strings.
DOWNLOAD_STATUS = {0: 'Finished', 1: 'Offline', 2: 'Online', 3: 'Queued', 4: 'Skipped',
    5: 'Hoster wait', 6: 'Temporary failure', 7: 'Starting', 8: 'Aborted', 9: 'Decrypting',
    10: 'Processing', 11: 'Unknown', 12: 'Downloading', 13: 'Failed', 14: 'Missing', 15: 'Password', 16: 'Checking'}
ITEM_FIELDS = ('id', 'title', 'state', 'reason', 'operation', 'observed_at', 'since',
               'waiting_seconds', 'next_attempt', 'fiche')
CURRENT_FIELDS = ('queue_observed_at', 'state', 'operation', 'item', 'title', 'reason', 'observed_at', 'since',
                  'last_progress_at', 'next_attempt')


def number(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and 0 <= value < 1e15 else None


def allowed(value: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in names:
        v = value.get(name)
        if name in ('observed_at', 'since', 'last_progress_at', 'next_attempt', 'waiting_seconds', 'queue_observed_at'):
            result[name] = number(v)
        elif name == 'fiche':
            if re.fullmatch(r'/fiche/[0-9a-f]{64}', str(v)):
                result[name] = v
        else:
            result[name] = text(v)
    return result


def project(snapshot: dict[str, Any], *, timestamp: float | None = None) -> dict[str, Any]:
    stamp = time.time() if timestamp is None else timestamp
    stages = {}
    for name in STAGES:
        raw = snapshot.get('stages', {}).get(name, {})
        stage = allowed(raw, CURRENT_FIELDS)
        observed = number(raw.get('observed_at'))
        age = max(0, stamp - observed) if observed is not None else None
        freshness = ('unavailable' if raw.get('unavailable') or raw.get('freshness') == 'unavailable' else 'missing' if age is None else
                     'stale' if age > 90 else 'current')
        state = raw.get('state', 'unknown')
        if state not in STATES or freshness != 'current' or raw.get('orphaned'):
            state = 'unknown'
        last = number(raw.get('last_progress_at'))
        stage.update(label=LABELS[name], state=state, freshness=freshness, age_seconds=age,
            no_progress_seconds=max(0, stamp - last) if last is not None else None,
            waiting_seconds=max(0, stamp - stage['since']) if state in ('waiting', 'blocked') and stage['since'] is not None else None,
            counts={text(k): v for k, v in raw.get('counts', {}).items() if type(v) is int and v >= 0},
            items=[allowed(v, ITEM_FIELDS) for v in raw.get('items', [])[:2000]],
            details_truncated=bool(raw.get('details_truncated')))
        stages[name] = stage
    events = {}
    for eid, event in snapshot.get('events', {}).items():
        at = number(event.get('at'))
        if at is not None and stamp - WINDOW <= at <= stamp and re.fullmatch('[0-9a-f]{64}', eid):
            events[eid] = {**allowed(event, CURRENT_FIELDS), 'at': at, 'stage': text(event.get('stage'))}
    resources = {}
    for key in ('data', 'byparr_memory', 'services'):
        raw = snapshot.get('resources', {}).get(key, {})
        observed = number(raw.get('observed_at'))
        resources[key] = {**allowed(raw, ('observed_at', 'reason')), 'freshness':
            'unavailable' if raw.get('unavailable') or raw.get('freshness') == 'unavailable' else 'missing' if observed is None else
            'stale' if stamp - observed > 90 else 'current'}
        for metric in ('free_bytes', 'used_bytes', 'limit_bytes'):
            if metric in raw:
                resources[key][metric] = number(raw[metric])
        if key == 'services':
            resources[key]['states'] = {k: text(raw.get('states', {}).get(k, 'unknown'))
                for k in ('collector', 'byparr', 'pyload', 'library')}
    return {'schema': 1, 'observed_at': number(snapshot.get('observed_at')), 'stages': stages,
            'resources': resources, 'events': events, 'history_started_at': number(snapshot.get('history_started_at'))}


def load_public(root: Path) -> dict[str, Any]:
    return project(read_yaml(root / 'evidence/activity-dashboard.yaml'))


def title(record: dict[str, Any], default: str) -> str:
    return text(record.get('title') or default)


class Observer:
    def __init__(self, root: Path, collector: Path, *, clock: Callable[[], float] = time.time):
        self.root, self.collector, self.clock = root, collector, clock
        self.directory = root / 'evidence/activity'
        self.saved = read_yaml(root / 'evidence/activity-dashboard.yaml')
        self.downloads = self.saved.get('stages', {}).get('downloads', {})
        self.resources = self.saved.get('resources', {})
        self.browser = read_yaml(self.root / 'evidence/browser-session.yaml')
        self.container_started: dict[str, float] = {}
        self.started = self.saved.get('history_started_at', clock())

    def sample_downloads(self, client: Any) -> None:
        queue = client.call('get_queue_data')
        active = {int(v['fid']): v for v in client.call('status_downloads')}
        if not isinstance(queue, list):
            raise ValueError('Invalid queue observation')
        stamp = self.clock()
        submissions = read_yaml(self.collector / 'monitor.yaml').get('submissions', {})
        captured = read_yaml(self.collector / 'state.yaml').get('items', {})
        tracked = {v.get('package_id'): source for source, v in submissions.items()}
        routing = read_yaml(self.collector / 'mirror-routing.yaml').get('packages', {})
        for operation in routing.values():
            if operation.get('source') in submissions and isinstance(operation.get('fallback'), dict):
                tracked[operation['fallback'].get('package_id')] = operation['source']
        previous = {v['id']: v for v in self.downloads.get('items', [])}
        items = []
        counts = {'active links': 0, 'waiting links': 0, 'finished links': 0, 'failed links': 0}
        for package in queue:
            source = tracked.get(package.get('pid'))
            if source is None:
                continue
            for link in package.get('links', []):
                status = link.get('status')
                kind = ('finished' if status == 0 else 'failed' if status in (1, 6, 8, 13, 14, 15)
                        else 'active' if status in (7, 9, 10, 12, 16) else 'waiting')
                counts[kind + ' links'] += 1
                ident = str(link['fid'])
                prior = previous.get(ident, {})
                since = prior.get('since') if prior.get('reason') == DOWNLOAD_STATUS.get(status, 'Unknown') else stamp
                item = {'id': ident, 'title': title(captured.get(source, {}), 'Booksys package ' + str(package['pid'])),
                    'state': 'working' if kind == 'active' else 'blocked' if kind == 'failed' else 'sleeping' if kind == 'finished' else 'waiting',
                    'reason': DOWNLOAD_STATUS.get(status, 'Unknown'), 'observed_at': stamp, 'since': since,
                    'next_attempt': number(active.get(link['fid'], {}).get('wait_until')) or None,
                    'waiting_seconds': max(0, stamp - since) if prior and kind == 'waiting' and since is not None else None}
                items.append(item)
        # Progress means bytes or a completed link changed; polling is not progress.
        token = reference([(p.get('pid'), [(v.get('fid'), v.get('status') == 0,
                     active.get(v.get('fid'), {}).get('bleft') if v.get('status') == 12 else None)
                     for v in p.get('links', []) if v.get('status') == 0 or v.get('fid') in active])
                     for p in queue if p.get('pid') in tracked and any(v.get('status') == 0 or v.get('fid') in active for v in p.get('links', []))])
        old_token = self.downloads.get('progress_token')
        progressed = old_token is not None and token != old_token
        recorder = Activity(self.directory, 'downloads', clock=self.clock)
        recorder.report('working' if counts['active links'] else 'blocked' if counts['failed links'] else 'waiting',
                        'queue observation', progressed=progressed)
        self.downloads = {**read_activity(self.directory, 'downloads').get('current', {}), 'items': items[:2000],
                          'details_truncated': len(items) > 2000, 'counts': counts, 'progress_token': token}

    def downloads_unavailable(self) -> None:
        self.downloads = {**self.downloads, 'unavailable': True}

    def sample_resources(self) -> None:
        import json
        import shutil
        import subprocess
        stamp = self.clock()
        def run(*args: str) -> str:
            return subprocess.run(args, capture_output=True, text=True, check=True, timeout=5).stdout
        try:
            self.resources['data'] = {'observed_at': stamp, 'free_bytes': shutil.disk_usage('/data').free}
        except OSError:
            self.resources['data'] = {**self.resources.get('data', {}), 'unavailable': True}
        try:
            # Fixed local identities; no environment, labels, mounts or Docker API in HTTP.
            values = json.loads(run('docker', 'inspect', '--format', '{{json .State}}', 'booksys-worker-1'))
            states = {'collector': values['Status']}
            self.container_started['collection'] = datetime.fromisoformat(values['StartedAt'].replace('Z', '+00:00')).timestamp()
            for container, key in (('booksys-byparr-1', 'byparr'), ('booksys-pyload', 'pyload')):
                state = json.loads(run('docker', 'inspect', '--format', '{{json .State}}', container))
                if key == 'byparr':
                    self.container_started['byparr'] = datetime.fromisoformat(state['StartedAt'].replace('Z', '+00:00')).timestamp()
                states[key] = state.get('Health', {}).get('Status', state['Status'])
            states['library'] = 'running'  # This observer runs inside the existing library service.
            self.resources['services'] = {'observed_at': stamp, 'states': states}
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            self.resources['services'] = {**self.resources.get('services', {}), 'unavailable': True}
        try:
            health = json.loads(run('docker', 'exec', 'booksys-byparr-1', 'curl', '--fail', '--silent', '--max-time', '3', 'http://127.0.0.1:8191/health'))
            generation = int(health['generation'])
            requests = int(health['requests'])
            if self.browser.get('generation') is not None and generation != self.browser['generation']:
                Activity(self.directory, 'byparr', clock=self.clock).report('sleeping', 'Browser session renewed')
            self.browser = {'observed_at': stamp, 'generation': generation, 'requests': requests}
            write_yaml(self.root / 'evidence/browser-session.yaml', self.browser)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            self.browser = {**self.browser, 'unavailable': True}
        try:
            metrics = json.loads(run('docker', 'stats', '--no-stream', '--format', '{{json .}}', 'booksys-byparr-1'))
            def bytes_value(value: str) -> int:
                match = re.fullmatch(r'([0-9.]+)([kMGT]?i?B)', value.strip())
                if not match:
                    raise ValueError('Unknown memory unit')
                scale = {'B': 1, 'kB': 1000, 'MB': 10**6, 'GB': 10**9, 'TB': 10**12,
                         'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3, 'TiB': 1024**4}
                return int(float(match[1]) * scale[match[2]])
            used, limit = metrics['MemUsage'].split('/')
            self.resources['byparr_memory'] = {'observed_at': stamp, 'used_bytes': bytes_value(used), 'limit_bytes': bytes_value(limit)}
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            self.resources['byparr_memory'] = {**self.resources.get('byparr_memory', {}), 'unavailable': True}

    def snapshot(self) -> dict[str, Any]:
        stamp = self.clock()
        stages: dict[str, Any] = {}
        events = {}
        for stage in STAGES:
            directory = self.collector / 'activity' if stage in ('collection', 'byparr') else self.directory
            prune(directory, stage, stamp)
            record = read_activity(directory, stage)
            current = dict(record.get('current', {}))
            current['orphaned'] = current.get('session_started', 0) < self.container_started.get('collection', 0) if stage in ('collection', 'byparr') else False
            for eid, event in record.get('events', {}).items():
                if event.get('at', 0) >= stamp - WINDOW:
                    events[eid] = {**event, 'stage': stage}
            if stage in ('local', 'identity', 'retirement') and current.get('state') in ('working', 'waiting'):
                try:
                    current['orphaned'] = Path('/proc/' + str(current['pid']) + '/stat').read_text().split()[21] != current.get('process_start')
                except (OSError, KeyError, IndexError):
                    current['orphaned'] = True
            service = 'byparr' if stage == 'byparr' else 'collector' if stage == 'collection' else 'library'
            observed_service = self.resources.get('services', {})
            if not observed_service.get('unavailable') and observed_service.get('states', {}).get(service) in ('exited', 'stopped', 'inactive', 'dead'):
                current.update(state='stopped', observed_at=observed_service.get('observed_at'))
            if stage == 'byparr' and self.browser:
                current['counts'] = {k: self.browser[k] for k in ('generation', 'requests') if type(self.browser.get(k)) is int}
                current['queue_observed_at'] = self.browser.get('observed_at')
                for eid, event in read_activity(self.directory, 'byparr').get('events', {}).items():
                    events[eid] = {**event, 'stage': 'byparr'}
            stages[stage] = current
        stages['downloads'] = dict(self.downloads)
        self.business(stages, stamp)
        value = {'observed_at': stamp, 'history_started_at': self.started, 'stages': stages,
                 'events': events, 'resources': self.resources}
        return project(value, timestamp=stamp)

    def business(self, stages: dict[str, Any], stamp: float) -> None:
        submissions = read_yaml(self.collector / 'monitor.yaml').get('submissions', {})
        captured = read_yaml(self.collector / 'state.yaml').get('items', {})
        for name in ('collection', 'byparr'):
            matched = next((v for k, v in captured.items() if reference(k) == stages[name].get('item')), None)
            if matched:
                stages[name]['title'] = title(matched, 'Bookys source')
        backfill = read_yaml(self.collector / 'backfill.yaml')
        transmitted = sum(v.get('status') == 'queued' for v in submissions.values())
        stages['collection']['counts'] = {'captured source entries': len(captured), 'transmitted source entries': transmitted}
        if backfill:
            pending = sum(submissions.get(s, {}).get('status') != 'queued' for s in backfill.get('items', {}))
            stages['collection']['counts']['remaining backfill source entries'] = pending
            stages['collection']['items'] = [{'id': reference(s), 'title': title(captured.get(s, {}), text(v)),
                'reason': 'Backfill source pending', 'state': 'waiting', 'observed_at': stamp}
                for s, v in backfill.get('items', {}).items() if submissions.get(s, {}).get('status') != 'queued'][:2000]
        state = read_yaml(self.root / 'state/library.yaml')
        origins, assets = state.get('origins', {}), state.get('assets', {})
        local, identity = [], []
        for oid, origin in origins.items():
            asset = assets.get(origin.get('sha256'), {})
            automation = asset.get('automation', {})
            label = title(captured.get(origin.get('source'), {}), Path(origin.get('path', 'Unknown file')).name)
            base = {'id': oid, 'title': label, 'fiche': '/fiche/' + oid, 'observed_at': stamp}
            if origin.get('availability') != 'validated':
                local.append({**base, 'state': 'blocked' if origin.get('availability') == 'rejected' else 'waiting',
                    'reason': 'Local validation rejected' if origin.get('availability') == 'rejected' else
                        'Intake budget deferred' if origin.get('reason') == 'Intake budget deferred' else 'Waiting for complete validated inputs'})
            if asset:
                admission = asset.get('admission', {})
                identity.append({**base, 'id': asset['sha256'], 'state': 'sleeping' if admission else 'waiting',
                    'reason': 'Copy ' + text(admission.get('status')) if admission else
                        'Operator decision pending' if asset.get('resolution') else
                        'Metadata retry scheduled' if automation.get('next_attempt') else 'Identification pending',
                    'next_attempt': automation.get('next_attempt')})
        stages['local']['queue_observed_at'] = stamp if state else None
        stages['identity']['queue_observed_at'] = stamp if state else None
        stages['local'].update(counts={'waiting files': len(local), 'validated files': sum(o.get('availability') == 'validated' for o in origins.values())}, items=local[:2000])
        unique = {v['id']: v for v in identity}
        stages['identity'].update(counts={'assets': len(assets), 'waiting assets': sum(v['state'] == 'waiting' for v in unique.values()),
            'resulting copies': sum(bool(a.get('admission')) for a in assets.values())}, items=list(unique.values())[:2000])
        history = read_yaml(self.collector / 'package-retirement.yaml').get('acquisitions', {})
        outcomes = read_yaml(self.root / 'evidence/retirement-cycle.yaml').get('results', [])
        items = {reference(s): {'id': reference(s), 'title': title(captured.get(s, {}), 'Booksys acquisition'),
            'state': 'sleeping' if v.get('phase') == 'retired' else 'blocked',
            'reason': 'Removed; local inputs retained' if v.get('phase') == 'retired' else 'Retirement intent requires reconciliation',
            'observed_at': stamp} for s, v in history.items()}
        for v in outcomes:
            ident = reference(v.get('source'))
            items.setdefault(ident, {'id': ident, 'title': title(captured.get(v.get('source'), {}), 'Booksys acquisition'),
                'state': 'waiting', 'reason': 'Retained: ' + text(v.get('reason')) if v.get('reason') in ('Target processing is active', 'Target link processing is active or unknown', 'Mirror routing is not settled', 'Extraction or deletion hooks are not qualified') else 'Retained; technical gates not satisfied' if v.get('outcome') == 'blocked' else 'Eligible at last inspection' if v.get('outcome') == 'eligible' else 'Retirement outcome requires inspection', 'observed_at': stamp})
        stages['retirement']['queue_observed_at'] = stamp if history or outcomes else None
        stages['collection']['queue_observed_at'] = stamp if captured or submissions else None
        stages['downloads']['queue_observed_at'] = self.downloads.get('observed_at')
        stages['retirement'].update(counts={'removed acquisitions': sum(v.get('phase') == 'retired' for v in history.values()),
            'eligible acquisitions (last inspection)': sum(v.get('outcome') == 'eligible' for v in outcomes),
            'retained acquisitions (last inspection)': sum(v.get('outcome') == 'blocked' for v in outcomes)}, items=list(items.values())[:2000])

    def save(self) -> None:
        value = self.snapshot()
        # Keep the progress token private but persistent across observer restarts.
        value['stages']['downloads']['progress_token'] = self.downloads.get('progress_token')
        write_yaml(self.root / 'evidence/activity-dashboard.yaml', value)
