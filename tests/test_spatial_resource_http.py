"""Exercise URL decoding and traversal protection with isolated files."""
import http.client
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from urllib.parse import quote
from unittest.mock import patch

from backend.common import ROOT
from backend.server import Handler


class SpatialResourceHttpTests(unittest.TestCase):
    def test_encoded_chinese_resource_and_traversal(self):
        root = ROOT / 'build' / '第三轮空间接口隔离测试' / uuid.uuid4().hex
        spatial = root / 'data' / 'derived' / 'spatial'
        image = spatial / '地形展示-隔离' / 'terrain-1200.png'
        image.parent.mkdir(parents=True)
        image.write_bytes(b'isolated terrain image')
        hidden = root / 'data' / 'hidden.txt'
        hidden.write_bytes(b'outside spatial root')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def get(path):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            connection.request('GET', path)
            response = connection.getresponse()
            result = response.status, response.getheader('Content-Type'), response.read()
            connection.close()
            return result

        try:
            with patch('backend.server.ROOT', root):
                status, content_type, body = get('/spatial/' + quote('地形展示-隔离/terrain-1200.png'))
                self.assertEqual((status, content_type, body), (200, 'image/png', b'isolated terrain image'))
                for path in (
                    '/spatial/%2e%2e/%2e%2e/hidden.txt',
                    '/spatial/%2e%2e%5c%2e%2e%5chidden.txt',
                    '/spatial/%252e%252e%252f%252e%252e%252fhidden.txt',
                    '/spatial/%2525252e%2525252e%2525252fhidden.txt',
                ):
                    status, _, body = get(path)
                    self.assertIn(status, (400, 404), path)
                    self.assertNotIn(b'outside spatial root', body)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
