"""Exact reverse-proxy origin pairs cross the real HTTP and Unix IPC boundary."""
import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from library.api import PublicAPI
from library.compose_transport import ReconnectingRemote, UnixController
from library.compose_web import PublicServer
from library.http import public_origins
from library.primitives import LibraryError,empty
from postgres_fixture import create_store


class OriginTests(unittest.TestCase):
    def test_configuration_rejects_noncanonical_origins(self):
        for origin in ('https://user:password@books.example', 'https://books.example/',
                       'https://books.example?q=1', 'https://books.example#fragment',
                       'https://books.example:0', 'https://books.example:99999',
                       'https://books.example\n', 'null', 'file://books.example',
                       'https://BOOKS.example', 'https://books.example:443/path'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                public_origins('127.0.0.1:8768', origin)
        self.assertEqual(public_origins('127.0.0.1:8768'), ('http://127.0.0.1:8768',))
        self.assertEqual(public_origins('127.0.0.1:8768', 'https://books.example'),
                         ('http://127.0.0.1:8768', 'https://books.example'))

    def test_controller_rejects_nonstring_or_cross_pair_envelopes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_store(self,root/"state",empty())
            api = PublicAPI(root, host='127.0.0.1:8768', reception=root/'unused',
                            public_origin='https://books.example')
            base = {'payload': {}, 'host': 'books.example',
                    'origin': 'https://books.example', 'token': api.token}
            default = PublicAPI(root, host='127.0.0.1:8768', reception=root/'unused')
            default.validate('confirm', {**base, 'host': default.host, 'origin': 'http://'+default.host, 'token': default.token})
            with self.assertRaises(LibraryError):
                default.validate('confirm', {**base, 'token': default.token})
            api.validate('confirm', base)
            api.validate('confirm', {**base, 'host': api.host, 'origin': 'http://'+api.host})
            for changes in ({'host': api.host}, {'origin': 'http://books.example'},
                            {'origin': 'https://evil.example'}, {'host': []},
                            {'origin': []}, {'token': []}, {'token': 'wrong'}, {'token': 'é'}):
                with self.subTest(changes=changes), self.assertRaises(LibraryError):
                    api.validate('confirm', {**base, **changes})

    def test_real_http_and_controller_preserve_https_ip_and_csrf(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_store(self,root/"state",empty())
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0)); listener.listen(16)
            local = '127.0.0.1:'+str(listener.getsockname()[1])
            api = PublicAPI(root, host=local, reception=root/'unused',
                            public_origin='https://books.example')
            remote = ReconnectingRemote(root/'controller.sock')
            with UnixController(root/'controller.sock', api.invoke, validate=api.validate), patch.object(
                    api.resolution, 'search', return_value={'results': []}) as search:
                with PublicServer(listener, remote) as server:
                    server.public_host = local
                    server.public_origin = 'https://books.example'
                    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
                    def request(host, origin=None, token=None, duplicates=(), forwarded=None, path='/api/resolution/search'):
                        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                        connection.putrequest('GET' if origin is None else 'POST', path, skip_host=True)
                        connection.putheader('Host', host)
                        if origin is not None:
                            connection.putheader('Origin', origin)
                            connection.putheader('X-CSRF-Token', token or '')
                            connection.putheader('Content-Type', 'application/json')
                            body = b'{"query":"isolated"}'
                            connection.putheader('Content-Length', str(len(body)))
                        else: body = b''
                        for key, value in duplicates:connection.putheader(key,value)
                        if forwarded:connection.putheader('X-Forwarded-Host',forwarded)
                        connection.endheaders(body)
                        response=connection.getresponse();result=(response.status,response.read());connection.close();return result
                    try:
                        status, raw = request('books.example', path='/api/resolution/token')
                        self.assertEqual(status, 200);token=json.loads(raw)['token']
                        self.assertEqual(request('books.example','https://books.example',token)[0],200)
                        self.assertEqual(request(local,'http://'+local,token)[0],200)
                        for host, origin, tok in [('books.example','http://books.example',token),
                                (local,'https://books.example',token),('evil.example','https://books.example',token),
                                ('books.example','https://books.example','wrong'),
                                ('books.example','https://books.example','é')]:
                            self.assertIn(request(host,origin,tok)[0],(403,409))
                        for key, value in [('Host','books.example'),('Origin','https://books.example'),('X-CSRF-Token',token)]:
                            self.assertEqual(request('books.example','https://books.example',token,[(key,value)])[0],403)
                        self.assertEqual(request('books.example',duplicates=[('Origin','https://books.example'),('Origin','https://books.example')],path='/')[0],403)
                        self.assertEqual(request('evil.example',forwarded='books.example',path='/')[0],403)
                        self.assertEqual(search.call_count,2)
                    finally:
                        server.shutdown();thread.join(2);remote.close()

if __name__ == '__main__':unittest.main()
