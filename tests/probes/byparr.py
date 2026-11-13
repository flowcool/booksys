"""Probe the actual adapter, then run synthetic sessions in its dependency environment."""

import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import types
import unittest
from urllib.error import URLError
from urllib.request import Request, urlopen

if sys.argv[1] == "readiness":
    assert Path("/proc/1/comm").read_text().strip() == "tini"
    assert Path("/proc/1/cmdline").read_bytes().split(b"\0")[:5] == [
        b"tini", b"-s", b"--", b"/app/.venv/bin/python", b"/app/session_api.py"]
    for attempt in range(150):
        try:
            with urlopen("http://127.0.0.1:8191/health", timeout=1) as response:
                health = json.load(response)
            break
        except (URLError, TimeoutError):
            if attempt == 149:
                raise
            time.sleep(0.2)
    assert health == {"status": "ok", "generation": 0, "requests": 0}, health
    # The real endpoint rejects this input before calling the lazy browser factory.
    request = Request("http://127.0.0.1:8191/v1", method="POST",
                      data=b'{"url":"https://outside.example.invalid"}',
                      headers={"Content-Type": "application/json"})
    try:
        urlopen(request, timeout=2)
    except URLError as exc:
        assert getattr(exc, "code", None) == 422, exc
    else:
        raise AssertionError("External origin was accepted")
    with urlopen("http://127.0.0.1:8191/health", timeout=2) as response:
        assert json.load(response) == health
    processes = [p.read_text().strip() for p in Path("/proc").glob("[0-9]*/comm")]
    assert not any(any(browser in p.lower() for browser in ("firefox", "chrome", "playwright"))
                   for p in processes), processes
    print(json.dumps({"actual_entrypoint": "tini/session_api.py", "health": health,
                      "invalid_origin": 422, "browser_processes": 0, "processes": processes}))
elif sys.argv[1] == "sessions":
    # The harness overrides the image entrypoint with Python. Restore its tini
    # subreaper before starting Firefox, so orphaned children are reaped as in
    # the service instead of remaining zombies under the test interpreter.
    if os.getpid() == 1:
        os.execvp("tini", ["tini", "-s", "--", sys.executable, *sys.orig_argv[1:]])
    assert Path("/proc/1/comm").read_text().strip() == "tini"
    # Firefox159 checks this legacy application directory before -profile.
    Path("/home/byparr/.mozilla/firefox").mkdir(parents=True, exist_ok=True)
    Path("/cache/mozilla/firefox").mkdir(parents=True, exist_ok=True)
    # Import the delivered adapter, not a second source copy. Stream only tests.
    import session_api
    assert importlib.util.find_spec("src") is not None
    # Dotted mock targets import parent packages before resolving the adapter.
    ops = types.ModuleType("ops")
    byparr = types.ModuleType("ops.byparr")
    ops.byparr = byparr
    byparr.session_api = session_api
    sys.modules["ops"] = ops
    sys.modules["ops.byparr"] = byparr
    sys.modules["ops.byparr.session_api"] = session_api
    module = types.ModuleType("synthetic_sessions")
    exec(compile(sys.stdin.read(), "test_byparr_session.py", "exec"), module.__dict__)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    assert suite.countTestCases() > 0, "Missing adapter tests"
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    assert result.wasSuccessful() and not result.skipped, "Adapter cases failed or skipped"
    print(json.dumps({"tests": result.testsRun, "skipped": len(result.skipped),
                      "adapter": session_api.__file__, "python": sys.version}))
else:
    raise SystemExit("Expected readiness or sessions")
