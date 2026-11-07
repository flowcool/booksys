"""Create an API key in an existing OFFLINE pyLoad database. Keep all users unchanged."""

import json
import logging
import os
import secrets
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from pyload.core.threads.database_thread import DatabaseThread
from pyload.core.api import Api

os.umask(0o077)
database = Path("/config/data/pyload.db")
assert database.is_file()
connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
admins = list(connection.execute("SELECT id FROM users WHERE role=0 ORDER BY id"))
connection.close()
assert admins, "Existing administrator required; no user is created or reset"
core = SimpleNamespace(userdir="/config", _=lambda text: text, log=logging.getLogger("authorize"))
core.db = DatabaseThread(core)
core.db.setup()
try:
    token = secrets.token_urlsafe(32)
    identifier = core.db.create_user_apikey(admins[0][0], "Booksys collector", 0, token)
    assert identifier
    full_key = f"pl_{len(str(identifier))}{identifier}{token}"
    assert Api(core).check_apikey(full_key)["success"], "New API key did not pass upstream verification"
    path = Path("/secrets/pyload.json")
    path.write_text(json.dumps({"instance": "booksys-pyload", "api_key": full_key}, indent=2) + "\n")
    path.chmod(0o600)
    print("Created collector API key for existing pyLoad; login and password unchanged")
finally:
    core.db.shutdown()
    core.db.jobs.put("quit")
    core.db.join(timeout=10)
