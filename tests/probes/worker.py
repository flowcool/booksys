"""Qualify the current application roles without a database or external network."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from booksys_core.parsing import catalogue, source_plan
import psycopg
from psycopg import pq

assert sys.version_info[:2] == (3, 14)
assert psycopg.__version__ == "3.3.8"
assert pq.__impl__ == "c"
assert os.getuid() == 1000
assert Path("/usr/local/bin/docker").is_file()
for forbidden in ("/var/run/docker.sock", "/secrets", "/data/booksys", "/root/.ssh"):
    assert not Path(forbidden).exists(), forbidden
roles = ("library.cli", "library.stage_worker", "ops.library_service")
for role in roles:
    result = subprocess.run([sys.executable, "-m", role, "--help"],
        capture_output=True, text=True, timeout=20, check=True)
    assert "usage:" in result.stdout.lower(), role
category = "https://www.bookys-ebooks.com/bandes-dessinees/new"
source = "https://www.bookys-ebooks.com/bandes-dessinees/1-synthetic"
items, following = catalogue('<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="' + source + '">Synthetic</a></div>', category)
assert items == {source: "Synthetic"} and following is None
plan = source_plan('<h1>Synthetic</h1><a href="/bandes-dessinees/1-synthetic/dl/1">fixture</a>', source, None)
assert plan
namespace = {"__name__": "image_provider_spacing"}
exec(compile(sys.stdin.read(), "test_provider_spacing.py", "exec"), namespace)
suite = unittest.defaultTestLoader.loadTestsFromTestCase(namespace["ProviderSpacingTests"])
assert suite.countTestCases() == 2
result = unittest.TextTestRunner(verbosity=2).run(suite)
assert result.wasSuccessful() and result.testsRun == 2 and not result.skipped
print(json.dumps({"probe": "worker", "roles": roles, "python": list(sys.version_info[:2]),
                  "postgresql_driver": psycopg.__version__, "synthetic_parser": True,
                  "provider_business_tests": result.testsRun,
                  "database_connection": False, "external_network": False}))
