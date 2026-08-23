from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cobol_rag_platform.config import load_platform, load_program
from cobol_rag_platform.pipeline import Pipeline, fingerprint_paths


ROOT = Path(__file__).resolve().parents[1]


class PlatformTests(unittest.TestCase):
    def test_pdcbvc_manifest_resolves_real_inputs(self) -> None:
        platform = load_platform(ROOT / "config" / "platform.toml")
        program = load_program(ROOT / "programs" / "PDCBVC" / "program.toml", platform)
        self.assertEqual(program.name, "PDCBVC")
        self.assertTrue(program.cobol_source.is_file())
        self.assertTrue(program.copybooks.is_dir())
        self.assertTrue(program.mapa.is_file())
        self.assertTrue(program.controlflow.is_file())

    def test_plan_selects_combined_mode_when_rekt_exporter_is_configured(self) -> None:
        platform = load_platform(ROOT / "config" / "platform.toml")
        program = load_program(ROOT / "programs" / "PDCBVC" / "program.toml", platform)
        with tempfile.TemporaryDirectory() as temp:
            plan = Pipeline(platform, program, Path(temp)).plan()
        analysis = next(item for item in plan if item["stage"] == "analysis")
        self.assertEqual(analysis["mode"], "both")

    def test_fingerprint_changes_with_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.txt"
            path.write_text("first", encoding="utf-8")
            before = fingerprint_paths([path])
            path.write_text("second", encoding="utf-8")
            after = fingerprint_paths([path])
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
