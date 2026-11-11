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
    5: 'Hoster wait', 6: 'Temporary failure', 7: 'Starting', 8: 'Failed', 9: 'Aborted',
    10: 'Decrypting', 11: 'Custom', 12: 'Downloading', 13: 'Processing', 14: 'Unknown'}
LOCAL_REASONS = ('Intake budget deferred', 'Transfer is not confirmed finished',
    'Skipped mirror link', 'Offline mirror link', 'Failed mirror link',
    'Finished origin is missing or renamed', 'Ambiguous canonical origin references',
    'Unsupported or ambiguous suffix', 'Multipart representation',
    'Multiple image groups: possible multi-work archive', 'Reader policy differs from controller')

ITEM_FIELDS = ('id', 'title', 'state', 'reason', 'operation', 'observed_at', 'since',
               'waiting_seconds', 'next_attempt', 'fiche')
CURRENT_FIELDS = ('queue_observed_at', 'scope', 'state', 'operation', 'item', 'title', 'reason', 'observed_at', 'since',
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
        heartbeat = bool(raw.get('heartbeat_source') or raw.get('session'))
        tolerance = 45 if heartbeat else 90
        freshness = ('unavailable' if raw.get('unavailable') or raw.get('freshness') == 'unavailable' else 'missing' if age is None else
                     'stale' if age > tolerance else 'current')
        state = raw.get('state', 'unknown')
        if state not in STATES or freshness != 'current' or raw.get('orphaned'):
            state = 'unknown'
        last = number(raw.get('last_progress_at'))
        queue_at = number(raw.get('queue_observed_at'))
        queue_freshness = raw.get('queue_freshness')
        if queue_freshness != 'unavailable':
            queue_freshness = 'missing' if queue_at is None else 'stale' if stamp - queue_at > 90 else 'current'
        stage.update(heartbeat_source=heartbeat, sources=[{
            'producer_scope': source['producer_scope'],
            **{key: number(source.get(key)) for key in ('observed_at', 'progress_at', 'session_started')}}
            for source in raw.get('sources', [])
            if isinstance(source, dict) and source.get('producer_scope') in ('collector', 'observer', 'controller')], label=LABELS[name], state=state, freshness=freshness, age_seconds=age, queue_freshness=queue_freshness,
            no_progress_seconds=max(0, stamp - last) if last is not None else None,
            waiting_seconds=max(0, stamp - stage['since']) if state in ('waiting', 'blocked') and stage['since'] is not None else None,
            counts={text(k): v for k, v in raw.get('counts', {}).items() if type(v) is int and v >= 0},
            items=[allowed(v, ITEM_FIELDS) for v in raw.get('items', [])[:2000]],
            details_truncated=bool(raw.get('details_truncated')))
        stages[name] = stage
    events = {}
    for eid, event in snapshot.get('events', {}).items():
        at = number(event.get('at'))
        if at is not None and stamp - WINDOW <= at <= stamp and re.fullmatch(r'(?:[0-9a-f]{64}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})', eid):
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
    from library.stage_observation import public_queues
    return {'schema': 1, 'observed_at': number(snapshot.get('observed_at')), 'stages': stages,
            'queues': public_queues(snapshot.get('queues', {}), timestamp=stamp),
            'resources': resources, 'events': events, 'history_started_at': number(snapshot.get('history_started_at'))}


def load_public(root: Path) -> dict[str, Any]:
    return project(status_store(root).activity())


class PublicSnapshot:
    """Publish one filtered observation; request-time work only ages metadata.

    The observer replaces the complete value atomically. Readers retain their
    immutable local reference while the next observation is prepared.
    """
    def __init__(self, root: Path):
        self.update(status_store(root).activity())

    def update(self, raw: dict[str, Any]) -> None:
        from library.presentation import public_value
        value = public_value(project(raw))
        items = {name: stage['items'] for name, stage in value['stages'].items()}
        metadata = {**value, 'events': {}, 'stages': {
            name: {**stage, 'items': []} for name, stage in value['stages'].items()}}
        self._snapshot = (metadata, items, value['events'])

    def response(self) -> Any:
        import json
        from library.ipc import EncodedJSON
        stamp = time.time()
        metadata, items, events = self._snapshot
        value = project(metadata, timestamp=stamp)
        for name, stage in value['stages'].items():
            stage['items'] = items[name]
        value['events'] = {key: event for key, event in events.items()
                           if stamp - WINDOW <= event['at'] <= stamp}
        return EncodedJSON(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                      separators=(',', ':')).encode())


