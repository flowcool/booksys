"""Run the real collector CLI against an in-container synthetic Byparr API."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

import yaml

CATEGORY = "https://www7.bookys-ebooks.com/bandes-dessinees/new"
ITEM = "https://www7.bookys-ebooks.com/bandes-dessinees/1-synthetic"
OUTGOING = "https://files.example.invalid/synthetic.cbz"
PAGES = {
    CATEGORY: '<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="' + ITEM + '">Synthetic</a></div>',
    ITEM: '<h1>Synthetic</h1><a href="/bandes-dessinees/1-synthetic/dl/1">fixture</a>',
    ITEM + "/dl/1": '<h1>Download</h1><a class="bys-link bys-host" href="' + OUTGOING + '">Synthetic</a>',
}
requests = []
failure = False


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append(request["url"])
        assert request["url"] in PAGES
        payload = {"status": "error", "message": "synthetic failure"} if failure else {
            "status": "ok", "solution": {"status": 200, "url": request["url"],
                                             "response": PAGES[request["url"]]}}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    with tempfile.TemporaryDirectory() as directory:
        command = [sys.executable, "/collector/booksys.py", "--category", CATEGORY,
                   "--byparr", f"http://127.0.0.1:{server.server_port}/v1", "--delay", "0",
                   "--directory", directory]
        first = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
        assert json.loads(first.stdout) == {"pages": 1, "collected": 1, "skipped": 0}
        state = yaml.safe_load((Path(directory) / "state.yaml").read_text())
        assert state["items"][ITEM]["downloads"][ITEM + "/dl/1"]["urls"] == [OUTGOING]
        assert (Path(directory) / "fixture.txt").read_text().strip() == OUTGOING
        second = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
        assert json.loads(second.stdout) == {"pages": 1, "collected": 0, "skipped": 1}
        failure = True
        stopped = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert stopped.returncode == 1 and "Collection stopped" in stopped.stderr
        assert requests == [CATEGORY, ITEM, ITEM + "/dl/1", CATEGORY, CATEGORY]
    print(json.dumps({"probe": "worker", "synthetic_only": True, "collected": 1,
                      "restart_skipped": 1, "solver_failure_stopped": True}))
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
