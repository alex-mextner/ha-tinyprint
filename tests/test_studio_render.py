"""Exercise the real pure renderer without mocking Pillow or QR encoding."""

import base64
import io
from pathlib import Path

import pytest
from PIL import Image, ImageOps
from shared_loader import load

r = load("studio_render")


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


BUNDLED = Path(r.__file__).parent / "frontend" / "fonts"


def black_pixels(image):
    return image.convert("L").histogram()[0]


def ink_width(image):
    box = ImageOps.invert(image.convert("L")).getbbox()
    return box[2] - box[0]


def render(**options):
    return r.render_document(document(options=options))


def test_default_font_is_bundled_roboto_flex_not_a_system_font(recorded_font_paths):
    assert r.normalize_options({})["font"] == "roboto_flex"
    r.render_document(document())
    assert recorded_font_paths
    assert all(Path(p).parent == BUNDLED for p in recorded_font_paths)


def test_font_path_pinned_to_the_bundled_file_matches_the_default_render():
    pinned = r.render_document(
        document(), font_path=str(BUNDLED / "RobotoFlex.ttf"), is_allowed_path=lambda p: True
    )
    assert r.bitmap(pinned) == r.bitmap(r.render_document(document()))


def test_pillow_axes_follow_fvar_order():
    narrow, wide = render(font_width=25), render(font_width=151)
    light, heavy = render(font_weight=100), render(font_weight=1000)
    assert r.bitmap(narrow) != r.bitmap(wide)
    assert r.bitmap(light) != r.bitmap(heavy)
    assert ink_width(wide) > ink_width(narrow)
    assert black_pixels(heavy) > black_pixels(light)


@pytest.mark.parametrize("value", [24, 152, True, 100.5, "100"])
def test_font_width_validates_before_pillow_clamps(value):
    with pytest.raises(ValueError, match="font_width"):
        r.normalize_options({"font_width": value})


@pytest.mark.parametrize("value", [99, 1001, True, 400.5])
def test_font_weight_validates_before_pillow_clamps(value):
    with pytest.raises(ValueError, match="font_weight"):
        r.normalize_options({"font_weight": value})


@pytest.mark.parametrize(
    "options",
    [{"font_width": w} for w in (25, 100, 151)] + [{"font_weight": w} for w in (100, 400, 1000)],
)
def test_font_axis_edges_render(options):
    assert render(**options).width == 384


def test_font_width_message_names_the_key_and_bounds():
    with pytest.raises(ValueError, match="font_width must be between 25 and 151"):
        r.normalize_options({"font_width": 24})


@pytest.mark.parametrize("name", ["", "arial", None, True, 1, "Roboto_Flex", [], {}])
def test_font_is_a_closed_enum_in_options(name):
    with pytest.raises(ValueError):
        r.normalize_options({"font": name})


@pytest.mark.parametrize("name", ["roboto_flex", "tiny5"])
def test_both_bundled_fonts_render(name):
    assert render(font=name).width == 384


@pytest.mark.parametrize(
    ("font", "size", "ok"),
    [
        ("roboto_flex", 11, False),
        ("roboto_flex", 12, True),
        ("roboto_flex", 96, True),
        ("roboto_flex", 97, False),
        ("roboto_flex", 24.0, False),
        ("roboto_flex", True, False),
        ("tiny5", 12, False),
        ("tiny5", 8, True),
        ("tiny5", 16, True),
        ("tiny5", 48, True),
        ("tiny5", 56, False),
        ("tiny5", 16.0, False),
        ("tiny5", True, False),
        ("tiny5", "16", False),
    ],
)
def test_font_size_bounds_follow_the_font(font, size, ok):
    options = {"font": font, "font_size": size}
    if ok:
        assert r.normalize_options(options)["font_size"] == size
    else:
        with pytest.raises(ValueError, match="font_size"):
            r.normalize_options(options)


@pytest.mark.parametrize("value", [0, 2, 3, True, "1", 1.0])
def test_columns_only_one_is_accepted(value):
    with pytest.raises(ValueError, match="columns"):
        r.normalize_options({"columns": value})


@pytest.mark.parametrize("value", [2, 3])
def test_columns_two_and_three_point_to_the_studio_editor(value):
    with pytest.raises(ValueError, match="columns 2 and 3 are available only in the Studio editor"):
        r.normalize_options({"columns": value})
    assert r.normalize_options({"columns": 1})["columns"] == 1


def test_tiny5_pillow_limits():
    with pytest.raises(ValueError, match="font_width 200 is available only in the Studio editor"):
        r.normalize_options({"font": "tiny5", "font_width": 200})
    with pytest.raises(ValueError, match="font_weight"):
        r.normalize_options({"font": "tiny5", "font_weight": 700})
    assert render(font="tiny5", font_width=100).width == 384


def test_derived_sizes_follow_the_font():
    fields = {"title": "T", "text": "x", "footer": "f"}
    options = {"font": "tiny5", "font_size": 16}
    plan = r.layout_blocks(document(template="note", fields=fields, options=options))
    # title (16 + 6 snapped to the 8 grid), body, footer (16 - 4 snapped)
    assert [b["size"] for b in plan["blocks"]] == [24, 16, 16]


