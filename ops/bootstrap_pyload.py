"""Initialize a NEW, offline pyLoad user directory with random credentials.

Run inside the pinned pyLoad image. Never point this script at existing data.
Use upstream DatabaseThread and Api to create the user and API key.
"""

import json
import logging
import os
import secrets
from pathlib import Path
from types import SimpleNamespace

from pyload.core.api import Api
from pyload.core.threads.database_thread import DatabaseThread

os.umask(0o077)
userdir = Path("/config")
secret_path = Path("/secrets/pyload.json")
if (userdir / "data/pyload.db").exists() or secret_path.exists():
    raise SystemExit("Refusing to overwrite an existing database or credentials")

core = SimpleNamespace(userdir=str(userdir), _=lambda text: text, log=logging.getLogger("bootstrap"))
core.db = DatabaseThread(core)
core.db.setup()
try:
    password = secrets.token_urlsafe(32)
    assert core.db.add_user("booksys", password, role=0, perms=0)
    api = Api(core)
    result = api.generate_apikey("booksys", password, "Booksys local collector")
    assert result["success"]
    secret_path.write_text(json.dumps({"username": "booksys", "password": password,
        "api_key": result["data"]["key"]}, indent=2) + "\n")
    default = Path(__import__("pyload").__file__).parent / "core/config/default.cfg"
    config = default.read_text()
    replacements = {
        'folder storage_folder : "Download folder" = ~/Downloads/pyLoad':
            'folder storage_folder : "Download folder" = /downloads',
        'int min_free_space : "Minimum free space in MiB" = 1024':
            'int min_free_space : "Minimum free space in MiB" = 20480',
        'ip host : "IP address" = localhost': 'ip host : "IP address" = 0.0.0.0',
        'bool debug_mode : "Debug mode" = True': 'bool debug_mode : "Debug mode" = False',
        'bool skip_existing : "Skip already existing files" = False':
            'bool skip_existing : "Skip already existing files" = True',
    }
    for before, after in replacements.items():
        assert before in config, before
        config = config.replace(before, after)
    settings = userdir / "settings"
    settings.mkdir(exist_ok=True)
    (settings / "pyload.cfg").write_text(config)
    print("Initialized new Booksys pyLoad user, API key and download path; credentials not printed")
finally:
    core.db.shutdown()
    core.db.jobs.put("quit")
    core.db.join(timeout=10)
