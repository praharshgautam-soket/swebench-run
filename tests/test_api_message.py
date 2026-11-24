from swebench_run.cluster import api_message


class E(Exception):
    def __init__(self, body):
        self.body = body


def test_extracts_the_kubernetes_message():
    body = '{"kind": "Status", "message": "pods x is forbidden: exceeded quota: swebench-cap"}'
    assert "exceeded quota: swebench-cap" in api_message(E(body))


def test_tolerates_garbage_and_missing_bodies():
    assert api_message(E("not json")) == ""
    assert api_message(E(None)) == ""
    assert api_message(Exception("no body attribute")) == ""
