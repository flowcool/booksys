"""Public request and retained-job boundary for the isolated launcher candidate."""
import tempfile
import unittest
from pathlib import Path
import hashlib
import json
import os
import uuid
import zipfile
import subprocess
from unittest.mock import patch
import yaml

from ops.qualification.reader_jobs import JobLauncher, JobError


class RequestBoundaryTests(unittest.TestCase):
    def test_materialization_bounds_unsynced_nfs_bytes_before_decoder_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs, jobs = root/'inputs', root/'jobs'
            inputs.mkdir(); jobs.mkdir()
            source = inputs/'album.cbz'
            source.write_bytes(b'x'*(17*1024**2))
            job = jobs/uuid.uuid4().hex
            job.mkdir(mode=0o700)
            checksum = hashlib.sha256(source.read_bytes()).hexdigest()
            (job/'manifest.json').write_text(json.dumps({'operation': 'validate',
                'input': source.name, 'sha256': checksum, 'payload': {}}))
            synced = 0
            batches = []
            fsync = os.fsync
            def sync(fd):
                nonlocal synced
                if Path('/proc/self/fd/'+str(fd)).resolve() == job/'input':
                    size = os.fstat(fd).st_size
                    batches.append(size-synced)
                    synced = size
                fsync(fd)
            launcher = JobLauncher(inputs, jobs, 'sha256:'+'0'*64)
            failed_create = subprocess.CompletedProcess([], 1, b'', b'test stops before decoder')
            with patch('library.reader_jobs.os.fsync', side_effect=sync), patch(
                    'library.reader_jobs.subprocess.run', return_value=failed_create):
                with self.assertRaises(JobError):
                    launcher.execute({'operation': 'validate', 'job': job.name})
            self.assertEqual(hashlib.sha256((job/'input').read_bytes()).hexdigest(), checksum)
            self.assertEqual(synced, source.stat().st_size)
            self.assertLessEqual(max(batches), 8*1024**2)

    def test_caller_cannot_supply_docker_commands_paths_or_other_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'inputs').mkdir()
            (root/'jobs').mkdir()
            launcher = JobLauncher(root/'inputs', root/'jobs', 'sha256:'+'0'*64)
            for request in [
                {'operation': 'validate', 'job': '../escape'},
                {'operation': 'shell', 'job': 'a'*32},
                {'operation': 'validate', 'job': 'a'*32, 'image': 'other'},
                {'operation': 'validate', 'job': 'a'*32, 'command': 'id'},
                {'operation': 'validate', 'job': 'a'*32, 'input': '/etc/shadow'},
                {'operation': 'validate', 'job': 'a'*32},
            ]:
                with self.subTest(request=request), self.assertRaises(JobError):
                    launcher.execute(request)

    def test_changed_inputs_symlinks_and_manifest_path_escape_cannot_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs, jobs = root/'inputs', root/'jobs'
            inputs.mkdir(); jobs.mkdir()
            (inputs/'album.cbz').write_bytes(b'new content')
            (root/'private.cbz').write_bytes(b'private')
            (inputs/'link.cbz').symlink_to(root/'private.cbz')
            launcher = JobLauncher(inputs, jobs, 'sha256:'+'0'*64)
            for name, expected in [('album.cbz', '0'*64), ('link.cbz', hashlib.sha256(b'private').hexdigest()),
                                   ('../private.cbz', hashlib.sha256(b'private').hexdigest())]:
                job = jobs/uuid.uuid4().hex
                job.mkdir(mode=0o700)
                (job/'manifest.json').write_text(json.dumps({'operation': 'validate', 'input': name,
                    'sha256': expected, 'payload': {}}))
                with self.subTest(name=name), self.assertRaises(JobError):
                    launcher.execute({'operation': 'validate', 'job': job.name})
                self.assertFalse((job/'intent.yaml').exists())


