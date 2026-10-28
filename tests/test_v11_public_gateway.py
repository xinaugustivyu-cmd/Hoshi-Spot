"""本机短时只读网关测试；使用假上游，不连接 Binance。"""

import http.client
import threading
import unittest
from http.server import ThreadingHTTPServer

from hoshi_v11.public_gateway import make_handler
from hoshi_v11.order_guard import RealOrderBlocked, _check_fetch


class PublicGatewayTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def upstream(path):
            self.calls.append(path)
            return b"[]"

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(upstream))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method, path):
        self.connection.request(method, path)
        response = self.connection.getresponse()
        response.read()
        return response.status

    def test_only_public_spot_get_reaches_market_only_upstream(self):
        self.assertEqual(self.call("GET", "/api/v3/klines?symbol=BTCUSDT&interval=1h"), 200)
        self.assertEqual(self.calls, ["/api/v3/klines?symbol=BTCUSDT&interval=1h"])
        for method, path in (("POST", "/api/v3/order"),
                             ("DELETE", "/api/v3/order"),
                             ("GET", "/api/v3/order"),
                             ("GET", "/sapi/v1/capital/withdraw/apply"),
                             ("GET", "http://evil.example/api/v3/klines"),
                             ("GET", "/api/v3/klines?signature=secret"),
                             ("GET", "/api/v3/klines?APIKEY=secret")):
            with self.subTest(method=method, path=path):
                self.assertIn(self.call(method, path), {403, 405})
        self.assertEqual(len(self.calls), 1)

    def test_ccxt_low_level_guard_only_allows_internal_public_get(self):
        _check_fetch("http://hoshi-market-ingest:8088/api/v3/klines", "GET")
        for url, method in (("http://hoshi-market-ingest:8088/api/v3/order", "GET"),
                            ("http://hoshi-market-ingest:8088/api/v3/klines", "POST"),
                            ("http://evil:8088/api/v3/klines", "GET")):
            with self.subTest(url=url, method=method), self.assertRaises(RealOrderBlocked):
                _check_fetch(url, method)


if __name__ == "__main__":
    unittest.main()
