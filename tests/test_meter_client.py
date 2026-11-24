import json

from swebench_run.meter import KubeMeterClient, stats_to_usage


def test_stats_to_usage_maps_fields_and_keeps_unknown_unknown():
    assert stats_to_usage(None) == {}
    assert stats_to_usage({"calls": 0}) == {}
    u = stats_to_usage({"calls": 5, "ok_calls": 4, "prompt_tokens": 100, "completion_tokens": 7})
    assert u == {"input_tokens": 100, "output_tokens": 7, "steps": 4}


class FakeApi:
    def __init__(self):
        self.calls = []

    def call_api(self, path, method, **kw):
        self.calls.append((method, path, kw))

        class R:
            data = json.dumps({"calls": 2}).encode()

        return R()


def test_client_uses_the_service_proxy_path_with_the_admin_token():
    api = FakeApi()
    c = KubeMeterClient(api, "bench", "sbr-meter", 8080, "tok123")
    c.reset("k1")
    assert c.usage("k1") == {"calls": 2}
    (m1, p1, kw1), (m2, p2, _) = api.calls
    assert (m1, p1) == ("POST", "/api/v1/namespaces/bench/services/sbr-meter:8080/proxy/admin/tok123/reset/k1")
    assert (m2, p2) == ("GET", "/api/v1/namespaces/bench/services/sbr-meter:8080/proxy/admin/tok123/usage/k1")
    assert kw1["_preload_content"] is False


class NewStyleApi:
    """kubernetes client >= 30: call_api(method, url, header_params, body, post_params, _request_timeout)."""

    class configuration:  # noqa: N801
        host = "https://127.0.0.1:10443/"

        @staticmethod
        def auth_settings():
            return {"BearerToken": {"in": "header", "key": "authorization", "value": "Bearer abc"}}

    def __init__(self):
        self.calls = []

    def call_api(self, method, url, header_params=None, body=None, post_params=None, _request_timeout=None):
        self.calls.append((method, url, header_params))

        class R:
            data = None

            def read(self):
                self.data = json.dumps({"calls": 3}).encode()

        return R()


def test_client_works_with_the_new_kubernetes_client_signature():
    api = NewStyleApi()
    c = KubeMeterClient(api, "bench", "sbr-meter", 8080, "tok123")
    assert c.usage("k1") == {"calls": 3}
    method, url, headers = api.calls[0]
    assert method == "GET"
    assert url == "https://127.0.0.1:10443/api/v1/namespaces/bench/services/sbr-meter:8080/proxy/admin/tok123/usage/k1"
    assert headers == {"authorization": "Bearer abc"}


def test_healthy_reports_true_false_and_never_raises():
    class Ok(FakeApi):
        def call_api(self, path, method, **kw):
            self.calls.append((method, path, kw))

            class R:
                data = b'{"ok": true}'

            return R()

    ok = Ok()
    assert KubeMeterClient(ok, "bench", "sbr-meter", 8080, "t").healthy() is True
    assert ok.calls[0][1].endswith("/services/sbr-meter:8080/proxy/healthz")

    class Down(FakeApi):
        def call_api(self, *a, **k):
            raise ConnectionError("no route")

    assert KubeMeterClient(Down(), "bench", "sbr-meter", 8080, "t").healthy() is False
