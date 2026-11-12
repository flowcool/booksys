"""Existing pyLoad transport and journaled submission contract."""
from __future__ import annotations
from datetime import datetime, timezone
from contextvars import ContextVar
from contextlib import contextmanager
import time
import uuid
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from booksys_core.errors import BrowserError, CollectionError
from library.operational_state import CollectorState
from booksys_core.network_admission import RollingBudget, configured_budget, before_native_write, NativeMutation, current_native, native_receipt
from fiches import CoverError, ImageFetcher, write_fiche

REFERENCES: ContextVar[list[dict[str, str]]] = ContextVar('downloader_references', default=[])


@contextmanager
def references(values: list[dict[str, str]]) -> Iterator[None]:
    token = REFERENCES.set([*REFERENCES.get(), *values])
    try:
        yield
    finally:
        REFERENCES.reset(token)


class Pyload:
    admission: RollingBudget | None = None
    downloads: Path | None = None
    fetch_image: ImageFetcher | None = None

    def __init__(self, endpoint: str, key: str, *, admission: RollingBudget | None = None):
        parts = urlsplit(endpoint)
        if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1", "pyload"}
                or parts.username or parts.password):
            raise CollectionError("pyLoad must use a local endpoint")
        self.endpoint, self.key = endpoint.rstrip("/"), key
        self.admission = admission or configured_budget('native')

    def call(self, method: str, *, write: bool = False, **parameters: Any) -> Any:
        from booksys_core.diagnostics import emit
        request_id = uuid.uuid4().hex
        refs = list(REFERENCES.get())
        if type(parameters.get('package_id')) is int:
            refs.append({'kind': 'package', 'id': 'booksys-pyload:' + str(parameters['package_id'])})
        started = time.monotonic()
        emit(service='collector', stage='downloads', code='started', request_id=request_id, refs=refs)
        try:
            if self.admission is not None:
                self.admission.debit()
            guard = before_native_write.get()
            if write and guard is not None:
                guard()
            mutation = current_native.get()
            if write and self.admission is not None:
                if mutation is None or mutation.directory != self.admission.path.parent:
                    raise CollectionError('Native mutation requires journal ownership')
                mutation.prepare()
            result = self._call(method, write=write, **parameters)
        except Exception as exc:
            if isinstance(exc, HTTPError) and self.admission is not None and (exc.code == 429 or exc.headers.get('Retry-After') is not None):
                self.admission.remote_deadline(exc.headers.get('Retry-After'))
            emit(service='collector', stage='downloads', code='failed', request_id=request_id, refs=refs,
                 reason='unknown_effect' if write else 'provider_unavailable',
                 duration_ms=int((time.monotonic() - started) * 1000))
            raise
        emit(service='collector', stage='downloads', code='completed', request_id=request_id, refs=refs,
             duration_ms=int((time.monotonic() - started) * 1000))
        return result

    @contextmanager
    def mutation(self, owner: str, journal: Path) -> Iterator[NativeMutation | None]:
        if self.admission is None:
            yield None
        else:
            with NativeMutation(self.admission.path.parent, owner, journal).hold() as mutation:
                yield mutation

    def _call(self, method: str, *, write: bool, **parameters: Any) -> Any:
        url = f"{self.endpoint}/api/{method}"
        body = json.dumps(parameters).encode() if write else None
        if not write and parameters:
            url += "?" + urlencode(parameters)
        headers = {"X-API-Key": self.key}
        if write:
            headers["Content-Type"] = "application/json"
        request = Request(url, body, headers)
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        if isinstance(result, dict) and "error" in result:
            raise CollectionError(f"pyLoad rejected {method}")
        return result

    def host_availability(self) -> dict[str, Any]:
        from mirrors import public_hosts
        return public_hosts()

    def submit(self, source: str, record: dict[str, Any], journal: dict[str, Any], path: Path) -> int:
        """Create in collector, verify all links, then move to the download queue.

        Record intent before POST. Reconcile uncertain writes by exact package name.
        Never resend an unconfirmed create operation.
        """
        from booksys_core.values import digest
        entry = journal.get('submissions', {}).get(source, {})
        operation = entry.get('operation_id') or uuid.uuid4().hex
        token = REFERENCES.set([{'kind': 'source', 'id': digest(source)},
                                {'kind': 'operation', 'id': operation}])
        try:
            with self.mutation('submission:' + source, path) as mutation:
                result = self._submit(source, record, journal, path, operation)
                if mutation is not None:
                    from booksys_core.values import digest
                    mutation.resolve(digest(journal['submissions'][source]))
                return result
        finally:
            REFERENCES.reset(token)

    def _submit(self, source: str, record: dict[str, Any], journal: dict[str, Any],
                path: Path, operation: str) -> int:
        submissions = journal.setdefault("submissions", {})
        entry = submissions.get(source)
        if entry and entry["status"] == "queued":
            return int(entry["package_id"])
        marker = "Booksys-" + hashlib.sha256(source.encode()).hexdigest()[:16]
        name = f"{marker} {record['title']}"
        from mirrors import host_sets, select_primary
        all_links = sorted({url for urls in host_sets(record).values() for url in urls})
        if entry:
            links = entry['links']
            host = entry.get('primary_host')
        else:
            host, links = select_primary(record, self.host_availability())
        if not links:
            raise CollectionError("Refusing to submit an empty item")
        packages = self.call("get_queue") + self.call("get_collector")
        matches = [package for package in packages if package["name"] == name]
        if len(matches) > 1:
            raise CollectionError(f"Several pyLoad packages match {marker}; intervention required")
        if matches:
            package_id = int(matches[0]["pid"])
        elif entry:
            raise CollectionError(f"Unconfirmed pyLoad write for {marker}; no automatic resend")
        else:
            submissions[source] = {"operation_id": operation, "status": "creating", "name": name, "links": links, "all_links": all_links,
                                   "primary_host": host, "primary_selected_at": datetime.now(timezone.utc).isoformat()}
            CollectorState(path.parent).submission(source,journal['submissions'][source])
            result = self.call("add_package", write=True, name=name, links=links, dest=0)
            if type(result) is not int:
                raise CollectionError("pyLoad returned an invalid package ID")
            package_id = result
        package = self.call("get_package_data", package_id=package_id)
        from collections import Counter
        actual = Counter(file["url"] for file in package["links"])
        if actual != Counter(links):
            raise CollectionError(f"pyLoad link readback differs for {marker}; queue unchanged")
        from booksys_core.values import digest
        native_receipt(digest(package))
        if self.downloads is not None:
            try:
                write_fiche(source, record, package, self.downloads, self.fetch_image)
            except CoverError as exc:
                if isinstance(exc, BrowserError):
                    raise
                from booksys_core.diagnostics import emit
                from booksys_core.values import digest
                emit(service='collector', stage='collection', code='waiting', reason='provider_unavailable',
                     refs=[{'kind': 'source', 'id': digest(source)}])
        submissions[source] = {**submissions.get(source, {}), "operation_id": operation, "status": "moving", "name": name, "links": links,
                               "all_links": all_links, "primary_host": host, "primary_links": links, "package_id": package_id}
        CollectorState(path.parent).submission(source,journal['submissions'][source])
        if package["dest"] != 1:
            self.call("move_package", write=True, destination=1, package_id=package_id)
        confirmed = self.call("get_package_data", package_id=package_id)
        if confirmed["dest"] != 1:
            raise CollectionError(f"pyLoad did not confirm queue destination for {marker}")
        submissions[source]["status"] = "queued"
        CollectorState(path.parent).submission(source,journal['submissions'][source])
        return package_id
