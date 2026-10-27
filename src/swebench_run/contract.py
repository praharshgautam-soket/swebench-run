"""The run contract between the controller and a harness bundle (see docs/CONTRACT.md).

The bundle's entrypoint prints one base64 tar.gz between two marker lines at the
end of the pod log. The controller reads the pod log, extracts the block, and
unpacks it here.
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import re
import tarfile
from dataclasses import dataclass, field

BEGIN = "===SBR-RESULT-BEGIN==="
END = "===SBR-RESULT-END==="

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_FILES = 200

_BLOCK = re.compile(re.escape(BEGIN) + r"\s*(.*?)\s*" + re.escape(END), re.DOTALL)


@dataclass
class ResultBundle:
    files: dict[str, bytes] = field(default_factory=dict)

    @property
    def meta(self) -> dict:
        try:
            return json.loads(self.files.get("meta.json", b"{}"))
        except json.JSONDecodeError:
            return {}

    @property
    def patch(self) -> str:
        return self.files.get("patch.diff", b"").decode("utf-8", "replace")

    @property
    def usage(self) -> dict:
        try:
            return json.loads(self.files.get("usage.json", b"{}"))
        except json.JSONDecodeError:
            return {}


def strip_result_block(log: str) -> str:
    """The log without the (large, binary-looking) result block."""
    return _BLOCK.sub("[result block removed]", log)


def extract_result(log: str) -> ResultBundle | None:
    """Parse the result block from a pod log; None if absent or unreadable."""
    matches = _BLOCK.findall(log)
    if not matches:
        return None
    # Take the last block: an agent that printed the marker text itself can't
    # shadow the entrypoint's block, which is always printed last.
    try:
        raw = base64.b64decode("".join(matches[-1].split()), validate=True)
        tar = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except (binascii.Error, tarfile.TarError, EOFError, OSError):
        return None
    files: dict[str, bytes] = {}
    with tar:
        for member in tar:
            if not member.isfile():
                continue
            name = member.name.removeprefix("./")
            parts = name.split("/")
            if name.startswith("/") or ".." in parts or not name:
                continue
            if len(files) >= MAX_FILES or member.size > MAX_FILE_BYTES:
                continue
            f = tar.extractfile(member)
            if f is not None:
                files[name] = f.read()
    return ResultBundle(files)
