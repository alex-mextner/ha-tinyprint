# Changelog

## 0.3.0

### Added
- Live WYSIWYG label editor: the panel renders the strip in the browser and prints what you see ([Editor](studio.md#editor)).
- Newspaper `columns` 1-3 for the main text of templates ([Editor](studio.md#editor)).
- Bundled variable Roboto Flex (width and weight axes, sizes 12-96) and pixel font Tiny5, both OFL ([Fonts](studio.md#fonts)).
- Options `font`, `font_width`, `font_weight`, `columns` in documents ([Images and controls](studio.md#images-and-controls)).
- Action `tinyprint.print_bitmap` (`device_id`, `rows`, `bitmap_b64`; response has `bitmap_sha256`) ([Actions](studio.md#actions)).
- `printer_info` returns each printer's `entity_id`, plus top-level `in_flight_states` and `fonts`.
- Renderer self-check in the panel diagnostics ([Renderer self-check](studio.md#renderer-self-check)).

### Changed
- The default font is now the bundled Roboto Flex: the output, and `expected_sha256`, of existing automations change; pin `font_path` to keep the old output ([Panel and automations](studio.md#panel-and-automations-use-different-renderers)).
- `font_size` is 12-96 for Roboto Flex (legacy `print_text` keeps 12-64); title and heading derived sizes are capped at `max(64, font_size)`.
- The panel no longer calls `preview` or `print_document`; both stay for automations (Pillow renderer).
- `font_path` is an override; the `/config/tinyprint_fonts` drop-in directory is no longer searched ([Fonts](studio.md#fonts)).

### Notes
- Columns 2-3 and Tiny5 at width 200 are browser-only; Pillow actions reject them. Panel and Pillow output are not pixel-identical ([details](studio.md#panel-and-automations-use-different-renderers)).
- Phone support: the renderer gate passed in iPhone Safari (user agent `Version/27.0.1`, `iPhone OS 18_7`); the Companion app and paper output are not yet verified ([Phone](studio.md#phone-and-companion-app)).
- `print_bitmap` data appears in `call_service` events and is stored by the recorder; see [Privacy and persistence](studio.md#privacy-and-persistence).
- Characters missing from the bundled fonts print as your device's fallback glyphs in the panel and as a missing-glyph box in Pillow actions ([Fonts](studio.md#fonts)).
