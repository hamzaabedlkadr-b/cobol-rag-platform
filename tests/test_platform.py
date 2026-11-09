from __future__ import annotations

import errno
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cobol_rag_platform.config import load_platform, load_program
from cobol_rag_platform.pipeline import Pipeline, fingerprint_paths


ROOT = Path(__file__).resolve().parents[1]


class PlatformTests(unittest.TestCase):
    def test_doctor_rejects_missing_configured_jcl(self) -> None:
        platform = load_platform(ROOT / "config" / "platform.toml")
        program = load_program(ROOT / "programs" / "PDCBVC" / "program.toml", platform)
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "jcl"
            checks = Pipeline(platform, replace(program, jcl=missing), Path(temp) / "runs").doctor()
        self.assertIn(("error", "JCL", str(missing)), checks)

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

    def test_publish_reuses_cached_jcl_without_copying_it(self) -> None:
        platform = load_platform(ROOT / "config" / "platform.toml")
        program = load_program(ROOT / "programs" / "PDCBVC" / "program.toml", platform)
        with tempfile.TemporaryDirectory() as temp:
            pipeline = Pipeline(platform, program, Path(temp) / "runs")
            source = pipeline.analysis_output / "artifacts"
            job = source / "jcl" / "JOB1"
            job.mkdir(parents=True)
            (job / "jcl.summary.json").write_text('{"job": "JOB1"}')
            (source / "jcl.file_io.json").write_text('{"content": {"known_jobs": ["JOB1"]}}')
            destination = pipeline.corpus_final_scripts / program.name
            (pipeline.corpus_final_scripts / ".FAILED.tmp-orphan").mkdir(parents=True)
            copytree = shutil.copytree

            def copy_with_jcl_failure(src, dst, *args, **kwargs):
                if Path(src).name == "jcl":
                    raise OSError(errno.ENOMEM, "Cannot allocate memory")
                return copytree(src, dst, *args, **kwargs)

            with patch("cobol_rag_platform.pipeline.shutil.copytree", side_effect=copy_with_jcl_failure):
                pipeline._publish_program_artifacts(source)
            self.assertTrue((destination / "jcl").is_symlink())
            self.assertEqual(list((destination / "jcl").glob("**/jcl.summary.json"))[0].read_text(),
                             '{"job": "JOB1"}')
            self.assertTrue((destination / "jcl.file_io.json").is_file())
            registry = json.loads((pipeline.corpus_final_scripts / "corpus.registry.json").read_text())
            self.assertEqual([row["program"] for row in registry["programs"]], [program.name])

    def test_failed_publication_preserves_previous_artifacts(self) -> None:
        platform = load_platform(ROOT / "config" / "platform.toml")
        program = load_program(ROOT / "programs" / "PDCBVC" / "program.toml", platform)
        with tempfile.TemporaryDirectory() as temp:
            pipeline = Pipeline(platform, program, Path(temp) / "runs")
            source = pipeline.analysis_output / "artifacts"
            source.mkdir(parents=True)
            destination = pipeline.corpus_final_scripts / program.name
            destination.mkdir(parents=True)
            (destination / "old.json").write_text("{}")
            replace_path = Path.replace

            def fail_staging_rename(path, target):
                if ".tmp-" in path.name:
                    raise OSError(errno.ENOMEM, "Cannot allocate memory")
                return replace_path(path, target)

            with patch.object(Path, "replace", fail_staging_rename), self.assertRaises(OSError):
                pipeline._publish_program_artifacts(source)
            self.assertTrue((destination / "old.json").is_file())
            self.assertEqual(list(pipeline.corpus_final_scripts.iterdir()), [destination])

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
                    "resources": [
                        {"resource_type": "MAP", "resource": "SCREEN1"},
                        {"resource_type": "MAPSET", "resource": "SCRNSET"},
                        {"resource_type": "QUEUE", "resource": "TS-QUEUE"},
                    ],
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
            self.assertIn(("queue", "TS-QUEUE"), typed)
            self.assertNotIn(("map", "SCRNSET"), typed)


if __name__ == "__main__":
    unittest.main()
