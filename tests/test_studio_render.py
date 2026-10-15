"""Exercise the real pure renderer without mocking Pillow or QR encoding."""

import base64
import importlib.util
import io
import sys
from pathlib import Path

import pytest
from PIL import Image

PATH = Path(__file__).parents[1] / "custom_components/tinyprint/studio_render.py"
spec = importlib.util.spec_from_file_location("tinyprint_studio_renderer", PATH)
r = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = r
spec.loader.exec_module(r)


def encoded(image):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return base64.b64encode(stream.getvalue()).decode()


def document(**overrides):
    return {
        "kind": "template",
        "template": "note",
        "fields": {"title": "Заметка", "text": "Привет, Белград!"},
        **overrides,
    }


@pytest.mark.parametrize(
    "name", ["note", "heading", "checklist", "qr_label", "storage_label", "spool_label"]
)
def test_every_template_has_real_raster(name):
    fields = dict(r.TEMPLATES[name]["example"])
    image = r.render_document(document(template=name, fields=fields))
    assert image.mode == "1" and image.width == 384 and 1 <= image.height <= 2048
    assert image.getextrema() == (0, 255)


def test_preview_is_exact_print_bitmap():
    image = r.render_document(document())
    preview = r.preview_result(image)
    restored = Image.open(io.BytesIO(base64.b64decode(preview["png_base64"])))
    assert r.bitmap(restored) == r.bitmap(image)
    assert preview["width"] == 384 and preview["height"] == image.height
    assert 0 < preview["ink_percent"] < 100


@pytest.mark.parametrize("method", ["threshold", "floyd_steinberg", "ordered"])
def test_all_image_methods_are_binary_deterministic(method):
    gradient = Image.linear_gradient("L").resize((256, 128))
    doc = {"kind": "image", "image_base64": encoded(gradient), "options": {"method": method}}
    one, two = r.render_document(doc), r.render_document(doc)
    assert one.mode == "1" and one.tobytes() == two.tobytes()
    assert one.getextrema() == (0, 255)


def test_methods_produce_different_halftones():
    source = encoded(Image.new("L", (100, 100), 125))
    outputs = [
        r.render_document(
            {"kind": "image", "image_base64": source, "options": {"method": method}}
        ).tobytes()
        for method in ["threshold", "floyd_steinberg", "ordered"]
    ]
    assert len(set(outputs)) == 3


def test_transparency_is_white_not_black():
    out = r.render_document(
        {"kind": "image", "image_base64": encoded(Image.new("RGBA", (100, 100), (0, 0, 0, 0)))}
    )
    assert out.getextrema() == (255, 255)


def test_rotation_and_original_size():
    doc = {
        "kind": "image",
        "image_base64": encoded(Image.new("L", (30, 70), 0)),
        "options": {"fit": "original", "rotate": 90},
    }
    assert r.render_document(doc).height == 30


def test_unknown_fields_and_options_rejected():
    for doc in [
        document(surprise=True),
        document(options={"unknown": 1}),
        document(fields={"text": "x", "exec": "anything"}),
    ]:
        with pytest.raises(ValueError):
            r.render_document(doc)


@pytest.mark.parametrize(
    "options",
    [
        {"contrast": float("nan")},
        {"brightness": float("inf")},
        {"font_size": True},
        {"width": 99999},
        {"invert": "yes"},
        {"rotate": 45},
        {"method": "unknown"},
        {"threshold": -1},
    ],
)
def test_invalid_options_rejected(options):
    with pytest.raises(ValueError):
        r.render_document(document(options=options))


@pytest.mark.parametrize(
    "doc",
    [
        {"kind": "text", "text": " "},
        {"kind": "text", "text": "x" * 3001},
        {"kind": "qr", "text": "x" * 501},
        {"kind": "image", "image_base64": "invalid!"},
        {"kind": "template", "template": "missing"},
        {"kind": "template", "template": "qr_label", "fields": {}},
    ],
)
def test_invalid_payload_rejected(doc):
    with pytest.raises(ValueError):
        r.render_document(doc)


def test_local_paths_require_explicit_authorization(tmp_path):
    path = tmp_path / "image.png"
    Image.new("L", (30, 30)).save(path)
    doc = {"kind": "image", "path": str(path)}
    with pytest.raises(ValueError, match="allowed"):
        r.render_document(doc)
    assert r.render_document(doc, is_allowed_path=lambda p: p == str(path)).width == 384


def test_image_sources_mutually_exclusive():
    with pytest.raises(ValueError):
        r.render_document({"kind": "image", "path": "/x", "image_base64": "eA=="})


def test_oversize_source_rejected_before_decode():
    large = Image.new("1", (2001, 2000), 1)
    with pytest.raises(ValueError, match="pixels"):
        r.render_document({"kind": "image", "image_base64": encoded(large)})


def test_tall_text_fails_without_truncation():
    with pytest.raises(ValueError, match="rows"):
        r.render_document({"kind": "text", "text": "word\n" * 550})


def test_qr_decodes_and_ignores_image_processing():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    text = "https://example.test/spool/42"
    image = r.render_document(
        {
            "kind": "qr",
            "text": text,
            "options": {"invert": True, "contrast": 3, "method": "ordered"},
        }
    )
    data, _, _ = cv2.QRCodeDetector().detectAndDecode(np.array(image.convert("L")))
    assert data == text
    assert all(image.getpixel((x, 0)) == 255 for x in range(384))


def test_never_prints_or_opens_network(monkeypatch):
    import socket

    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("No network in rendering"))
    r.preview_result(r.render_document(document()))


def test_non_regular_local_input_rejected_before_open(tmp_path, monkeypatch):
    import os

    pipe = tmp_path / "fifo.png"
    os.mkfifo(pipe)

    def forbidden_open(*args, **kwargs):
        raise AssertionError("A FIFO must not be opened by the renderer")

    monkeypatch.setattr(Path, "open", forbidden_open)
    with pytest.raises(ValueError, match="regular"):
        r.render_document({"kind": "image", "path": str(pipe)}, is_allowed_path=lambda path: True)