ALL_TEMPLATES = ["note", "heading", "checklist", "qr_label", "storage_label", "spool_label"]


def example(name, **options):
    return document(template=name, fields=dict(r.TEMPLATES[name]["example"]), options=options)


@pytest.mark.parametrize("name", ALL_TEMPLATES)
def test_layout_blocks_describe_the_rendered_label(name):
    doc = example(name)
    plan = r.layout_blocks(doc)
    blocks = plan["blocks"]
    assert plan["rows"] == r.render_document(doc).height
    assert blocks[0]["y"] == 8
    for before, after in zip(blocks, blocks[1:]):
        assert after["y"] == before["y"] + before["h"]
    assert sum(b["h"] for b in blocks) + 16 == plan["rows"]
    for block in blocks:
        if block["kind"] == "text":
            assert block["text_y"] == block["y"] + 8
            assert block["h"] == len(block["lines"]) * block["step"] + 16
            assert block["step"] == block["ascent"] + block["descent"] + 5
            assert block["w"] == 384 - block["x"] - 10
        else:
            assert block["lines"] == [] and block["x"] == (384 - block["w"]) // 2


@pytest.mark.parametrize("name", ALL_TEMPLATES)
def test_every_ink_row_lies_inside_a_block_band_or_the_border(name):
    for border in (False, True):
        doc = example(name, border=border)
        plan = r.layout_blocks(doc)
        image = r.render_document(doc).convert("L")
        low, high = (0, plan["rows"]) if border else (8, plan["rows"] - 8)
        for y in range(plan["rows"]):
            ink = image.crop((0, y, 384, y + 1)).getextrema()[0] == 0
            if ink:
                assert low <= y < high, (name, border, y)


def test_title_is_never_smaller_than_body_text_at_size_96():
    for name in ("note", "heading"):
        plan = r.layout_blocks(example(name, font_size=96))
        assert plan["blocks"][0]["field"] == "title" and plan["blocks"][0]["size"] >= 96
    sizes = {"note": 30, "heading": 38}
    for name, title in sizes.items():
        plan = r.layout_blocks(example(name, font_size=24))
        assert plan["blocks"][0]["size"] == title
    plan = r.layout_blocks(document(template="note", fields={"text": "x", "footer": "f"}))
    assert plan["blocks"][-1]["field"] == "footer" and plan["blocks"][-1]["size"] == 20


def test_layout_blocks_for_text_and_qr_documents():
    plan = r.layout_blocks({"kind": "text", "text": "one\ntwo", "options": {"font_size": 24}})
    assert [b["field"] for b in plan["blocks"]] == ["text"]
    assert plan["blocks"][0]["lines"] == ["one", "two"]
    doc = {"kind": "qr", "text": "https://example.com"}
    plan = r.layout_blocks(doc)
    (block,) = plan["blocks"]
    assert block["kind"] == "qr" and block["field"] == "qr" and block["y"] == 8
    assert plan["rows"] == block["h"] + 16 == r.render_document(doc).height


def test_layout_blocks_rejects_image_documents():
    with pytest.raises(ValueError, match="template, text and qr"):
        r.layout_blocks({"kind": "image", "path": "/x.png"})


def test_spool_rows_are_one_band_with_value_anchors():
    plan = r.layout_blocks(example("spool_label"))
    rows = [b for b in plan["blocks"] if b["field"] == "spool_rows"]
    assert len(rows) == 1
    block = rows[0]
    assert [a["field"] for a in block["anchors"]] == ["material", "color", "weight", "nozzle"]
    assert block["lines"][0].startswith("Материал: ")
    for index, anchor in enumerate(block["anchors"]):
        assert anchor["text_y"] == block["text_y"] + index * block["step"]
        assert anchor["h"] == block["step"] and anchor["x"] > block["x"]
        assert 0 < anchor["w"] and anchor["x"] + anchor["w"] == 384 - 10


def test_spool_anchor_follows_the_line_where_the_value_starts():
    # At size 96 "Материал:" no longer fits beside its value, so the value starts a line lower.
    plan = r.layout_blocks(example("spool_label", font_size=96))
    block = next(b for b in plan["blocks"] if b["field"] == "spool_rows")
    for anchor in block["anchors"]:
        assert 0 < anchor["w"] and anchor["x"] + anchor["w"] == 374 and anchor["h"] > 0
        assert (
            anchor["text_y"] + anchor["h"] <= block["text_y"] + len(block["lines"]) * block["step"]
        )
    first = block["anchors"][0]
    assert first["text_y"] > block["text_y"] and first["x"] == 10


def test_layout_blocks_authorizes_font_files_like_render_document(tmp_path):
    font = tmp_path / "x.ttf"
    doc = {"kind": "text", "text": "x"}
    with pytest.raises(ValueError, match="allowed Home Assistant path"):
        r.layout_blocks(doc, str(font))
    with pytest.raises(ValueError, match="allowed Home Assistant path"):
        r.layout_blocks(doc, str(font), lambda path: False)


def test_checklist_ignores_align():
    left = r.layout_blocks(example("checklist", align="left"))
    centre = r.layout_blocks(example("checklist", align="center"))
    assert left == centre
