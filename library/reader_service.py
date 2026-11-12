"""Private fixed-operation launcher. Only this service has Docker authority."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
from typing import Any

from library.decoder_ownership import DecoderOwnership
from library.ipc import Channel, IPCError
from library.primitives import LibraryError, atomic_yaml
from library.reader_jobs import JobLauncher, JobError
from library.stage_resources import Resources


class GrantedLauncher(JobLauncher):
    def __init__(self, inputs: Path, jobs: Path, image: str, state: Path, *,
                 job_uid: int, cgroup_parent: str):
        super().__init__(inputs, jobs, image, job_uid=job_uid,
                         cgroup_parent=cgroup_parent, remove_stopped=False,
                         allowed_prefixes=('downloads/', 'library-lab/state/bundle-inputs/', 'library-lab/library/'))
        self.state = state
        from library.operational_state import ReaderJobs
        from library.postgres import Database
        self.retained=ReaderJobs(Database.configured(Path(os.environ['BOOKSYS_LAUNCHER_DATABASE_DIR'])))
        from library.nas_storage import configured_contract, NASMonitor
        contract = configured_contract()
        self.nas = NASMonitor(contract, state=state) if contract else None

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        # Validate the entire public envelope before looking up retained data.
        if (set(request) != {'operation', 'job'} or request['operation'] not in
                ('validate', 'bundle', 'ocr', 'proof', 'transform')
                or not isinstance(request['job'], str) or not re.fullmatch('[0-9a-f]{32}', request['job'])):
            raise JobError('Only fixed operations and owned job references are accepted')
        job = self.jobs/request['job']
        pending = self.state/'reader-requests'/request['job']
        if any(p.is_symlink() for p in (pending, *pending.parents)):
            raise JobError('Symlink in retained reader request')
        assert self.retained is not None
        accepted=self.retained.accepted(job.name)
        raw=accepted['manifest_raw'].encode()
        if __import__('library.codec',fromlist=['decode']).decode(accepted['manifest'])!=json.loads(raw):
            raise JobError('Manifest bytes differ from accepted request')
        if self.nas is not None:
            self.nas.require()
        if not job.exists():
            job.mkdir(mode=0o700)
            with (job/'manifest.json').open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        elif (job/'manifest.json').read_bytes()!=raw:
            raise JobError('Retained manifest bytes differ')
        return super().execute(request)

    def reconcile(self, reference: str) -> dict[str, Any]:
        if not re.fullmatch('[0-9a-f]{32}', reference):
            raise JobError('Invalid job reference')
        job = self.jobs/reference
        if any(p.is_symlink() for p in (job, *job.parents)):
            raise JobError('Symlink in retained job')
        assert self.retained is not None
        # Repair only the exact immutable SQL/file gap. This never launches a worker.
        import yaml
        for kind in ('intent','container','receipt','reconciled'):
            evidence=job/(kind+'.yaml')
            if self.retained.read(reference,kind) is None and evidence.exists():
                if evidence.is_symlink() or evidence.stat().st_size>8*1024**2:
                    raise JobError('Invalid reader phase evidence')
                self.retained_write(job,kind,yaml.safe_load(evidence.read_text()))
        value=self.retained_read(job,'intent')
        if value is None:
            return {'stopped':True,'status':'not_started'}
        expected = 'booksys-reader-'+reference
        if value['name'] != expected or not re.fullmatch(r'sha256:[0-9a-f]{64}', value['image']):
            raise JobError('Retained worker identity drift')
        # Called only by the exclusive service owner after its synchronous
        # execution returned or after the old launcher process lost its lock.
        inspected = subprocess.run(['docker', 'inspect', '--format', '{{json .}}', expected],
                                  capture_output=True, timeout=10)
        if inspected.returncode:
            # A stopped worker can be removed by explicit cleanup or one-shot GC.
            # Daemon failure is not absence. Only the durable exact terminal
            # receipt can prove termination after a confirmed missing object.
            absent = (b'no such object:' in inspected.stderr.lower()
                      or b'no such container:' in inspected.stderr.lower())
            receipt=self.retained_read(job,'receipt')
            retained=self.retained_read(job,'container')
            if absent and receipt is not None and retained is not None:
                if (all(receipt.get(key)==value[key]==retained.get(key) for key in ('name','image','manifest_sha256'))
                    and receipt.get('container')==retained.get('container') and receipt.get('stopped') is True
                    and receipt.get('status') in ('completed','worker_failed','blocked')
                    and isinstance(receipt.get('worker_state'),dict) and receipt['worker_state'].get('Running') is False):
                    return {'stopped':True,'status':'retained_terminal'}
            raise JobError('Worker identity after create intent is unproved')
        if len(inspected.stdout) > 256*1024:
            raise JobError('Worker identity after create intent is unproved')
        worker = json.loads(inspected.stdout)
        labels = worker['Config'].get('Labels') or {}
        if (worker['Image'] != value['image'] or labels.get('booksys.reader.job') != reference
                or labels.get('booksys.reader.manifest') != value['manifest_sha256']):
            raise JobError('Worker identity drift; no stop permitted')
        retained=self.retained_read(job,'container')
        if retained is not None and retained['container']!=worker['Id']:
            raise JobError('Worker was replaced; no stop permitted')
        container = worker['Id']
        if retained is None:
            self.retained_write(job,'container',{**value,'container':container})
        subprocess.run(['docker', 'stop', '--time', '1', container], capture_output=True, timeout=15)
        state = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', container],
                               capture_output=True, timeout=10)
        if state.returncode:
            raise JobError('Worker absence after create intent is unproved')
        status = json.loads(state.stdout)
        if status['Running']:
            raise JobError('Worker termination is unproved')
        self.retained_write(job,'reconciled',{**value,'container':container,'stopped':True,'state':status})
        return {'stopped': True, 'status': 'reconciled'}


class ReaderHandler:
    """One IPC request boundary shared by runtime and isolated qualification."""
    def __init__(self, launcher: GrantedLauncher, state: Path, image: str):
        self.launcher, self.state, self.image = launcher, state, image
        self.admission = threading.Lock()
        self.active_job: str | None = None

    def handle(self, connection: socket.socket) -> None:
        with connection:
            channel = Channel(connection)
            connection.settimeout(2)
            try:
                request = channel.receive()
                if len(json.dumps(request).encode()) > 65536 or set(request) != {'operation', 'job'}:
                    raise JobError('Invalid launcher request')
                connection.settimeout(None)
                if request['operation'] == 'diagnostics':
                    from library.compose_diagnostics import read_request
                    try:
                        result = read_request(self.state, request['job'], os.environ['BOOKSYS_COMPOSE_PROJECT'])
                    except TimeoutError:
                        channel.send({'result':None,'error':'diagnostic_budget_exhausted'})
                        return
                elif request == {'operation': 'probe', 'job': '0'*32}:
                    probe = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', self.image],
                        capture_output=True, timeout=5)
                    result = {'idle': self.active_job is None, 'available': probe.returncode == 0 and probe.stdout.decode().strip() == self.image
                              and (self.launcher.nas is None or self.launcher.nas.ready())}
                else:
                    with self.admission:
                        if self.active_job is not None:
                            # Only a distinct, not-yet-created job proves that
                            # this request cannot have an earlier reader effect.
                            reference = request['job']
                            if (isinstance(reference, str) and re.fullmatch('[0-9a-f]{32}', reference)
                                    and reference != self.active_job and not (self.launcher.jobs/reference).exists()):
                                if request['operation'] == 'reconcile':
                                    channel.send({'result': {'stopped': True, 'status': 'not_started'}, 'error': None})
                                    return
                                if request['operation'] in ('validate', 'bundle', 'ocr', 'proof', 'transform'):
                                    channel.send({'result': None, 'error': 'reader_busy'})
                                    return
                            raise JobError('Reader owner busy; retained effect requires reconciliation')
                        self.active_job = request['job']
                    try:
                        result = (self.launcher.reconcile(request['job']) if request['operation'] == 'reconcile'
                                  else self.launcher.execute(request))
                    finally:
                        with self.admission:
                            self.active_job = None
                channel.send({'result': result, 'error': None})
            except (ValueError, TypeError, KeyError, LibraryError, OSError, subprocess.TimeoutExpired, IPCError):
                try:
                    channel.send({'result': None, 'error': 'Reader unavailable; reconcile retained job before retry'})
                except OSError:
                    pass  # Retained intent/receipt remains authoritative after client loss.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('socket', 'inputs', 'jobs', 'state'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--job-uid', type=int, required=True)
    parser.add_argument('--cgroup-parent', required=True)
    args = parser.parse_args()
    launcher = GrantedLauncher(args.inputs, args.jobs, args.image, args.state,
                               job_uid=args.job_uid, cgroup_parent=args.cgroup_parent)
    # The owner lock is local technical state, not NFS. A restart cannot race
    # a still-live launcher even if its Unix listener disappeared.
    with (args.state/'reader-service.lock').open('a') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from library.compose_diagnostics import observe_sources
        threading.Thread(target=observe_sources, args=(args.state, os.environ['BOOKSYS_COMPOSE_PROJECT'],
            threading.Event()), daemon=True, name='compose-diagnostic-observer').start()
        if args.socket.exists():
            if not args.socket.is_socket():
                raise LibraryError('Launcher socket target changed')
            args.socket.unlink()
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(args.socket))
            os.chmod(args.socket, 0o600)
            listener.listen(8)
            slots = threading.BoundedSemaphore(2)
            handler = ReaderHandler(launcher, args.state, args.image)
            def handle(connection: socket.socket) -> None:
                try:
                    handler.handle(connection)
                finally:
                    slots.release()
            while True:
                connection, _ = listener.accept()
                if not slots.acquire(blocking=False):
                    connection.close()  # Bound connections even during blocked hard-NFS work.
                    continue
                threading.Thread(target=handle, args=(connection,), daemon=True).start()


if __name__ == '__main__':
    main()
