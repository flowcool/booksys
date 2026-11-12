"""Fixed typed parent operations over bounded length-prefixed JSON frames."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import base64
import queue
import socket
import threading
import time
from typing import Any, Callable
import uuid
import zlib

FRAME_LIMIT = 4 * 1024**2
REQUEST_LIMIT = 65536
LANES = {'read': (2, 8, 1.5), 'diagnostics': (1, 3, 1.5)}
ACTIONS = {'status': 'read', 'library': 'read', 'fiche': 'read', 'cover': 'read',
           'relationships': 'read', 'diagnostics': 'diagnostics'}


class IPCError(ValueError):
    pass


@dataclass(frozen=True)
class EncodedJSON:
    """A complete bounded controller response relayed by the restricted child."""
    body: bytes
    compressed: str | None = None


def decode(raw: bytes) -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in values:
            if key in result:
                raise IPCError('Duplicate IPC field')
            result[key] = value
        return result
    def constant(value: str) -> Any:
        raise IPCError('Invalid IPC constant')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


class Channel:
    def __init__(self, connection: socket.socket):
        self.socket = connection
        self.write_lock = threading.Lock()

    def _read(self, count: int) -> bytes:
        result = bytearray()
        while len(result) < count:
            chunk = self.socket.recv(count - len(result))
            if not chunk:
                raise IPCError('IPC disconnected; dispatched completion may be unknown')
            result.extend(chunk)
        return bytes(result)

    def receive(self) -> dict[str, Any]:
        length = int.from_bytes(self._read(4), 'big')
        if not 0 < length <= FRAME_LIMIT:
            raise IPCError('IPC frame exceeds bound')
        try:
            result = decode(self._read(length))
        except (ValueError, UnicodeError) as exc:
            raise IPCError('Invalid IPC JSON') from exc
        if not isinstance(result, dict):
            raise IPCError('IPC object required')
        if '_binary_length' in result:
            count = result.pop('_binary_length')
            if (type(count) is not int or not 0 < count <= 3 * 1024**2
                    or set(result) != {'id', 'result', 'error'} or result['result'] != {'png': None}
                    or result['error'] is not None):
                raise IPCError('Invalid proof binary segment')
            result['result']['png'] = base64.b64encode(self._read(count)).decode()
        return result

    def send(self, value: dict[str, Any]) -> None:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
        if len(raw) > FRAME_LIMIT:
            raise IPCError('IPC response exceeds bound; incomplete ownership view')
        with self.write_lock:
            self.socket.sendall(len(raw).to_bytes(4, 'big') + raw)

    def result(self, ident: str, value: Any, *, chunks: bool = False) -> None:
        if isinstance(value, dict) and set(value) == {'png'}:
            raw = base64.b64decode(value['png'], validate=True)
            if not 0 < len(raw) <= 3 * 1024**2:
                raise IPCError('Proof binary bound exceeded')
            header = json.dumps({'id': ident, 'result': {'png': None}, 'error': None,
                                 '_binary_length': len(raw)}, separators=(',', ':')).encode()
            with self.write_lock:
                self.socket.sendall(len(header).to_bytes(4, 'big') + header + raw)
            return
        if chunks:
            raw = value.body if isinstance(value, EncodedJSON) else json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
            if len(raw) > 16 * 1024**2:
                raise IPCError('Compatibility response exceeds16MiB')
            compressed = value.compressed if isinstance(value, EncodedJSON) and value.compressed is not None else base64.b64encode(zlib.compress(raw, level=1)).decode()
            if len(compressed) < FRAME_LIMIT - 1024:
                self.send({'id': ident, 'result': {'_compatibility_zlib': compressed}, 'error': None})
                return
            # The child relays this trusted controller serialization unchanged.
            # Keep JSON-string fragments in bounded JSON frames. Reconstructing
            # thousands of row objects in the child only to encode them again
            # exhausted the1500ms shared read deadline with four viewers.
            data = (value.body.decode() if isinstance(value, EncodedJSON) else
                    json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
            if len(data.encode()) > 16 * 1024**2:
                raise IPCError('Compatibility response exceeds16MiB')
            size = 256 * 1024  # <=1MiB UTF-8, <=2MiB after JSON escaping.
            for index, start in enumerate(range(0, len(data), size)):
                self.send({'id': ident, 'chunk': data[start:start + size], 'index': index,
                           'final': start + size >= len(data)})
            return
        self.send({'id': ident, 'result': value, 'error': None})

    def close(self) -> None:
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


class Controller:
    def __init__(self, channel: Channel, invoke: Callable[[str, dict[str, Any]], Any], *,
                 validate: Callable[[str, dict[str, Any]], None] | None = None):
        self.channel, self.invoke = channel, invoke
        self.validate = validate
        self.closed = threading.Event()
        self.group_lock = threading.Lock()
        self.groups: dict[str, list[str]] = {}
        self.group_owners: dict[str, str] = {}
        self.pools = {name: ThreadPoolExecutor(max_workers=workers, thread_name_prefix='http-' + name)
                      for name, (workers, _, _) in LANES.items()}
        self.slots = {name: threading.BoundedSemaphore(workers + queued)
                      for name, (workers, queued, _) in LANES.items()}

    def recipients(self, ident: str) -> list[str]:
        with self.group_lock:
            key = self.group_owners.pop(ident, None)
            return self.groups.pop(key) if key is not None else [ident]

    def respond(self, ident: str, *, result: Any = None, error: str | None = None) -> None:
        try:
            for recipient in self.recipients(ident):
                self.channel.send({'id': recipient, 'result': result, 'error': error})
        except (OSError, IPCError):
            self.closed.set()

    def execute(self, ident: str, action: str, value: dict[str, Any], admitted: float) -> None:
        lane = ACTIONS[action]
        try:
            if time.monotonic() - admitted >= LANES[lane][2]:
                self.respond(ident, error='Request deadline expired before dispatch')
                return
            result = self.invoke(action, value)
            if time.monotonic() - admitted > LANES[lane][2]:
                self.respond(ident, error='Request deadline expired; dispatched completion unknown')
            else:
                targets = self.recipients(ident)
                try:
                    if action == 'status':
                        raw = result.body if isinstance(result, EncodedJSON) else json.dumps(
                            result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
                        if len(raw) > 16 * 1024**2:
                            raise ValueError('Compatibility body exceeds bound')
                        result = EncodedJSON(raw, base64.b64encode(zlib.compress(raw, level=1)).decode())
                    for recipient in targets:
                        self.channel.result(recipient, result, chunks=action == 'status')
                except (TypeError, ValueError):
                    for recipient in targets:
                        self.respond(recipient, error='Response exceeds bound; ownership view incomplete')
        except Exception:
            self.respond(ident, error='Action unavailable; inspect filtered diagnostics')
        finally:
            self.slots[lane].release()

    def serve(self) -> None:
        try:
            while not self.closed.is_set():
                request = self.channel.receive()
                if (set(request) != {'id', 'action', 'value'} or not isinstance(request['id'], str)
                        or len(request['id']) != 32 or any(c not in '0123456789abcdef' for c in request['id'])
                        or not isinstance(request['action'], str) or not isinstance(request['value'], dict)
                        or len(json.dumps(request).encode()) > REQUEST_LIMIT):
                    raise IPCError('Malformed IPC request')
                ident, action, value = request['id'], request['action'], request['value']
                lane = ACTIONS.get(action)
                if lane is None:
                    self.respond(ident, error='Unsupported action')
                    continue
                if self.validate is not None:
                    try:
                        self.validate(action, value)
                    except Exception:
                        self.respond(ident, error='Forbidden request envelope')
                        continue
                share = lane == 'diagnostics' or action in {'status', 'library', 'relationships'}
                group_key = json.dumps([action, value], sort_keys=True, separators=(',', ':')) if share else None
                if group_key is not None:
                    with self.group_lock:
                        shared = self.groups.get(group_key)
                        if shared is not None:
                            if len(shared) < 16:
                                shared.append(ident)
                            else:
                                self.channel.send({'id': ident, 'result': None,
                                                   'error': 'busy: shared read capacity reached'})
                            continue
                if not self.slots[lane].acquire(blocking=False):
                    self.respond(ident, error='busy: fixed lane capacity reached')
                    continue
                if group_key is not None:
                    with self.group_lock:
                        self.groups[group_key] = [ident]
                        self.group_owners[ident] = group_key
                self.pools[lane].submit(self.execute, ident, action, value, time.monotonic())
        except (IPCError, OSError):
            self.closed.set()

    def close(self) -> None:
        self.closed.set()
        self.channel.close()
        for pool in self.pools.values():
            pool.shutdown(wait=False, cancel_futures=True)


class Remote:
    def __init__(self, channel: Channel, *, raw_compatibility: bool = False):
        self.channel = channel
        self.raw_compatibility = raw_compatibility
        self.pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self.lock = threading.Lock()
        self.chunk_indexes: dict[str, int] = {}
        self.object_chunks: dict[str, list[str]] = {}
        self.chunk_sizes: dict[str, int] = {}
        self.closed = threading.Event()
        self.receiver = threading.Thread(target=self._receive, daemon=True, name='ipc-responses')
        self.receiver.start()

    def _receive(self) -> None:
        try:
            while not self.closed.is_set():
                value = self.channel.receive()
                if set(value) == {'id', 'chunk', 'index', 'final'}:
                    ident = value['id']
                    if not isinstance(ident, str) or type(value['index']) is not int or type(value['final']) is not bool:
                        raise IPCError('Invalid continuation frame')
                    with self.lock:
                        if ident not in self.pending:
                            continue
                        expected = self.chunk_indexes.get(ident, 0)
                    if value['index'] != expected:
                        raise IPCError('Out-of-order response chunk')
                    chunk = value['chunk']
                    if not isinstance(chunk, str) or len(chunk) > 256 * 1024:
                        raise IPCError('Invalid compatibility fragment')
                    self.chunk_sizes[ident] = self.chunk_sizes.get(ident, 0) + len(chunk.encode())
                    if self.chunk_sizes[ident] > 16 * 1024**2:
                        raise IPCError('Compatibility response bound exceeded')
                    assembled = self.object_chunks.setdefault(ident, [])
                    assembled.append(chunk)
                    self.chunk_indexes[ident] = expected + 1
                    if not value['final']:
                        continue
                    body = ''.join(assembled).encode()
                    result = EncodedJSON(body) if self.raw_compatibility else decode(body)
                    value = {'id': ident, 'result': result, 'error': None}
                    self.object_chunks.pop(ident, None)
                    self.chunk_sizes.pop(ident, None)
                    self.chunk_indexes.pop(ident, None)
                if set(value) != {'id', 'result', 'error'} or not isinstance(value['id'], str):
                    raise IPCError('Malformed IPC response')
                if isinstance(value['result'], dict) and set(value['result']) == {'_compatibility_zlib'}:
                    try:
                        packed = base64.b64decode(value['result']['_compatibility_zlib'], validate=True)
                        unpacker = zlib.decompressobj()
                        body = unpacker.decompress(packed, 16 * 1024**2 + 1)
                        if (len(body) > 16 * 1024**2 or not unpacker.eof
                                or unpacker.unconsumed_tail or unpacker.unused_data):
                            raise IPCError('Compatibility expansion exceeds bound')
                        value['result'] = EncodedJSON(body) if self.raw_compatibility else decode(body)
                    except (ValueError, TypeError, zlib.error) as exc:
                        raise IPCError('Invalid compressed compatibility response') from exc
                with self.lock:
                    waiting = self.pending.get(value['id'])
                if waiting is not None:
                    waiting.put_nowait(value)
        except (IPCError, OSError, queue.Full):
            self.closed.set()
            with self.lock:
                for ident, waiting in self.pending.items():
                    try:
                        waiting.put_nowait({'id': ident, 'result': None,
                                            'error': 'IPC disconnected; dispatched completion unknown'})
                    except queue.Full:
                        pass

    def call(self, action: str, value: dict[str, Any]) -> Any:
        if action not in ACTIONS or self.closed.is_set():
            raise IPCError('Unsupported action or unavailable controller')
        ident = uuid.uuid4().hex
        waiting: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self.lock:
            if len(self.pending) >= 16:
                raise IPCError('busy: HTTP connection capacity reached')
            self.pending[ident] = waiting
        try:
            self.channel.send({'id': ident, 'action': action, 'value': value})
            try:
                response = waiting.get(timeout=LANES[ACTIONS[action]][2])
            except queue.Empty as exc:
                raise IPCError('IPC deadline; dispatched completion unknown; do not resend confirmation') from exc
            if response['error'] is not None:
                raise IPCError(response['error'])
            return response['result']
        finally:
            with self.lock:
                self.pending.pop(ident, None)
                self.object_chunks.pop(ident, None)
                self.chunk_sizes.pop(ident, None)
                self.chunk_indexes.pop(ident, None)

    def close(self) -> None:
        self.closed.set()
        self.channel.close()
        self.receiver.join(1)
