"""Shared pytest fixtures."""

import pytest
from PIL import ImageFont


@pytest.fixture
def recorded_font_paths(monkeypatch):
    """Record every path handed to `PIL.ImageFont.truetype` while the test runs."""
    opened: list[str] = []
    real = ImageFont.truetype

    def truetype(font=None, *args, **kwargs):
        opened.append(str(font))
        return real(font, *args, **kwargs)

    monkeypatch.setattr(ImageFont, "truetype", truetype)
    return opened
