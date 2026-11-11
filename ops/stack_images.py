"""Build and retain identified stack images from a committed, clean candidate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from library.authority import code_digest


def command(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if command('git', 'status', '--porcelain'):
        raise SystemExit('Commit the complete candidate before building retained images')
    revision = command('git', 'rev-parse', 'HEAD')
    args.evidence.mkdir(parents=True, exist_ok=False)
    images = {}
    for role, dockerfile, target in (('app', 'Dockerfile.stack', 'application'),
            ('reader', 'Dockerfile.stack', 'reader'), ('launcher', 'Dockerfile.stack', 'launcher')):
        tag = 'booksys-'+role+':compose-'+revision[:12]
        argv = ['docker', 'build', '-f', dockerfile, '--build-arg', 'BOOKSYS_REVISION='+revision, '-t', tag]
        if target:
            argv += ['--target', target]
        with (args.evidence/(role+'-build.log')).open('w') as log:
            subprocess.run([*argv, '.'], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
        identity = command('docker', 'image', 'inspect', '--format', '{{.Id}}', tag)
        retained = 'booksys-'+role+':compose-retained-'+identity.removeprefix('sha256:')[:12]
        command('docker', 'image', 'tag', identity, retained)
        images[role] = {'candidate': tag, 'retained': retained, 'id': identity}
    probe = '''import json, psycopg, sys
from library.authority import code_digest
assert sys.version_info[:2] == (3,14)
print(json.dumps({'application_sha256': code_digest(), 'python': sys.version, 'psycopg': psycopg.__version__}))'''
    for role in ('app', 'reader', 'launcher'):
        container = command('docker', 'create', '--name', 'booksys-'+role+'-probe-'+revision[:12], '--network=none', '--read-only',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--entrypoint=/app/.venv/bin/python',
            images[role]['id'], '-c', probe)
        # Save identity before starting. A failed probe remains inspectable.
        (args.evidence/(role+'-probe-id')).write_text(container+'\n')
        command('docker', 'start', container)
        command('docker', 'wait', container)
        with (args.evidence/(role+'-probe.log')).open('w') as log:
            subprocess.run(['docker', 'logs', container], stdout=log, stderr=subprocess.STDOUT, check=True)
        retained_probe = json.loads(command('docker', 'inspect', '--format', '{{json .}}', container))
        (args.evidence/(role+'-probe.json')).write_text(json.dumps(retained_probe, indent=2)+'\n')
        if retained_probe['Id'] != container or retained_probe['State']['Running'] or retained_probe['State']['ExitCode'] != 0:
            raise SystemExit('Probe did not stop successfully; retain identity and logs')
        observed = json.loads((args.evidence/(role+'-probe.log')).read_text())
        if observed['application_sha256'] != code_digest():
            raise SystemExit('Built application digest differs from committed source')
        images[role]['runtime'] = observed
        images[role]['probe'] = retained_probe
    value = {'schema': 1, 'commit': revision, 'tree': command('git', 'rev-parse', 'HEAD^{tree}'), 'application_sha256': code_digest(), 'images': images,
             'retention': 'Named local images retained; no prune or automatic cleanup',
             'limits': 'Clean retrieval succeeded now; future registry and Debian snapshot availability is not guaranteed'}
    (args.evidence/'images.json').write_text(json.dumps(value, indent=2)+'\n')
    print('Built three identified images; stopped probes and exact application digests retained')


if __name__ == '__main__':
    main()
