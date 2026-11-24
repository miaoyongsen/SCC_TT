"""Offline tests for the WAF IP forwarding endpoint (no real SCC calls)."""
import os
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.waf_proxy import router


class FakeUpstreamClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class WafProxyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "WAF_TOKEN": "third-party-test-token",
            "SCC_API_TOKEN": "scc-secret-test-token",
            "SCC_WAF_URL": "http://127.0.0.1:8000/api/v1/waf/requests",
        })
        self.env.start()
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.env.stop()

    @patch("app.waf_proxy.httpx.AsyncClient")
    def test_get_single_ip_forwards_scc_token_and_returns_original_json(self, async_client):
        result = {"code": 0, "data": {"client_request_id": "scc_is_202611240001"}}
        fake = FakeUpstreamClient(httpx.Response(200, json=result))
        async_client.return_value = fake
        response = self.client.get("/waf_ip/update", params={
            "block_ip": "192.0.2.25/32",
            "token": "third-party-test-token",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), result)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(fake.calls[0][1]["json"], {"block_ip": "192.0.2.25/32"})
        self.assertEqual(fake.calls[0][1]["headers"]["Authorization"], "Bearer scc-secret-test-token")

    @patch("app.waf_proxy.httpx.AsyncClient")
    def test_post_multiple_ips_preserves_scc_failure(self, async_client):
        upstream_error = {"detail": "Unauthorized"}
        fake = FakeUpstreamClient(httpx.Response(401, json=upstream_error))
        async_client.return_value = fake
        response = self.client.post("/waf_ip/update", json={
            "block_ip": "192.0.2.25/32,192.0.2.26/32",
            "token": "third-party-test-token",
        })
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), upstream_error)
        self.assertEqual(fake.calls[0][1]["json"], {
            "block_ip": "192.0.2.25/32,192.0.2.26/32",
        })

    @patch("app.waf_proxy.httpx.AsyncClient")
    def test_rejects_wrong_token_and_invalid_ip(self, async_client):
        wrong = self.client.get("/waf_ip/update", params={
            "block_ip": "192.0.2.25",
            "token": "wrong",
        })
        self.assertEqual(wrong.status_code, 401)
        multiple = self.client.get("/waf_ip/update", params={
            "block_ip": "not-an-ip",
            "token": "third-party-test-token",
        })
        self.assertEqual(multiple.status_code, 422)
        async_client.assert_not_called()

    @patch("app.waf_proxy.httpx.AsyncClient")
    def test_get_invalid_or_empty_ip_never_calls_scc(self, async_client):
        cases = ["", " ", ",", "1.1.1.1/32,", ",1.1.1.1/32",
                 "1.1.1.1/32,,2.2.2.2/32", "1.1.1.999/32",
                 "1.1.1.1/33", "hello", "1.1.1.1/32,hello"]
        for value in cases:
            with self.subTest(value=value):
                response = self.client.get("/waf_ip/update", params={
                    "block_ip": value, "token": "third-party-test-token"
                })
                self.assertEqual(response.status_code, 422)
        async_client.assert_not_called()

    @patch("app.waf_proxy.httpx.AsyncClient")
    def test_post_invalid_or_empty_ip_never_calls_scc(self, async_client):
        cases = ["", "  ", "1.1.1.1/32,", ",2.2.2.2/32",
                 "1.1.1.1/32,,2.2.2.2/32", "abc", "1.1.1.1/32,abc"]
        for value in cases:
            with self.subTest(value=value):
                response = self.client.post("/waf_ip/update", json={
                    "block_ip": value, "token": "third-party-test-token"
                })
                self.assertEqual(response.status_code, 422)
        async_client.assert_not_called()

    @patch("app.waf_proxy.httpx.AsyncClient")
    def test_get_multiple_ips_forwards_as_single_block_ip(self, async_client):
        fake = FakeUpstreamClient(httpx.Response(200, json={"code": 0}))
        async_client.return_value = fake
        response = self.client.get("/waf_ip/update", params={
            "block_ip": "192.0.2.25/32,192.0.2.26/32,192.0.2.27/32",
            "token": "third-party-test-token",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake.calls[0][1]["json"], {
            "block_ip": "192.0.2.25/32,192.0.2.26/32,192.0.2.27/32"
        })

    @patch("app.waf_proxy.httpx.AsyncClient")
    def test_timeout_does_not_retry(self, async_client):
        fake = FakeUpstreamClient(error=httpx.ReadTimeout("timeout"))
        async_client.return_value = fake
        response = self.client.post("/waf_ip/update", json={
            "block_ip": "192.0.2.25",
            "token": "third-party-test-token",
        })
        self.assertEqual(response.status_code, 504)
        self.assertEqual(len(fake.calls), 1)


if __name__ == "__main__":
    unittest.main()