class LifecycleTests(unittest.TestCase):
    def test_receipt_and_logs_precede_exact_stopped_container_removal(self):
        for mode in ('ok', 'logs_failed', 'remove_failed', 'running'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                inputs, jobs = root/'inputs', root/'jobs'
                inputs.mkdir(); jobs.mkdir()
                source = inputs/'album.cbz'
                source.write_bytes(b'isolated fixture')
                job = jobs/uuid.uuid4().hex
                job.mkdir(mode=0o700)
                (job/'manifest.json').write_text(json.dumps({'operation': 'validate',
                    'input': source.name, 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                    'payload': {}}))
                launcher = JobLauncher(inputs, jobs, 'sha256:'+'0'*64)
                removed = []
                def engine(argv, **kwargs):
                    if argv[1] == 'create':
                        return subprocess.CompletedProcess(argv, 0, ('2'*64).encode(), b'')
                    if argv[1] == 'inspect':
                        return subprocess.CompletedProcess(argv, 0, b'{"Running":true}' if mode == 'running' and (job/'receipt.yaml').exists() else b'{"Running":false}', b'')
                    if argv[1] == 'rm':
                        receipt = yaml.safe_load((job/'receipt.yaml').read_text())
                        self.assertTrue(receipt['stopped'])
                        self.assertEqual(receipt['container'], '2'*64)
                        self.assertEqual((job/'worker.stdout.log').read_bytes(), b'worker output')
                        self.assertEqual((job/'worker.stderr.log').read_bytes(), b'worker diagnostic')
                        self.assertEqual(argv, ['docker', 'rm', '2'*64])
                        removed.append(argv)
                        return subprocess.CompletedProcess(argv, 1 if mode == 'remove_failed' else 0, b'', b'')
                    self.fail(str(argv))
                def run(argv, **kwargs):
                    if argv[1] == 'logs':
                        return subprocess.CompletedProcess(argv, 1 if mode == 'logs_failed' else 0,
                            b'worker output', b'worker diagnostic')
                    return subprocess.CompletedProcess(argv, 0, b'{"status":"validated"}', b'')
                with patch('library.reader_jobs.subprocess.run', side_effect=engine), patch(
                        'library.reader_jobs.bounded_run', side_effect=run):
                    if mode in ('logs_failed', 'running'):
                        with self.assertRaises(JobError):
                            launcher.execute({'operation': 'validate', 'job': job.name})
                        self.assertFalse(removed)
                        self.assertTrue((job/'receipt.yaml').exists())
                    else:
                        result = launcher.execute({'operation': 'validate', 'job': job.name})
                        self.assertEqual(len(removed), 1)
                        self.assertEqual(result, launcher.execute({'operation': 'validate', 'job': job.name}))
                        self.assertEqual(len(removed), 1)


@unittest.skipUnless(os.environ.get('BOOKSYS_COMPOSE_READER_IMAGE'), 'Explicit isolated Docker qualification only')
class ReaderJobTests(unittest.TestCase):
    def fixture(self):
        from postgres_fixture import create_store
        from library.catalog import empty
        from library.reader_service import GrantedLauncher
        from library.operational_state import ReaderJobs
        from library.decoder_ownership import DecoderOwnership
        from library.stage_tasks import StageTasks, Obligation
        from library.stage_resources import Resources, Demand
        from datetime import datetime, timezone
        # Retain every synthetic input, phase and log outside temporary cleanup.
        root = Path(os.environ['BOOKSYS_COMPOSE_READER_EVIDENCE'])/uuid.uuid4().hex
        inputs, jobs = root/'inputs', root/'jobs'
        private_root=Path(os.environ['BOOKSYS_COMPOSE_READER_PRIVATE_ROOT'])
        state=private_root/uuid.uuid4().hex
        if state.is_relative_to(Path(os.environ['BOOKSYS_COMPOSE_READER_EVIDENCE'])):
            self.fail('Private PostgreSQL state must be outside shareable evidence')
        (inputs/'downloads').mkdir(parents=True)
        jobs.mkdir()
        store = create_store(self, state, empty())
        self.enterContext(patch.dict(os.environ, {'BOOKSYS_LAUNCHER_DATABASE_DIR': str(state)}))
        tasks = StageTasks(store)
        stamp = datetime.now(timezone.utc)
        task = tasks.discover(Obligation('ocr', 'fixture', '1', 'v1'), now=stamp)
        key = task.key+':1'
        resources = Resources.create(state/'stage-resources.yaml')
        self.assertEqual(resources.admit(key, age=stamp, demand=Demand(memory_mib=1792,
            cpu_millis=1250, disk=1, output_bytes=4*1024**3), free_bytes=100*1024**3, external_bytes=0), '')
        owner = DecoderOwnership.create(state/'stage-children'/(key+'.yaml'))
        tasks.start(task.key, task.generation, now=stamp, resources=True)
        launcher = GrantedLauncher(inputs, jobs, os.environ['BOOKSYS_COMPOSE_READER_IMAGE'], state,
            job_uid=os.getuid(), cgroup_parent=os.environ['BOOKSYS_COMPOSE_READER_CGROUP'])
        retained = ReaderJobs(store._database)
        self.launcher,self.retained,self.owner,self.key=launcher,retained,owner,key
        def invoke(operation, path, payload):
            reference = uuid.uuid4().hex
            manifest = {'operation': operation, 'input': str(path.relative_to(inputs)),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'payload': payload}
            retained.request(reference, key, manifest)
            owner.register('booksys-reader-'+reference)
            result = launcher.execute({'operation': operation, 'job': reference})
            self.assertTrue(result['stopped'])
            self.assertEqual(result['status'], 'completed', result)
            job = jobs/reference
            # The legacy removal path saves logs. Qualification retains stopped
            # containers and saves their exact bounded logs explicitly.
            from library.process import bounded_run
            logs = bounded_run(['docker', 'logs', '--timestamps', result['container']],
                timeout=10, stdout_limit=4*1024**2, stderr_limit=65536)
            self.assertEqual(logs.returncode, 0, logs.stderr)
            for name, data in (('worker.stdout.log', logs.stdout),
                               ('worker.stderr.log', logs.stderr)):
                with (job/name).open('xb') as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                self.assertEqual((job/name).read_bytes(), data)
            inspected = subprocess.run(['docker', 'inspect', '--format', '{{json .}}', result['container']], capture_output=True, check=True)
            worker = json.loads(inspected.stdout)
            self.assertEqual(worker['Id'], result['container'])
            self.assertFalse(worker['State']['Running'])
            self.assertEqual(worker['Config']['Labels']['booksys.reader.job'], reference)
            self.assertEqual(result, launcher.execute({'operation': operation, 'job': reference}))
            self.assertEqual(launcher.reconcile(reference), {'stopped': True, 'status': 'reconciled'})
            return job, result['outcome']
        return inputs, invoke

    def test_validation_uses_the_retained_source_and_proves_worker_stop(self):
        from test_library_readers import image_bytes
        inputs, invoke = self.fixture()
        source = inputs/'downloads/album.cbz'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('001.png', image_bytes())
        _, result = invoke('validate', source, {})
        self.assertEqual(result['status'], 'validated')
        self.assertEqual(result['input_sha256'], hashlib.sha256(source.read_bytes()).hexdigest())

    def test_all_five_routes_use_fixed_workers_and_their_own_output(self):
        from test_library_readers import image_bytes
        import base64
        inputs, invoke = self.fixture()
        source = inputs/'downloads/album.cbz'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('001.png', image_bytes())
        _, validation = invoke('validate', source, {})
        _, proof = invoke('proof', source, {'page': 1})
        self.assertTrue(base64.b64decode(proof['png']).startswith(b'\x89PNG'))
        _, ocr = invoke('ocr', source, {'pages': validation['page_manifest']})
        self.assertEqual(ocr['status'], 'ok', ocr)
        job, transformed = invoke('transform', source, {
            'comicinfo': base64.b64encode(b'<ComicInfo><Title>Synthetic</Title></ComicInfo>').decode()})
        self.assertEqual(transformed['status'], 'validated', transformed)
        self.assertTrue((job/'work/book.cbz').is_file())
        bundle = inputs/'downloads/bundle.cbz'
        with zipfile.ZipFile(bundle, 'w') as archive:
            archive.writestr('album.cbz', source.read_bytes())
        job, extracted = invoke('bundle', bundle, {})
        self.assertEqual(extracted['status'], 'extracted', extracted)
        self.assertEqual(len(list((job/'work').iterdir())), 1)

    def test_owned_engine_is_stopped_and_partial_evidence_preserved_on_timeout(self):
        import time
        inputs,_=self.fixture()
        source=inputs/'downloads/timeout-bundle.cbz'
        # A bounded compressed synthetic member gives the real fixed worker time
        # to produce partial bytes. No external source or decoder command changes.
        with zipfile.ZipFile(source,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as archive:
            with archive.open('partial.cbz','w') as member:
                member.write(b'PK\x03\x04')
                block=b'x'*(1024**2)
                for _ in range(512):member.write(block)
        source_sha=hashlib.sha256(source.read_bytes()).hexdigest()
        reference=uuid.uuid4().hex
        self.retained.request(reference,self.key,{'operation':'bundle','input':str(source.relative_to(inputs)),
            'sha256':source_sha,'payload':{}})
        self.owner.register('booksys-reader-'+reference)
        job=self.launcher.jobs/reference
        observed={}
        def expire_after_real_progress(argv,**kwargs):
            self.assertEqual(argv[:4],['docker','start','--attach','--interactive'])
            container=argv[-1]
            subprocess.run(['docker','start',container],capture_output=True,check=True,timeout=20)
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                inspected=subprocess.run(['docker','inspect','--format','{{json .}}',container],
                    capture_output=True,check=True,timeout=2)
                worker=json.loads(inspected.stdout)
                partials=[p for p in (job/'work').iterdir() if p.is_file() and p.stat().st_size>0]
                if worker['State']['Running'] and partials:
                    self.assertEqual(worker['Id'],container)
                    self.assertEqual(worker['Config']['Labels']['booksys.reader.job'],reference)
                    observed.update(container=container,running=worker,partials=[str(p.relative_to(job)) for p in partials])
                    raise TimeoutError('Qualification wall limit after actual owned worker progress')
                if not worker['State']['Running']:
                    self.fail('Worker completed before controlled timeout; retain its proof')
                time.sleep(.01)
            self.fail('No actual partial output before bounded qualification deadline')
        with patch('library.reader_jobs.bounded_run',side_effect=expire_after_real_progress):
            with self.assertRaisesRegex(TimeoutError,'Qualification wall limit'):
                self.launcher.execute({'operation':'bundle','job':reference})
        receipt=self.launcher.retained_read(job,'receipt')
        self.assertIsNotNone(receipt)
        self.assertTrue(receipt['stopped'])
        self.assertEqual(receipt['status'],'blocked')
        self.assertEqual(receipt['container'],observed['container'])
        self.assertFalse(receipt['worker_state']['Running'])
        self.assertEqual(receipt['worker_state']['ExitCode'],137)
        stopped=json.loads(subprocess.run(['docker','inspect','--format','{{json .}}',receipt['container']],
            capture_output=True,check=True,timeout=2).stdout)
        self.assertEqual(stopped['Id'],observed['container'])
        self.assertFalse(stopped['State']['Running'])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),source_sha)
        self.assertEqual(hashlib.sha256((job/'input').read_bytes()).hexdigest(),source_sha)
        partial_proof={}
        for relative in observed['partials']:
            path=job/relative
            self.assertGreater(path.stat().st_size,0)
            partial_proof[relative]={'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        from library.process import bounded_run
        logs=bounded_run(['docker','logs','--timestamps',receipt['container']],timeout=10,
            stdout_limit=4*1024**2,stderr_limit=65536)
        self.assertEqual(logs.returncode,0,logs.stderr)
        for name,data in (('worker.stdout.log',logs.stdout),('worker.stderr.log',logs.stderr)):
            with (job/name).open('xb') as stream:
                os.fchmod(stream.fileno(),0o600);stream.write(data);stream.flush();os.fsync(stream.fileno())
        self.assertEqual(self.launcher.reconcile(reference),{'stopped':True,'status':'reconciled'})
        # A retained blocked receipt is a readback. It cannot start a second worker.
        with patch('library.reader_jobs.bounded_run',side_effect=AssertionError('No timeout replay')), patch(
                'library.reader_jobs.subprocess.run',side_effect=AssertionError('No second worker creation')):
            self.assertEqual(self.launcher.execute({'operation':'bundle','job':reference}),receipt)
        from library.authority import code_digest
        (job/'timeout-proof.json').write_text(json.dumps({'result':'PASS','job':reference,
            'application_sha256':code_digest(),'candidate':os.environ.get('BOOKSYS_CANDIDATE'),
            'manifest_sha256':hashlib.sha256((job/'manifest.json').read_bytes()).hexdigest(),
            'observed_running':observed['running'],'observed_stopped':stopped,
            'container':receipt['container'],'source_sha256':source_sha,'receipt':receipt,
            'partial_files':partial_proof,'injection':'bounded_run timeout after real Running and native partial bytes'},indent=2)+'\n')
