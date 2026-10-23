"""The Python layout plan against the committed golden fixtures, inside the HA image.

Pillow text output depends on the FreeType/HarfBuzz build and on libraqm, so these checks only
mean something in the Home Assistant image that CI (job `home-assistant`) runs. Regenerate with
`tools/gen_layout_cases.py --write` through that image (see the tool's header).
"""

import importlib.util
import json
import unittest
from pathlib import Path

from PIL import features

TOOL = Path(__file__).parents[1] / "tools" / "gen_layout_cases.py"
spec = importlib.util.spec_from_file_location("gen_layout_cases", TOOL)
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def load_fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class LayoutParityTest(unittest.TestCase):
    def test_raqm_is_available(self):
        self.assertTrue(features.check_feature("raqm"), "Pillow has no libraqm: layout differs")

    def test_environment_matches_the_fixtures(self):
        here = gen.build_env()
        for path in (gen.HASHES_PATH, gen.CASES_PATH):
            fixture = load_fixture(path)["env"]
            differing = [
                f"{key}: fixture {fixture.get(key)}, here {here.get(key)}"
                for key in sorted(here.keys() | fixture.keys())
                if here.get(key) != fixture.get(key)
            ]
            self.assertEqual([], differing, f"{path.name} was generated in another environment")

    def test_existing_bitmaps_are_unchanged_by_the_layout_split(self):
        expected = load_fixture(gen.HASHES_PATH)["hashes"]
        actual = gen.build_hashes()
        changed = sorted(
            k for k in expected.keys() | actual.keys() if expected.get(k) != actual.get(k)
        )
        self.assertEqual([], changed, "bitmap hashes changed for these template|font|align keys")

    def test_python_layout_matches_the_committed_fixture(self):
        expected = load_fixture(gen.CASES_PATH)
        actual = json.loads(json.dumps(gen.build_cases()))
        expected_cases = {c["name"]: c for c in expected["cases"]}
        actual_cases = {c["name"]: c for c in actual["cases"]}
        self.assertEqual(list(expected_cases), list(actual_cases), "the set of cases changed")
        for name, case in expected_cases.items():
            self.assertEqual(case, actual_cases[name], f"first differing case: {name}")
        for key in ("metrics", "derived"):
            self.assertEqual(expected[key], actual[key], f"{key} differ from the fixture")
        self.assertEqual(expected, actual)


if __name__ == "__main__":
    unittest.main()
