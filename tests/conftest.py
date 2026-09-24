"""Load AstrBot from the sibling checkout without modifying runtime data."""

import os
import sys
import tempfile
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE))
ASTRBOT = WORKSPACE / "AstrBot"
if ASTRBOT.is_dir():
    sys.path.insert(0, str(ASTRBOT))

_runtime = tempfile.TemporaryDirectory(prefix="markdown-image-test-")
_previous_root = os.environ.get("ASTRBOT_ROOT")
os.environ["ASTRBOT_ROOT"] = _runtime.name


def pytest_unconfigure(config):
    if _previous_root is None:
        os.environ.pop("ASTRBOT_ROOT", None)
    else:
        os.environ["ASTRBOT_ROOT"] = _previous_root
    _runtime.cleanup()
