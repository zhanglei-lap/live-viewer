import json
import os
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import requests

from live_viewer.core import PlatformClient, ViewerError, new_session
from live_viewer.network import IPv4HTTPSConnection, IPv4HTTPSPool, connection_message
from live_viewer.qr_login import LoginHttp
from live_viewer.settings import sanitize_settings


class NetworkTests(unittest.TestCase):
    def test_ipv4_priority_falls_back_to_ipv6_without_changing_global_socket_api(self):
        addresses = [(socket.AF_INET6,socket.SOCK_STREAM,6,'',('::1',443,0,0)),
                     (socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
        failed, fallback = Mock(), Mock()
        failed.connect.side_effect=TimeoutError('SYNTHETIC')
        connection=IPv4HTTPSConnection('cdn.example',443,timeout=1)
        original=socket.getaddrinfo
        with patch('live_viewer.network.socket.getaddrinfo',return_value=addresses), \
             patch('live_viewer.network.socket.socket',side_effect=[failed,fallback]) as create:
            self.assertIs(connection._new_conn(), fallback)
            self.assertEqual([call.args[0] for call in create.call_args_list],[socket.AF_INET,socket.AF_INET6])
            failed.close.assert_called_once();fallback.close.assert_not_called()
        self.assertIs(socket.getaddrinfo, original)

    def test_direct_pool_preserves_tls_connection_type_and_is_scoped_to_session(self):
        from urllib3.poolmanager import pool_classes_by_scheme
        original=dict(pool_classes_by_scheme)
        direct, system=new_session(),new_session(network_mode='system')
        try:
            self.assertIs(direct.get_adapter('https://').poolmanager.pool_classes_by_scheme['https'],IPv4HTTPSPool)
            self.assertEqual(pool_classes_by_scheme, original)
            self.assertIs(system.get_adapter('https://').poolmanager.pool_classes_by_scheme['https'],original['https'])
            self.assertTrue(issubclass(IPv4HTTPSConnection,requests.packages.urllib3.connection.HTTPSConnection))
        finally: direct.close();system.close()

    def test_direct_ignores_invalid_proxy_while_system_mode_honors_it(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(b'local-test')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        environment = {'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}
        try:
            with patch.dict(os.environ, environment, clear=True):
                direct, system = new_session(), new_session(network_mode='system')
                try:
                    url = f'http://127.0.0.1:{server.server_port}/probe'
                    self.assertEqual(direct.get(url, timeout=1).content, b'local-test')
                    with self.assertRaises(requests.exceptions.ProxyError): system.get(url, timeout=1)
                finally: direct.close();system.close()
        finally: server.shutdown();server.server_close()

    def test_network_errors_hide_signed_query_and_proxy_credentials(self):
        url = 'https://api.live.bilibili.com/room?TOKEN=SECRET'
        for exc, category in [(requests.exceptions.ProxyError('SECRET'), '代理'),
                              (requests.exceptions.SSLError('SECRET'), 'TLS'),
                              (requests.exceptions.Timeout('SECRET'), '超时'),
                              (requests.exceptions.ConnectionError(socket.gaierror(-2, 'SECRET')), 'DNS')]:
            message = connection_message(exc, '平台接口', url)
            self.assertIn(category, message);self.assertIn('api.live.bilibili.com', message)
            self.assertNotIn('SECRET', message);self.assertNotIn('TOKEN', message)

    def test_platform_api_keeps_stage_on_connection_failure(self):
        client = PlatformClient('bilibili', network_mode='direct')
        try:
            with patch.object(client.session, 'request', side_effect=requests.exceptions.SSLError('SECRET')):
                with self.assertRaises(ViewerError) as error: client.resolve('35')
            self.assertIn('平台接口：TLS', str(error.exception))
            self.assertNotIn('SECRET', str(error.exception))
        finally: client.close()

    def test_route_is_opt_in_and_settings_do_not_store_proxy_urls(self):
        self.assertEqual(sanitize_settings({})['network_mode'], 'direct')
        self.assertEqual(sanitize_settings({'network_mode':'system'})['network_mode'], 'system')
        self.assertNotIn('SECRET', json.dumps(sanitize_settings({'proxy_url':'SECRET','network_mode':'bad'})))

    @unittest.skipUnless(os.name == 'nt', 'Windows curl fallback')
    def test_curl_login_fallback_obeys_direct_policy(self):
        http = LoginHttp('bilibili')
        result = Mock(returncode=0, stdout=b'HTTP/1.1 200 OK\r\n\r\n{}')
        try:
            with patch('live_viewer.qr_login.subprocess.run', return_value=result) as run:
                http._curl('GET', 'https://passport.bilibili.com/demo', None)
            payload = run.call_args.kwargs['input']
            self.assertIn(b'noproxy = "*"', payload)
            self.assertIn(b'proxy = ""', payload)
        finally: http.close()


if __name__ == '__main__': unittest.main()
