import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_base_image_is_versioned_and_digest_pinned():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM python:3\.12\.\d+-slim@sha256:[0-9a-f]{64}$", dockerfile, re.MULTILINE)
