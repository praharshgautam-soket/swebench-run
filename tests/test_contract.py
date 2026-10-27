import base64
import io
import json
import tarfile

from swebench_run.contract import BEGIN, END, extract_result, strip_result_block


def make_block(files: dict[str, bytes], extra_members=()):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra_members:
            tar.addfile(info)
    return f"{BEGIN}\n{base64.b64encode(buf.getvalue()).decode()}\n{END}\n"


def test_extracts_files_and_meta():
    meta = json.dumps({"status": "ok", "wall_sec": 3}).encode()
    log = "agent chatter\n" + make_block({"./patch.diff": b"diff --git a b\n", "meta.json": meta})
    res = extract_result(log)
    assert res.patch.startswith("diff --git")
    assert res.meta["status"] == "ok"
    assert res.usage == {}


def test_missing_block_returns_none():
    assert extract_result("no markers here") is None


def test_garbage_block_returns_none():
    assert extract_result(f"{BEGIN}\n!!!notbase64!!!\n{END}") is None


def test_last_block_wins_over_agent_printed_marker():
    fake = make_block({"patch.diff": b"FAKE"})
    real = make_block({"patch.diff": b"REAL"})
    assert extract_result(fake + real).patch == "REAL"


def test_path_traversal_members_dropped():
    log = make_block({"../evil": b"x", "/abs": b"x", "ok.txt": b"fine"})
    assert set(extract_result(log).files) == {"ok.txt"}


def test_symlinks_ignored():
    link = tarfile.TarInfo("link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    log = make_block({"ok.txt": b"fine"}, extra_members=[link])
    assert set(extract_result(log).files) == {"ok.txt"}


def test_strip_result_block():
    log = "before\n" + make_block({"a": b"b"}) + "after"
    out = strip_result_block(log)
    assert "before" in out and "after" in out and BEGIN not in out
