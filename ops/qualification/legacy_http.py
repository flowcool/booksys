"""Measure exact legacy HTTP behavior on an isolated identical fixture.

Stop after the first failing query: baseline failure motivates the change and
is never counted as a successful candidate proof. No external API is available.
"""
from __future__ import annotations
import argparse
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import resource
import sys
import threading
import time
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--legacy', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = (args.dataset / 'baseline').resolve()
    if root.is_relative_to('/data/booksys'):
        raise ValueError('No live baseline is allowed')
    # Bound this legacy whole-YAML allocation on the shared lab host.
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024**2, 1536 * 1024**2))
    sys.path.insert(0, str(args.legacy.resolve()))
    from library.web import build_projection, handler
    from library.catalog import atomic_yaml
    import library.web
    if Path(library.web.__file__).resolve().parents[1] != args.legacy.resolve():
        raise ValueError('Legacy module selection differs')
    started = time.perf_counter()
    result = {'schema': 1, 'legacy_commit': '1b0f4d9', 'snapshot': json.loads((args.dataset / 'dataset.json').read_text()),
              'input': str(root / 'state/library.yaml'), 'external_calls': 0}
    try:
        projection = build_projection(root)
        atomic_yaml(root / 'evidence/dashboard.yaml', projection)
        preparation = (time.perf_counter() - started) * 1000
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(root))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            start = time.perf_counter()
            with urllib.request.urlopen('http://127.0.0.1:' + str(server.server_port) + '/api/status', timeout=30) as response:
                body = response.read()
            elapsed = (time.perf_counter() - start) * 1000
            result.update(preparation_ms=preparation, first_query_ms=elapsed, bytes=len(body),
                          under_2000ms=elapsed < 2000, outcome='complete',
                          peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)
    except (MemoryError, OSError, ValueError) as exc:
        result.update(outcome=type(exc).__name__, under_2000ms=False,
                      elapsed_ms=(time.perf_counter() - started) * 1000)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: value for key, value in result.items() if key != 'snapshot'}))


if __name__ == '__main__':
    main()
