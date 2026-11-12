"""Fixed reader operations with private per-job input grants.

The trusted host supplies image and roots. Callers supply only operation/job.
The base class is used by isolated qualification; runtime adds retained grants.
"""
from __future__ import annotations

from pathlib import Path
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
import re
import subprocess
import stat
from typing import Any, BinaryIO

from library.primitives import atomic_yaml, sync_dir
from library.process import bounded_run
import yaml
from library.operational_state import ReaderJobs


class JobError(ValueError):
    pass


def input_fd(root: Path, relative: Path) -> int:
    """Pin the input inode without following a changed parent symlink."""
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)


def file_hash(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(fd), 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def materialize(incoming: BinaryIO, outgoing: BinaryIO) -> None:
    """Bound dirty NFS pages charged to the small launcher cgroup.

    fsync completes each batch before more dirty pages can accumulate. The
    advisory eviction drops completed cache; source and output hashes still
    establish integrity before Docker receives any input.
    """
    while chunk := incoming.read(8*1024**2):
        outgoing.write(chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())
        os.posix_fadvise(outgoing.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)


class JobLauncher:
    def __init__(self, inputs: Path, jobs: Path, image: str, *, job_uid: int | None = None,
                 cgroup_parent: str | None = None, remove_stopped: bool = True,
                 allowed_prefixes: tuple[str, ...] | None = None):
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
            raise JobError('Immutable local image required')
        for root in (inputs, jobs):
            if (not root.is_absolute() or not root.is_dir() or
                    any(part.is_symlink() for part in (root, *root.parents)) or
                    any(char in str(root) for char in ',\n')):
                raise JobError('Existing absolute non-symlink roots required')
        self.inputs, self.jobs, self.image = inputs, jobs, image
        self.job_uid = os.getuid() if job_uid is None else job_uid
        self.cgroup_parent, self.remove_stopped = cgroup_parent, remove_stopped
        self.allowed_prefixes = allowed_prefixes
        self.retained: ReaderJobs | None = None

    def retained_read(self, job: Path, kind: str) -> dict[str,Any] | None:
        if self.retained is not None:
            value=self.retained.read(job.name,kind)
            path=job/(kind+'.yaml')
            if value is None:
                if path.exists():
                    raise JobError('Reader evidence needs forward phase reconciliation')
                return None
            retained=dict(value)
            proof=retained.pop('_evidence',None)
            if kind=='cleanup' and proof is not None:
                from library.codec import encode
                allowed={str(path),str(job/('cleanup-'+hashlib.sha256(encode(retained).encode()).hexdigest()+'.yaml'))}
                if proof.get('path') not in allowed:raise JobError('Reader cleanup evidence path differs')
                path=Path(proof['path'])
            if proof is None or (kind!='cleanup' and proof.get('path')!=str(path)) or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=proof.get('sha256'):
                raise JobError('Reader immutable evidence differs')
            return retained
        path=job/(kind+'.yaml')
        return yaml.safe_load(path.read_text()) if path.exists() else None

    def retained_write(self, job: Path, kind: str, value: dict[str,Any]) -> None:
        if self.retained is not None:
            path=job/(kind+'.yaml')
            if kind=='cleanup':
                from library.codec import encode
                path=job/('cleanup-'+hashlib.sha256(encode(value).encode()).hexdigest()+'.yaml')
            if path.exists():
                if path.is_symlink() or yaml.safe_load(path.read_text())!=value:
                    raise JobError('Retained reader evidence differs')
            else:
                atomic_yaml(path,value,exclusive=True)
            self.retained.retain(job.name,kind,{**value,'_evidence':{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}})
        else:
            atomic_yaml(job/(kind+'.yaml'),value,exclusive=True)

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        if (set(request) != {'operation', 'job'} or not isinstance(request['operation'], str) or
                request['operation'] not in ('validate', 'bundle', 'ocr', 'proof', 'transform') or
                not isinstance(request['job'], str) or
                not re.fullmatch(r'[0-9a-f]{32}', request['job'])):
            raise JobError('Only an allowed operation and an owned job reference are accepted')
        job = self.jobs / request['job']
        if job.is_symlink() or not job.is_dir() or job.stat().st_uid != self.job_uid or job.stat().st_mode & 0o077:
            raise JobError('Unknown job')
        lockfd = os.open(job/'launcher.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise JobError('Job already owned') from exc
            with ExitStack() as grants:
                return self._execute(job, request['operation'], grants)
        finally:
            os.close(lockfd)

    def _execute(self, job: Path, operation: str, grants: ExitStack) -> dict[str, Any]:
        manifest_path = job/'manifest.json'
        if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 65536:
            raise JobError('Missing or oversized manifest')
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
        if (not isinstance(manifest, dict) or set(manifest) != {'operation', 'input', 'sha256', 'payload'} or
                manifest['operation'] != operation or not isinstance(manifest['input'], str) or
                not isinstance(manifest['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', manifest['sha256']) or
                not isinstance(manifest['payload'], dict)):
            raise JobError('Invalid retained manifest')
        relative = Path(manifest['input'])
        if relative.is_absolute() or '..' in relative.parts or str(relative) != manifest['input']:
            raise JobError('Input outside declared root')
        if self.allowed_prefixes and not manifest['input'].startswith(self.allowed_prefixes):
            raise JobError('Input is outside allowed media namespaces')
        source = self.inputs/relative
        if any(part.is_symlink() for part in (source, *source.parents)) or not source.is_file():
            raise JobError('Input is not a declared regular file')
        if source.suffix.lower() not in (('.cbz', '.cbr', '.pdf', '.zip', '.rar') if operation == 'bundle' else ('.cbz', '.cbr', '.pdf')):
            raise JobError('Unsupported source')
        payload = manifest['payload']
        argv = ['-m', 'library.reader_worker', '/input', source.suffix.lower()]
        stdin = b''
        timeout = 45
        part_records: list[dict[str, Any]] = []
        if operation == 'proof':
            if set(payload) != {'page'} or type(payload['page']) is not int or not 1 <= payload['page'] <= 2000:
                raise JobError('Invalid proof index')
            argv = ['-m', 'library.proof_worker', '/input', source.suffix.lower(), str(payload['page'])]
            timeout = 180
        elif operation == 'ocr':
            if set(payload) != {'pages'} or not isinstance(payload['pages'], list) or not 1 <= len(payload['pages']) <= 8:
                raise JobError('Invalid OCR window')
            argv = ['-m', 'library.printed_worker', '/input', source.suffix.lower()]
            stdin = json.dumps(payload).encode()
            if len(stdin) > 16384:
                raise JobError('OCR request exceeds worker bound')
            timeout = 300
        elif operation == 'transform':
            import base64
            from library.xmlmetadata import validate_xml
            if set(payload) != {'comicinfo'} or not isinstance(payload['comicinfo'], str):
                raise JobError('Invalid metadata request')
            validate_xml(base64.b64decode(payload['comicinfo'], validate=True))
            argv += ['--transform', '/work/book.cbz']
            stdin = json.dumps(payload).encode()
            timeout = 1800 if source.suffix.lower() == '.pdf' else 60
        elif operation == 'bundle':
            if payload:
                if set(payload) != {'parts'} or not isinstance(payload['parts'], list) or not 2 <= len(payload['parts']) <= 64:
                    raise JobError('Invalid multipart set')
                part_records = payload['parts']
                if any(not isinstance(p, dict) or set(p) != {'input', 'sha256'}
                        or not isinstance(p['input'], str) or not isinstance(p['sha256'], str)
                        or not re.fullmatch('[0-9a-f]{64}', p['sha256']) for p in part_records):
                    raise JobError('Invalid multipart grant')
                paths = [Path(p['input']) for p in part_records]
                names = [re.fullmatch(r'(.+)\.part([0-9]{1,3})\.rar', p.name, re.I) for p in paths]
                if (paths[0] != relative or part_records[0]['sha256'] != manifest['sha256']
                        or any(p.parent != relative.parent or p.is_absolute() or '..' in p.parts for p in paths)
                        or any(m is None for m in names) or len({m[1] for m in names if m}) != 1
                        or [int(m[2]) for m in names if m] != list(range(1, len(paths)+1))):
                    raise JobError('Incomplete or mixed multipart set')
                argv = ['-m', 'library.bundle_worker', '/parts/'+source.name, '/work',
                        *['/parts/'+p.name for p in paths]]
            else:
                argv = ['-m', 'library.bundle_worker', '/input', '/work']
            timeout = 180
        elif payload:
            raise JobError('Validation payload is not accepted')
        fd = input_fd(self.inputs, relative)
        grants.callback(os.close, fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= (4 if operation == 'bundle' else 1)*1024**3:
            raise JobError('Source exceeds reader ceiling')
        from library.readers import sha256
        if file_hash(fd) != manifest['sha256']:
            raise JobError('Input changed before launch')
        binding = hashlib.sha256(raw).hexdigest()
        receipt = job/'receipt.yaml'
        value=self.retained_read(job,'receipt')
        if value is not None:
            if value['manifest_sha256'] != binding or not value['stopped']:
                raise JobError('Retained job requires reconciliation')
            return dict(value)
        if self.retained_read(job,'intent') is not None:
            raise JobError('Interrupted job requires exact worker reconciliation')
        output = job/'work'
        output.mkdir(mode=0o700)
        # runc rejects /proc/<launcher>/fd bind sources on this host. Materialize
        # one verified grant in the private job area instead. Runtime adoption
        # requires this area on qualified NAS storage and a copy-space grant.
        grant = job/'input'
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), 'rb') as original, grant.open('xb') as isolated:
            materialize(original, isolated)
        grant.chmod(0o444)
        if sha256(grant) != manifest['sha256']:
            raise JobError('Materialized grant differs from retained input')
        name = 'booksys-reader-'+job.name
        command = ['docker', 'create', '--interactive', '--name', name, '--network', 'none', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--user', f'{os.getuid()}:{os.getgid()}', '--memory', '1536m', '--memory-swap', '1536m',
            '--cpus', '1', '--pids-limit', '32', '--env', 'TMPDIR=/work',
            '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777',
            '--mount', f'type=bind,src={grant},dst=/input,readonly',
            '--mount', f'type=bind,src={output},dst=/work',
            '--entrypoint', '/app/.venv/bin/python', self.image, *argv]
        extra_parts = []
        if part_records:
            command[2:2] = ['--mount', f'type=bind,src={grant},dst=/parts/{source.name},readonly']
            directory = job/'parts'
            directory.mkdir(mode=0o700)
            total = before.st_size
            for part in part_records[1:]:
                part_path = self.inputs/part['input']
                part_fd = input_fd(self.inputs, Path(part['input']))
                grants.callback(os.close, part_fd)
                stamp = os.fstat(part_fd)
                if not stat.S_ISREG(stamp.st_mode) or stamp.st_size <= 0 or file_hash(part_fd) != part['sha256']:
                    raise JobError('Multipart input changed')
                total += stamp.st_size
                if total > 4*1024**3:
                    raise JobError('Multipart source exceeds total reservation')
                isolated = directory/part_path.name
                os.lseek(part_fd, 0, os.SEEK_SET)
                with os.fdopen(os.dup(part_fd), 'rb') as incoming, isolated.open('xb') as outgoing:
                    materialize(incoming, outgoing)
                isolated.chmod(0o444)
                if sha256(isolated) != part['sha256']:
                    raise JobError('Multipart private grant differs')
                command[2:2] = ['--mount', f'type=bind,src={isolated},dst=/parts/{part_path.name},readonly']
                extra_parts.append((part_fd, part_path, stamp, part['sha256']))
        if self.cgroup_parent:
            command[2:2] = ['--cgroup-parent', self.cgroup_parent]
        intent = {'schema': 1, 'name': name, 'image': self.image, 'manifest_sha256': binding}
        command[2:2] = ['--label', 'booksys.reader.job='+job.name,
                         '--label', 'booksys.reader.manifest='+binding]
        self.retained_write(job,'intent',intent)
        created = subprocess.run(command, capture_output=True, timeout=20, check=False)
        if created.returncode:
            raise JobError('Create result needs reconciliation; retained intent')
        container = created.stdout.decode().strip()
        if not re.fullmatch('[0-9a-f]{64}', container):
            raise JobError('Create identity is uncertain')
        self.retained_write(job,'container',{**intent,'container':container})
        result: dict[str, Any] = {**intent, 'container': container, 'stopped': False, 'status': 'blocked'}
        def check_output() -> None:
            total = 0
            for path in output.rglob('*'):
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise JobError('Reader produced a special file')
                total += info.st_size
                if total > 4*1024**3:
                    raise JobError('Reader exceeded total output reservation')
        try:
            completed = bounded_run(['docker', 'start', '--attach', '--interactive', container], timeout=timeout,
                                    input=stdin, stdout_limit=4*1024**2, stderr_limit=65536, check=check_output)
            check_output()
            result['exit_code'] = completed.returncode
            if completed.returncode == 0:
                result['outcome'] = json.loads(completed.stdout)
                result['status'] = 'completed'
            else:
                result['status'] = 'worker_failed'
        finally:
            state = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', container],
                                   capture_output=True, timeout=10, check=False)
            if state.returncode:
                raise JobError('Worker stop is unproved; retain grant and output')
            if json.loads(state.stdout)['Running']:
                subprocess.run(['docker', 'kill', container], capture_output=True, timeout=10, check=False)
            state = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', container],
                                   capture_output=True, timeout=10, check=False)
            if state.returncode or json.loads(state.stdout)['Running']:
                raise JobError('Worker stop is unproved; retain grant and output')
            result.update(stopped=True, worker_state=json.loads(state.stdout))
            after = source.lstat()
            if (file_hash(fd) != manifest['sha256'] or sha256(source) != manifest['sha256'] or
                    (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns) !=
                    (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)):
                result['status'] = 'input_changed'
            for part_fd, part_path, stamp, checksum in extra_parts:
                current = part_path.lstat()
                if (file_hash(part_fd) != checksum or sha256(part_path) != checksum
                        or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
                        != (stamp.st_dev, stamp.st_ino, stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns)):
                    result['status'] = 'input_changed'
            self.retained_write(job,'receipt',result)
            if self.remove_stopped:
                self.remove_terminal(job, container)
        return result

    def remove_terminal(self, job: Path, container: str) -> None:
        """Retain evidence before exact stopped-worker removal; retry is safe."""
        retained=self.retained_read(job,'container') if self.retained is not None else None
        if self.retained is not None and (retained is None or retained.get('container')!=container):
            raise JobError('Cleanup container differs from retained ownership')
        logs = bounded_run(['docker', 'logs', '--timestamps', container], timeout=10,
                           stdout_limit=4*1024**2, stderr_limit=65536)
        if logs.returncode:
            raise JobError('Worker logs unavailable; retain stopped container')
        for filename, raw_log in (('worker.stdout.log', logs.stdout),
                                  ('worker.stderr.log', logs.stderr)):
            path = job/filename
            if path.exists():
                if path.is_symlink() or path.read_bytes() != raw_log:
                    raise JobError('Retained worker log differs')
                continue
            with path.open('xb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw_log)
                stream.flush()
                os.fsync(stream.fileno())
        sync_dir(job)
        state = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', container],
                               capture_output=True, timeout=10, check=False)
        if state.returncode or json.loads(state.stdout).get('Running') is not False:
            raise JobError('Worker termination before removal is unproved')
        removed = subprocess.run(['docker', 'rm', container], capture_output=True, timeout=10, check=False)
        cleanup={'container':container,'removed':removed.returncode==0}
        if retained is not None:self.retained_write(job,'cleanup',{**retained,**cleanup})
        else:atomic_yaml(job/'cleanup.yaml',cleanup)
        # A failed removal does not turn a durable business result into a retry.
        # Reconciliation can retry cleanup of the same stopped identity.
