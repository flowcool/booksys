"""Run only inside the pinned, networkless pyLoad image with no live mounts."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from pyload.core import Core


with TemporaryDirectory() as folder:
    root = Path(folder)
    core = Core(str(root / 'config'), str(root / 'cache'), str(root / 'downloads'))
    try:
        core.thread_manager.pause = True
        target = core.api.add_package('Booksys-retirement-proof', ['http://127.0.0.1:9/comic.cbz'], 0)
        other = core.api.add_package('Lunii-preservation-proof', ['http://127.0.0.1:9/other.zip'], 0)
        package = core.api.get_package_data(target)
        original = root / 'downloads' / package.folder / 'comic.cbz'
        original.parent.mkdir(parents=True)
        original.write_bytes(b'original downloaded source must remain unchanged')
        before = hashlib.sha256(original.read_bytes()).hexdigest()
        other_fids = [f.fid for f in core.api.get_package_data(other).links]
        core.api.delete_packages([target])
        assert target not in [p.pid for p in core.api.get_collector()]
        assert [f.fid for f in core.api.get_package_data(other).links] == other_fids
        assert hashlib.sha256(original.read_bytes()).hexdigest() == before
        # The isolated database must keep this result after an explicit save/read.
        core.files.save()
        assert target not in [p.pid for p in core.api.get_collector()]
        newest = core.api.add_package('Booksys-newest-proof', ['http://127.0.0.1:9/new.cbz'], 0)
        newest_fids = [f.fid for f in core.api.get_package_data(newest).links]
        core.api.delete_packages([newest])
        later = core.api.add_package('Booksys-later-proof', ['http://127.0.0.1:9/later.cbz'], 0)
        later_fids = [f.fid for f in core.api.get_package_data(later).links]
        assert later > newest and min(later_fids) > max(newest_fids), 'Native IDs must not be reused'
        print(json.dumps({'target_absent': True, 'source_sha256': before,
                          'source_unchanged': True, 'unrelated_ids_preserved': True,
                          'native_ids_monotonic': True,
                          'network': 'none', 'live_mounts': False}))
    finally:
        core.terminate()
    subprocess.run([sys.executable, '-c', '''
import sys
from pyload.core import Core
core = Core(sys.argv[1], sys.argv[2], sys.argv[3])
try:
    ids = [p.pid for p in core.api.get_collector()]
    assert int(sys.argv[4]) not in ids
    assert int(sys.argv[5]) in ids
    print('{"native_restart_persistence": true}')
finally:
    core.terminate()
''', str(root / 'config'), str(root / 'cache'), str(root / 'downloads'), str(target), str(other)], check=True)
