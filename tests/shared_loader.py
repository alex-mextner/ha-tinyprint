"""Load the pure shared modules through a synthetic package, never the HA `__init__`.

The standalone bridge does the same (bridge/tinyprint_bridge/_shared.py), so tests exercise the
exact import shape that production uses. No Home Assistant import happens here.
"""

import importlib
import sys
import types
from pathlib import Path

SHARED_DIR = Path(__file__).parents[1] / "custom_components" / "tinyprint"
PACKAGE = "tinyprint_shared"


def load(name: str):
    package = sys.modules.get(PACKAGE)
    if package is None:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(SHARED_DIR)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.{name}")
