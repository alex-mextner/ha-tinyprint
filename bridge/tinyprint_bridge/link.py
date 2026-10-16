"""Compatibility import: bridge and HA use the same explicit-LE socket implementation."""

import sys

from ._shared import le_link

sys.modules[__name__] = le_link
