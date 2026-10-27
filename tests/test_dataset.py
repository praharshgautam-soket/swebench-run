"""Grader wiring against the real swebench package (skipped if it isn't installed)."""
import pytest

pytest.importorskip("swebench")
from swebench.harness.test_spec.test_spec import make_test_spec  # noqa: E402

from swebench_run.dataset import make_grader  # noqa: E402
from swebench_run.runner import NoTestOutput  # noqa: E402

ROW = {
    "repo": "astropy/astropy",
    "instance_id": "astropy__astropy-12907",
    "base_commit": "d16bfe05a744909de4b27f5875fe0d4ed41ce607",
    "patch": "",
    "test_patch": "diff --git a/astropy/modeling/tests/test_separable.py b/astropy/modeling/tests/test_separable.py\n",
    "problem_statement": "p",
    "hints_text": "",
    "created_at": "2022-03-03T15:14:54Z",
    "version": "4.3",
    "FAIL_TO_PASS": '["astropy/modeling/tests/test_separable.py::test_separable[compound_model6-result6]"]',
    "PASS_TO_PASS": '["astropy/modeling/tests/test_separable.py::test_coord_matrix"]',
    "environment_setup_commit": "298ccb478e6bf092953bca67a3d29dc6c35f6752",
}


def spec():
    return {ROW["instance_id"]: make_test_spec(ROW, namespace="swebench")}


def test_image_name_matches_docker_hub_layout():
    ts = make_test_spec(ROW, namespace="swebench")
    assert ts.instance_image_key == "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"


def test_log_without_test_output_raises():
    grade = make_grader(spec(), "m")
    with pytest.raises(NoTestOutput):
        grade("astropy__astropy-12907", "diff", "image crashed\n")


def test_passing_log_resolves_and_failing_log_does_not():
    f2p = "astropy/modeling/tests/test_separable.py::test_separable[compound_model6-result6]"
    p2p = "astropy/modeling/tests/test_separable.py::test_coord_matrix"
    grade = make_grader(spec(), "m")

    def log(f2p_status):
        return (
            ">>>>> Start Test Output\n"
            f"{f2p_status} {f2p}\nPASSED {p2p}\n"
            ">>>>> End Test Output\n"
        )

    resolved, report = grade("astropy__astropy-12907", "diff", log("PASSED"))
    assert resolved is True
    assert report["astropy__astropy-12907"]["resolved"] is True
    assert grade("astropy__astropy-12907", "diff", log("FAILED"))[0] is False
