from __future__ import annotations

import json
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

    def test_programs_publish_into_one_targetable_corpus_registry(self) -> None:
        platform = load_platform(ROOT / "config" / "platform.toml")
        program = load_program(ROOT / "programs" / "PDCBVC" / "program.toml", platform)
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            source = temp_root / "generated" / "PDCBVC"
            variable_dir = source / "dataflow.variable"
            variable_dir.mkdir(parents=True)
            (variable_dir / "dataflow.variable.TEST-FIELD.json").write_text("{}", encoding="utf-8")
            (source / "architecture.call_parameters.json").write_text(
                json.dumps({"calls": [{"target": "CALLED1", "call_type": "LINK"}]}),
                encoding="utf-8",
            )
            (source / "architecture.copybooks.json").write_text(
                json.dumps({"content": {"all": ["COPY1"]}}), encoding="utf-8",
            )
            (source / "controlflow.cfg.json").write_text(
                json.dumps({"nodes": [{"id": "PARA-1"}]}), encoding="utf-8",
            )
            (source / "architecture.cics_operations.json").write_text(
                json.dumps({"content": {"operations": [{
                    "command": "SEND", "paragraph": "SEND-SCREEN1",
                    "statement": "EXEC CICS SEND MAP('SCREEN1') MAPSET('SCRNSET') END-EXEC.",
                }]}}),
                encoding="utf-8",
            )

            pipeline = Pipeline(platform, program, temp_root / "runs")
            pipeline._publish_program_artifacts(source)

            self.assertEqual(pipeline.collection, "cobol-corpus")
            self.assertEqual(pipeline.legacy_collection, "cobol-pdcbvc")
            registry = json.loads(
                (pipeline.corpus_final_scripts / "corpus.registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(registry["program_count"], 1)
            values = {item["value"] for item in registry["programs"][0]["entities"]}
            self.assertTrue({"TEST-FIELD", "CALLED1", "COPY1", "PARA-1"}.issubset(values))

            # BMS map and mapset names exist only inside CICS statements. Without
            # them here the RAG resolves no entity for a map and answers that it
            # is not present in the corpus -- the registry short-circuits the
            # artifact walk, so this is the only place they can be added.
            typed = {
                (item["type"], item["value"])
                for item in registry["programs"][0]["entities"]
            }
            self.assertIn(("map", "SCREEN1"), typed)
            # Distinct types: a mapset name is frequently also a COPY member, so
            # collapsing the two would collide with the copybook entity.
            self.assertIn(("mapset", "SCRNSET"), typed)
            self.assertNotIn(("map", "SCRNSET"), typed)


if __name__ == "__main__":
    unittest.main()
