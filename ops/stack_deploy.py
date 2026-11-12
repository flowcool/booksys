"""Deploy the Booksys lab: build the images, activate the code digest, let Docker Compose converge.

Run from the repository root with a clean committed tree:
    uv run python ops/stack_deploy.py --evidence /data/booksys/postgresql-prfpw-20261111/evidence/deploy-NAME

Compose already orders the roles (depends_on with health conditions), restarts them and replaces
the ones whose image changed. Nothing here starts containers one by one or keeps container identities.
`install-unit` installs the systemd unit that runs the same `up -d --wait` at boot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml

from library.authority import code_digest
from library.codec import decode, encode
from library.pg_import import target_identity
from library.postgres import Database, guards
from library.primitives import atomic_yaml
from ops.postgres_roles import private

OP = Path('/data/booksys/postgresql-prfpw-20261111')
STATE = Path('/data/booksys/library-lab/state')
CONFIG = OP / 'audit1'
PROJECT = 'booksys-pg-prfpw-audit1'
# Compose owns the application roles. PostgreSQL, pyLoad and Byparr are retained containers (their own
# restart policy is unless-stopped); they are started by name and never recreated from this project.
ROLES = ('controller', 'discovery', 'collection', 'intake', 'ocr', 'metadata', 'copies',
         'routing')
RETAINED = ('booksys-pg-prfpw-postgres', 'booksys-pyload', 'booksys-byparr')
TARGET = dict(system_identifier='7706873868704579611', database='booksys',
              database_oid=16384, schema='booksys', server_version=180006)
BASE = ['docker', 'compose', '--project-name', PROJECT, '--env-file', str(CONFIG / 'candidate.env'),
        '--file', str(CONFIG / 'compose.yaml')]


def run(*argv: str, timeout: int = 1200) -> None:
    subprocess.run(argv, check=True, timeout=timeout)


def build(evidence: Path) -> Path:
    run(sys.executable, str(ROOT / 'ops/stack_images.py'), '--evidence', str(evidence / 'images'))
    return evidence / 'images' / 'images.json'


def activate(images_path: Path, evidence: Path) -> None:
    """Bind the stage activation record and the storage authority to this exact code and images."""
    images = json.loads(images_path.read_text())
    digest = code_digest()
    if images['application_sha256'] != digest or any(
            item['runtime']['application_sha256'] != digest for item in images['images'].values()):
        raise SystemExit('The images were not built from this code')
    database = Database(private(OP / 'secrets/migration.dsn'), 'booksys')
    if target_identity(database) != TARGET:
        raise SystemExit('The database is not the selected target')
    manifest = yaml.safe_load((STATE / 'storage-authority.yaml').read_text())
    receipt = evidence / 'qualification.json'
    receipt.write_text(json.dumps({'candidate': images['commit'], 'application': digest, 'images_manifest': str(images_path),
        'images_manifest_sha256': hashlib.sha256(images_path.read_bytes()).hexdigest(),
        'predecessor': manifest['code_sha256']}, indent=1))
    with database.connection(write=True) as db:
        guards(db, {'runtime-activation:stage'})
        row = db.execute('SELECT payload FROM runtime_activation WHERE role=%s FOR UPDATE', ('stage',)).fetchone()
        assert row is not None
        stage = decode(row[0])
        db.execute('UPDATE runtime_activation SET payload=%s WHERE role=%s',
                   (encode({**stage, 'code_digest': digest, 'qualification_receipt': str(receipt)}), 'stage'))
    authority = STATE / 'storage-authority.yaml'
    authority.chmod(0o600)
    atomic_yaml(authority, {**manifest, 'code_sha256': digest})
    authority.chmod(0o400)
    env = dict(line.split('=', 1) for line in (CONFIG / 'candidate.env').read_text().splitlines())
    for name, key in (('app', 'BOOKSYS_APP_IMAGE'),):
        env[key] = images['images'][name]['id']
    (CONFIG / 'candidate.env').write_text(''.join(f'{k}={v}\n' for k, v in sorted(env.items())))
    (CONFIG / 'compose.yaml').write_bytes((ROOT / 'compose.stack.yaml').read_bytes())


def wait_healthy(name: str, seconds: int = 120) -> None:
    for _ in range(seconds // 2):
        status = subprocess.run(['docker', 'inspect', '-f', '{{.State.Health.Status}}', name],
                                capture_output=True, text=True).stdout.strip()
        if status == 'healthy':
            return
        time.sleep(2)
    raise SystemExit(f'{name} is not healthy')


def deploy(evidence: Path, images: Path | None) -> None:
    evidence.mkdir(mode=0o700, exist_ok=False)
    if subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, capture_output=True, text=True).stdout.strip():
        raise SystemExit('Commit the complete candidate before deploying')
    images_path = images or build(evidence)
    # Stop with the new topology: a role added by this candidate is unknown to the previous compose file.
    (CONFIG / 'compose.yaml').write_bytes((ROOT / 'compose.stack.yaml').read_bytes())
    # The application roles stop first: a running worker refuses a new activation record.
    run(*BASE, 'stop', '--timeout', '60', *ROLES)
    run('docker', 'start', *RETAINED)
    wait_healthy('booksys-pg-prfpw-postgres')
    activate(images_path, evidence)
    run(*BASE, 'up', '-d', '--wait', '--remove-orphans', '--no-build', '--pull', 'never', '--wait-timeout', '900', *ROLES, timeout=1000)
    print('Stack converged:', ', '.join((*RETAINED, *ROLES)))


def install_unit() -> None:
    run('sudo', 'install', '-m', '644', str(ROOT / 'ops/booksys-compose.service'), '/etc/systemd/system/booksys-compose.service')
    run('sudo', 'systemctl', 'daemon-reload')
    run('sudo', 'systemctl', 'enable', 'booksys-compose.service')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', nargs='?', choices=('deploy', 'install-unit'), default='deploy')
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--images', type=Path, help='Use images that are already built')
    args = parser.parse_args()
    if args.command == 'install-unit':
        install_unit()
    elif args.evidence is None:
        parser.error('--evidence is required')
    else:
        deploy(args.evidence, args.images)


if __name__ == '__main__':
    main()
