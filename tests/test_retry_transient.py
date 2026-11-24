import pytest

from swebench_run.cluster import is_transient_api_error, retry_transient


class ApiErr(Exception):
    def __init__(self, status, message=""):
        self.status = status
        self.body = '{"message": "%s"}' % message


def flaky(failures, exc):
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] <= failures:
            raise exc
        return "created"

    return fn, state


def test_conflict_is_retried_until_it_works():
    fn, state = flaky(3, ApiErr(409, "the object has been modified"))
    sleeps = []
    assert retry_transient(fn, sleep=sleeps.append) == "created"
    assert state["n"] == 4 and len(sleeps) == 3


def test_quota_flavoured_403_is_retried_but_a_plain_403_is_not():
    assert is_transient_api_error(ApiErr(403, "pods x is forbidden: exceeded quota: swebench-cap"))
    assert not is_transient_api_error(ApiErr(403, "pods is forbidden: User cannot create"))
    fn, state = flaky(1, ApiErr(403, "forbidden: User cannot create"))
    with pytest.raises(ApiErr):
        retry_transient(fn, sleep=lambda s: None)
    assert state["n"] == 1


def test_gives_up_after_the_last_try_and_raises():
    fn, state = flaky(99, ApiErr(409, "conflict"))
    with pytest.raises(ApiErr):
        retry_transient(fn, tries=4, sleep=lambda s: None)
    assert state["n"] == 4


def test_non_api_errors_pass_straight_through():
    fn, state = flaky(1, ValueError("bug"))
    with pytest.raises(ValueError):
        retry_transient(fn, sleep=lambda s: None)
    assert state["n"] == 1


def test_server_trouble_statuses_are_transient():
    for status in (429, 500, 503):
        assert is_transient_api_error(ApiErr(status))
    assert not is_transient_api_error(ApiErr(404))
