import sys
import types
import unittest


def _install_import_stubs():
    requests_html = types.ModuleType("requests_html")
    requests_html.HTMLSession = object
    sys.modules.setdefault("requests_html", requests_html)

    google = types.ModuleType("google")
    google_oauth2 = types.ModuleType("google.oauth2")
    google_service_account = types.ModuleType("google.oauth2.service_account")
    google_service_account.Credentials = object
    google_oauth2.service_account = google_service_account
    google.oauth2 = google_oauth2
    sys.modules.setdefault("google", google)
    sys.modules.setdefault("google.oauth2", google_oauth2)
    sys.modules.setdefault("google.oauth2.service_account", google_service_account)

    googleapiclient = types.ModuleType("googleapiclient")
    googleapiclient_discovery = types.ModuleType("googleapiclient.discovery")
    googleapiclient_discovery.build = lambda *args, **kwargs: None
    googleapiclient.discovery = googleapiclient_discovery
    sys.modules.setdefault("googleapiclient", googleapiclient)
    sys.modules.setdefault("googleapiclient.discovery", googleapiclient_discovery)

    httplib2 = types.ModuleType("httplib2")
    httplib2.Http = object
    httplib2.ProxyInfo = object
    httplib2.socks = types.SimpleNamespace(PROXY_TYPE_HTTP=0)
    sys.modules.setdefault("httplib2", httplib2)

    google_auth_httplib2 = types.ModuleType("google_auth_httplib2")
    google_auth_httplib2.AuthorizedHttp = object
    sys.modules.setdefault("google_auth_httplib2", google_auth_httplib2)

    futu_group = types.ModuleType("futu_group")
    futu_group.sync_futu_group = lambda *args, **kwargs: None
    sys.modules.setdefault("futu_group", futu_group)


_install_import_stubs()

from notification_engine.engine import _feishu_cell_value_to_text


class FeishuCellValueToTextTest(unittest.TestCase):
    def test_empty_response_is_empty_text(self):
        self.assertEqual(_feishu_cell_value_to_text([]), "")
        self.assertEqual(_feishu_cell_value_to_text([[]]), "")

    def test_none_cell_is_empty_text(self):
        self.assertEqual(_feishu_cell_value_to_text([[None]]), "")

    def test_non_empty_cell_is_text(self):
        self.assertEqual(_feishu_cell_value_to_text([["existing"]]), "existing")
        self.assertEqual(_feishu_cell_value_to_text([[123]]), "123")


if __name__ == "__main__":
    unittest.main()
