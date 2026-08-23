"""Regression checks for the vendored session-mirror executable."""

import hashlib
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "bin" / "subfleet-mirror"
VENDORED_SHA256 = "ec9bfc1bcad6fc708b1bb7e0c3b2d2fe12d977c4186bdbd5ae3cfa41dbdd04b2"


def test_vendored_script_bytes_are_pinned():
    assert hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == VENDORED_SHA256


def test_usage_and_parser_use_public_command_name():
    source = SCRIPT.read_text()
    assert "Usage:\n    subfleet mirror" in source
    assert 'prog="subfleet mirror"' in source
    assert 'version="subfleet mirror %s" % __version__' in source


def test_version_uses_public_command_name():
    result = subprocess.run(
        [str(SCRIPT), "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "subfleet mirror 5.0"
