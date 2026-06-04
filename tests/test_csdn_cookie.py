import unittest

from web_tool import (
    analyze_csdn_cookie_input,
    app,
    cdp_cookie_header_from_cookies,
    cdp_json,
    cdp_page_websocket_url,
    clean_clipboard_text,
    normalize_cdp_port,
    resolve_csdn_cdp_port,
)
from unittest.mock import patch


class CsdnCookieTest(unittest.TestCase):
    def test_document_cookie_only_is_reported_as_incomplete(self) -> None:
        analysis = analyze_csdn_cookie_input("UN=qq_40141758; c_ins_fpage=/index.html; _qimei_uuid42=abc")
        self.assertFalse(analysis["ok"])
        self.assertTrue(analysis["looksIncomplete"])
        self.assertIn("自动浏览器", analysis["message"])

    def test_valid_auth_cookie_shape_passes_local_analysis(self) -> None:
        analysis = analyze_csdn_cookie_input("UserToken=abc; UserName=fengsha; uuid_tt_dd=123")
        self.assertTrue(analysis["ok"])
        self.assertTrue(analysis["hasUserToken"])

    def test_clean_clipboard_text_removes_title_and_data_uri_noise(self) -> None:
        text = clean_clipboard_text("<title>Demo</title>\n\n# Demo\n\n![](data:image/png;base64,AAAA)")
        self.assertNotIn("<title>", text)
        self.assertNotIn("base64", text)
        self.assertIn("# Demo", text)

    def test_cookie_test_stops_before_network_for_incomplete_cookie(self) -> None:
        with app.test_client() as client:
            response = client.post(
                "/api/csdn/cookie/test",
                json={"cdpPort": ""},
            )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertIn("自动浏览器", payload["error"])

    def test_cdp_cookie_header_keeps_csdn_auth_cookies(self) -> None:
        cookies = [
            {"name": "other", "value": "skip", "domain": ".example.com", "path": "/"},
            {"name": "uuid_tt_dd", "value": "123", "domain": ".csdn.net", "path": "/"},
            {"name": "UserToken", "value": "abc", "domain": ".csdn.net", "path": "/"},
            {"name": "UserToken", "value": "weaker", "domain": "blog.csdn.net", "path": "/"},
        ]
        header = cdp_cookie_header_from_cookies(cookies)
        self.assertIn("uuid_tt_dd=123", header)
        self.assertIn("UserToken=abc", header)
        self.assertNotIn("other=skip", header)
        self.assertNotIn("UserToken=weaker", header)

    def test_cdp_port_normalization(self) -> None:
        self.assertEqual(normalize_cdp_port("9223"), 9223)
        self.assertEqual(normalize_cdp_port(""), 9222)
        with self.assertRaises(Exception):
            normalize_cdp_port("not-a-port")

    def test_cdp_json_falls_back_from_ipv4_to_localhost(self) -> None:
        def fake_for_host(host, port, path, method="GET", timeout=3):
            if host == "127.0.0.1":
                raise Exception("wrong listener")
            return {"ok": True, "host": host}

        with patch("web_tool.cdp_json_for_host", side_effect=fake_for_host):
            self.assertEqual(cdp_json(9222, "/json/version")["host"], "localhost")

    def test_cdp_page_lookup_accepts_localhost_target(self) -> None:
        targets = [
            (
                "localhost",
                [
                    {
                        "type": "page",
                        "url": "https://mp.csdn.net/mp_blog/creation/editor",
                        "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/abc",
                    }
                ],
            )
        ]
        with patch("web_tool.iter_cdp_targets", return_value=targets):
            self.assertEqual(resolve_csdn_cdp_port("9222"), 9222)
            self.assertEqual(cdp_page_websocket_url(9222), "ws://localhost:9222/devtools/page/abc")


if __name__ == "__main__":
    unittest.main()
