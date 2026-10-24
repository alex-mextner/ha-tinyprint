"""Load the pure TinyPrint modules shared with the Home Assistant integration.

The bridge must reuse protocol.py, transport.py, att.py, bridge_wire.py, bitmap.py and
le_link.py from custom_components/tinyprint without copying them (copies drift) and without
executing that package's `__init__.py`, which imports Home Assistant. A synthetic package whose
`__path__` is the integration directory gives the modules their relative imports and nothing else.

The installer keeps the `app/bridge` + `app/custom_components/tinyprint` layout so the default
lookup works; TINYPRINT_SHARED_DIR overrides it for unusual deployments and tests.
"""

import importlib
import os
import sys
import types
from pathlib import Path

PACKAGE = "tinyprint_shared"


def shared_dir() -> Path:
    override = os.environ.get("TINYPRINT_SHARED_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "custom_components" / "tinyprint"


def _load(name: str) -> types.ModuleType:
    package = sys.modules.get(PACKAGE)
    if package is None:
        directory = shared_dir()
        if not (directory / "protocol.py").is_file():
            raise ImportError(
                f"TinyPrint shared modules not found in {directory}; "
                "keep the app/custom_components/tinyprint layout or set TINYPRINT_SHARED_DIR"
            )
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(directory)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.{name}")


protocol = _load("protocol")
transport = _load("transport")
att = _load("att")
bridge_wire = _load("bridge_wire")

le_link = _load("le_link")
