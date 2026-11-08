"""Run only inside the pinned, networkless pyLoad image with no live mounts."""
from __future__ import annotations

import hashlib
import json
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
        print(json.dumps({'target_absent': True, 'source_sha256': before,
                          'source_unchanged': True, 'unrelated_ids_preserved': True,
                          'network': 'none', 'live_mounts': False}))
    finally:
        core.terminate()