def title(record: dict[str, Any], default: str) -> str:
    return text(record.get('title') or default)


from library.operational_state import CollectorState,NativeHistory,StatusObservations
from library.postgres import Database

def status_store(root: Path) -> StatusObservations:
    return StatusObservations(Database.configured(root/'state'))


class Observer:
    def __init__(self, root: Path, collector: Path, *, clock: Callable[[], float] = time.time):
        self.root, self.collector, self.clock = root, collector, clock
        self.directory = root / 'evidence/activity'
        self.saved = status_store(root).activity()
        self.downloads = self.saved.get('stages', {}).get('downloads', {})
        self.resources = self.saved.get('resources', {})
        self.browser = status_store(self.root).browser()
        self.container_started: dict[str, float] = {}
        self.started = self.saved.get('history_started_at', clock())

    def sample_downloads(self, client: Any) -> None:
        queue = client.call('get_queue_data')
        active = {int(v['fid']): v for v in client.call('status_downloads')}
        if not isinstance(queue, list):
            raise ValueError('Invalid queue observation')
        stamp = self.clock()
        monitor = CollectorState(self.collector).monitor()
        records = CollectorState(self.collector).captured()
        if not isinstance(monitor.get('submissions'), dict) or not isinstance(records.get('items'), dict):
            raise ValueError('Booksys ownership observation unavailable')
        submissions, captured = monitor['submissions'], records['items']
        tracked = {v.get('package_id'): source for source, v in submissions.items()}
        routing = NativeHistory(self.collector).routing().get('packages', {})
        for operation in routing.values():
            source = operation.get('source')
            if (source in submissions and submissions[source].get('package_id') == operation.get('pid')
                    and type(operation.get('attempt_pid')) is int):
                tracked[operation['attempt_pid']] = source
        previous = {v['id']: v for v in self.downloads.get('items', [])}
        items: list[dict[str, Any]] = []
        counts = {'active links': 0, 'waiting links': 0, 'finished links': 0, 'failed links': 0}
        for package in queue:
            source = tracked.get(package.get('pid'))
            if source is None:
                continue
            for link in package.get('links', []):
                status = link.get('status')
                kind = ('finished' if status == 0 else 'failed' if status in (1, 6, 8, 9)
                        else 'active' if status in (7, 10, 12, 13) else 'waiting')
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
        # Progress requires a newly completed link or fewer bytes left on the same transfer.
        previous_finished = {k for k, v in previous.items() if v.get('reason') == 'Finished'}
        finished = {v['id'] for v in items if v['reason'] == 'Finished'}
        byte_counts = {str(k): number(v.get('bleft')) for k, v in active.items() if str(k) in {i['id'] for i in items}}
        prior_bytes = self.downloads.get('_bytes_left', {})
        progressed = bool(previous and finished - previous_finished) or any(
            count is not None and number(prior_bytes.get(k)) is not None and count < prior_bytes[k]
            for k, count in byte_counts.items())
        recorder = Activity(self.directory, 'downloads', clock=self.clock, scope='observer')
        for item in items:
            prior = previous.get(item['id'])
            if prior and prior.get('reason') != item['reason']:
                recorder.report(item['state'], 'Link state changed', item=item['id'], title=item['title'],
                                reason=item['reason'], progressed=item['reason'] == 'Finished')
        recorder.report('working' if counts['active links'] else 'blocked' if counts['failed links'] else 'waiting' if counts['waiting links'] else 'sleeping',
                        'queue observation', progressed=progressed)
        self.downloads = {**read_activity(self.directory, 'downloads').get('current', {}), 'items': items[:2000],
                          'details_truncated': len(items) > 2000, 'counts': counts, '_bytes_left': byte_counts}

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
                Activity(self.directory, 'byparr', clock=self.clock, scope='observer').report('sleeping', 'Browser session renewed')
            self.browser = {'observed_at': stamp, 'generation': generation, 'requests': requests}
            status_store(self.root).save_browser(self.browser)
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

    def snapshot(self, *, retirement: dict[str, Any] | None = None) -> dict[str, Any]:
        stamp = self.clock()
        stages: dict[str, Any] = {}
        events = {}
        for stage in STAGES:
            directory = self.collector / 'activity' if stage in ('collection', 'byparr') else self.directory
            prune(directory, stage, stamp)
            record = read_activity(directory, stage)
            current = dict(record.get('current', {}))
            current['sources'] = record.get('sources', [])
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
        from library.primitives import LibraryError
        try:
            self.business(stages, stamp, retirement=retirement)
        except (LibraryError,OSError,ValueError,KeyError,TypeError):
            for name in ('collection','local','identity','retirement'):
                previous=self.saved.get('stages',{}).get(name,{})
                for field in ('counts','items','queue_observed_at','details_truncated'):
                    stages[name][field]=previous.get(field,{} if field=='counts' else [] if field=='items' else None)
                stages[name]['queue_freshness']='unavailable'
        value = {'observed_at': stamp, 'history_started_at': self.started, 'stages': stages,
                 'events': events, 'resources': self.resources}
        return project(value, timestamp=stamp)

    def business(self, stages: dict[str, Any], stamp: float, *, retirement: dict[str, Any] | None = None) -> None:
        submissions = CollectorState(self.collector).monitor().get('submissions', {})
        captured = CollectorState(self.collector).captured().get('items', {})
        for name in ('collection', 'byparr'):
            matched = next((v for k, v in captured.items() if reference(k) == stages[name].get('item')), None)
            if matched:
                stages[name]['title'] = title(matched, 'Bookys source')
        backfill = (CollectorState(self.collector).backfill() or {})
        transmitted = sum(v.get('status') == 'queued' for v in submissions.values())
        collection_valid = isinstance(submissions,dict) and isinstance(captured,dict)
        if 'remaining backfill source entries' in self.saved.get('stages', {}).get('collection', {}).get('counts', {}) and not backfill:
            collection_valid = False
        stages['collection']['scope'] = 'backfill' if backfill and backfill.get('status') != 'complete' else 'new entries'
        stages['collection']['counts'] = {'captured source entries': len(captured), 'transmitted source entries': transmitted}
        if backfill:
            pending = sum(submissions.get(s, {}).get('status') != 'queued' for s in backfill.get('items', {}))
            stages['collection']['counts']['remaining backfill source entries'] = pending
            stages['collection']['items'] = [{'id': reference(s), 'title': title(captured.get(s, {}), text(v)),
                'reason': 'Backfill source pending', 'state': 'waiting', 'observed_at': stamp}
                for s, v in backfill.get('items', {}).items() if submissions.get(s, {}).get('status') != 'queued'][:2000]
        from library.catalog import Catalog
        catalogue = Catalog(self.root / 'state')
        backend = catalogue.backend()
        # Acquire metadata once; HTTP uses only the published filtered observation.
        with backend._database.connection() as db:
            backend.check(db)
            state={kind:{key:backend.restore(payload,media=False) for key,payload in
                db.execute(f'SELECT id,payload FROM {kind} ORDER BY id')} for kind in ('origins','assets')}
        origins, assets = state.get('origins', {}), state.get('assets', {})
        for stage_name in ('local', 'identity'):
            for key, asset in assets.items():
                if reference(key) == stages[stage_name].get('item'):
                    hints = list(asset.get('captured_hints', {}).values())
                    stages[stage_name]['title'] = title(hints[0] if hints else {}, key)
                    break
        local, identity = [], []
        for oid, origin in origins.items():
            asset = assets.get(origin.get('sha256'), {})
            automation = asset.get('automation', {})
            label = title(captured.get(origin.get('source'), {}), Path(origin.get('path', 'Unknown file')).name)
            base = {'id': oid, 'title': label, 'fiche': '/fiche/' + oid, 'observed_at': stamp}
            if origin.get('availability') not in ('validated', 'bundle', 'multipart_member'):
                from library.presentation import terminal_transfer
                terminal = terminal_transfer(origin.get('transfer_status')) if origin.get('availability') == 'pending' else None
                local.append({**base, 'state': terminal[0] if terminal else 'blocked' if origin.get('availability') == 'rejected' else 'waiting',
                    'reason': terminal[1] if terminal else origin['reason'] if origin.get('reason') in LOCAL_REASONS else
                        'Missing multipart input' if str(origin.get('reason', '')).startswith(('Awaiting part', 'Missing part')) else
                        'Local validation rejected' if origin.get('availability') == 'rejected' else 'Waiting for complete validated inputs',
                    'next_attempt': origin.get('intake_next_attempt') or origin.get('bundle_next_attempt')})
            if asset:
                admission = asset.get('admission', {})
                identity.append({**base, 'id': asset['sha256'], 'state': 'sleeping' if admission.get('status') == 'classified' else 'waiting',
                    'reason': 'Copy classified' if admission.get('status') == 'classified' else 'Copy retained; work identification pending' if admission else
                        'Operator decision pending' if asset.get('resolution') else
                        'Metadata retry scheduled' if automation.get('next_attempt') else 'Identification pending',
                    'next_attempt': automation.get('next_attempt')})
        stages['local']['queue_observed_at'] = stamp if state else None
        stages['identity']['queue_observed_at'] = stamp if state else None
        stages['local'].update(counts={'waiting files': sum(v['state'] == 'waiting' for v in local), 'blocked files': sum(v['state'] == 'blocked' for v in local),
            'skipped mirror files': sum(v['reason'] == 'Skipped mirror link' for v in local),
            'unavailable mirror files': sum(v['reason'] in ('Offline mirror link', 'Failed mirror link') for v in local),
            'validated files': sum(o.get('availability') == 'validated' for o in origins.values())}, items=local[:2000])
        unique = {v['id']: v for v in identity}
        stages['identity'].update(counts={'assets': len(assets), 'waiting assets': sum(v['state'] == 'waiting' for v in unique.values()),
            'resulting copies': sum(bool(a.get('admission')) for a in assets.values())}, items=list(unique.values())[:2000])
        if retirement is None:
            from library.retirement_observation import observe
            retirement = observe(self.collector / 'package-retirement.yaml')
        history = retirement.get('acquisitions', {})
        outcomes = status_store(self.root).retirement().get('results', [])
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

        validity = {'collection': collection_valid,
            'local': isinstance(state.get('origins'), dict) and isinstance(state.get('assets'), dict),
            'identity': isinstance(state.get('origins'), dict) and isinstance(state.get('assets'), dict),
            'retirement': isinstance(retirement.get('acquisitions'), dict)
                and isinstance(status_store(self.root).retirement().get('results'), list)}
        for name, valid in validity.items():
            stage = stages[name]
            previous = self.saved.get('stages', {}).get(name, {})
            if not valid:
                for field in ('counts', 'items', 'queue_observed_at', 'details_truncated'):
                    stage[field] = previous.get(field, {} if field == 'counts' else [] if field == 'items' else None)
                stage['queue_freshness'] = 'unavailable'
                continue
            stage['queue_observed_at'] = stamp
            prior_items = {v['id']: v for v in previous.get('items', [])}
            for item in stage.get('items', []):
                prior = prior_items.get(item['id'], {})
                same = prior.get('reason') == item.get('reason') and prior.get('state') == item.get('state')
                item['since'] = prior.get('since', stamp) if same else stamp
                item['waiting_seconds'] = max(0, stamp - item['since']) if same and item.get('state') in ('waiting', 'blocked') else None

    def save(self, *, retirement: dict[str, Any] | None = None) -> None:
        value = self.snapshot(retirement=retirement)
        # Keep byte counters private but persistent across observer restarts.
        value['stages']['downloads']['_bytes_left'] = self.downloads.get('_bytes_left', {})
        status_store(self.root).save_activity(value)
        self.saved = value
