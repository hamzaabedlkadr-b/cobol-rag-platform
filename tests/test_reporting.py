import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from scripts import rag_machine
from cobol_rag_platform.reporting import RunReport


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = RunReport(self.root / "reports", "test")

    def test_missing_empty_invalid_and_mismatched_inputs(self):
        programs, copies = self.root / "programs", self.root / "copies"
        (programs / "A").mkdir(parents=True)
        (programs / "EMPTY").mkdir()
        copies.mkdir()
        (programs / "A/A.CBL").write_text("PROGRAM-ID. WRONG.\nCOPY B.\nCOPY MISSING.")
        (programs / "A/A_result.txt").write_text("")
        (programs / "A/A_controlflow.json").write_text("{invalid")
        (copies / "B.CPY").write_text("COPY C.")
        (copies / "C.CPY").write_text("COPY B.")
        self.report.input_tree(programs, copies)
        details = "\n".join(item["detail"] for item in self.report.data["findings"])
        for text in ("differs", "Invalid JSON", "empty", "Incomplete", "COPY MISSING"):
            self.assertIn(text, details)
        self.assertEqual(len(self.report.data["dependencies"]), 4)
        self.assertTrue(any(row.get("sha256") for row in self.report.data["files"]))

    def test_duplicate_library_and_comment_copy(self):
        copies = self.root / "copies"
        (copies / "nested").mkdir(parents=True)
        (copies / "B.CPY").write_text("01 B PIC X.")
        (copies / "nested/B.cpy").write_text("01 OTHER PIC X.")
        members = self.report.library(copies, "copybook")
        source, mapa, cfg = [self.root / name for name in ("A.CBL", "A.txt", "A.json")]
        source.write_text("       PROGRAM-ID. A.\n      *COPY IGNORED.\n           COPY B.\n*> COPY IGNORE2.")
        mapa.write_text("mapa")
        cfg.write_text("{}")
        self.report.program("A", source, mapa, cfg, members)
        self.assertEqual([item["member"] for item in self.report.data["dependencies"]], ["B"])
        self.assertEqual(self.report.data["dependencies"][0]["status"], "ambiguous")

    def test_command_log_and_failure_report(self):
        import subprocess
        with self.assertRaises(subprocess.CalledProcessError):
            self.report.command([sys.executable, "-c", "print('diagnostic'); raise SystemExit(3)"], cwd=self.root)
        self.report.finish("failed", RuntimeError("stage failed"))
        self.assertIn("diagnostic", (self.report.directory / "command-001.log").read_text())
        data = json.loads((self.report.directory / "report.json").read_text())
        self.assertEqual(data["commands"][0]["exit_code"], 3)
        self.assertEqual(data["status"], "failed")
        self.assertIn("stage failed", (self.report.directory / "report.md").read_text())

    def test_analysis_diagnostics_and_unique_runs(self):
        output = self.root / "analysis"
        output.mkdir()
        (output / "jcl.summary.json").write_text('{"warnings": ["Unresolved procedure"]}')
        self.report.collect_analysis(output)
        self.assertIn("Unresolved procedure", self.report.markdown())
        other = RunReport(self.root / "reports", "test")
        self.assertNotEqual(self.report.directory, other.directory)

    def test_html_escapes_untrusted_input(self):
        self.report.issue("warning", "<script>alert(1)</script>", "<img src=x onerror=alert(1)>")
        output = self.report.html()
        self.assertNotIn("<script>", output)
        self.assertIn("&lt;script&gt;", output)
        self.assertNotIn("<img", output)

    def test_supported_dot_is_not_reported_as_invalid_json(self):
        cfg = self.root / "A_controlflow.json"
        cfg.write_text("digraph A { <A>-><B>; }")
        self.report.inspect_file(cfg, "controlflow")
        self.assertFalse(any(row["severity"] == "error" for row in self.report.data["findings"]))
        self.assertIn("DOT", self.report.data["files"][0]["format"])

    @unittest.skipIf(sys.version_info < (3, 11), "Pipeline requires Python 3.11+")
    def test_invalid_configuration_produces_bootstrap_report(self):
        from cobol_rag_platform.cli import main
        result = main(["--config", str(self.root / "absent.toml"), "--runs-dir", str(self.root / "runs"), "run", "A"])
        self.assertEqual(result, 1)
        self.assertEqual(len(list((self.root / "runs/reports").glob("*/report.json"))), 1)

    def test_setup_failure_still_writes_report(self):
        args = SimpleNamespace(dry_run=False, programs_dir=self.root / "missing", copybooks_dir=self.root / "copies",
                               jcl_dir=None, llm_model="test", embedding_model="test")
        with patch.object(rag_machine, "ROOT", self.root), patch.object(rag_machine, "_setup", side_effect=RuntimeError("broken")):
            with self.assertRaisesRegex(RuntimeError, "broken"):
                rag_machine.setup(args)
        reports = list((self.root / ".runs/reports").glob("*/report.json"))
        self.assertEqual(len(reports), 1)
        self.assertEqual(json.loads(reports[0].read_text())["status"], "failed")
        self.assertIsNone(rag_machine._ACTIVE_REPORT)

    def test_dry_run_does_not_write_report(self):
        with patch.object(rag_machine, "ROOT", self.root), patch.object(rag_machine, "_setup") as setup:
            rag_machine.setup(SimpleNamespace(dry_run=True))
            setup.assert_called_once()
        self.assertFalse((self.root / ".runs").exists())

    def test_start_failure_is_recorded(self):
        with patch.object(rag_machine, "ROOT", self.root), patch.object(rag_machine, "_start", side_effect=RuntimeError("Docker unavailable")):
            with self.assertRaisesRegex(RuntimeError, "Docker unavailable"):
                rag_machine.start(SimpleNamespace())
        report = next((self.root / ".runs/reports").glob("*/report.json"))
        self.assertEqual(json.loads(report.read_text())["status"], "failed")

    @unittest.skipIf(sys.version_info < (3, 11), "Pipeline requires Python 3.11+")
    def test_pipeline_failure_keeps_completed_stage_and_audit(self):
        from cobol_rag_platform.pipeline import Pipeline, StageResult
        config = SimpleNamespace(rag=SimpleNamespace(llm_model="test", embedding_model="test"))
        program = SimpleNamespace(name="A", source=self.root / "program.toml", cobol_source=self.root / "A.CBL",
                                  mapa=self.root / "A.txt", controlflow=self.root / "A.json", copybooks=self.root / "copies", jcl=None)
        pipeline = Pipeline(config, program, self.root / "runs")
        with patch.object(pipeline, "stage_prepare", return_value=StageResult("prepare", "cached", "test")), \
             patch.object(pipeline, "stage_rekt", side_effect=RuntimeError("analyzer failed")):
            with self.assertRaisesRegex(RuntimeError, "analyzer failed"):
                pipeline.run()
        report = next((pipeline.run_dir / "reports").glob("*/report.json"))
        data = json.loads(report.read_text())
        self.assertEqual([s["status"] for s in data["stages"]], ["cached", "failed"])
        self.assertEqual(data["status"], "failed")
        self.assertTrue(data["findings"])

    @unittest.skipIf(sys.version_info < (3, 11), "Pipeline requires Python 3.11+")
    def test_pipeline_cached_run_and_audit_failure_do_not_change_outcome(self):
        from cobol_rag_platform.pipeline import Pipeline, StageResult
        config = SimpleNamespace(rag=SimpleNamespace(llm_model="test", embedding_model="test"))
        program = SimpleNamespace(name="A", source=self.root / "program.toml", cobol_source=self.root / "A.CBL",
                                  mapa=self.root / "A.txt", controlflow=self.root / "A.json", copybooks=self.root / "copies", jcl=None)
        pipeline = Pipeline(config, program, self.root / "runs")
        with patch.object(pipeline, "stage_prepare", return_value=StageResult("prepare", "cached", "test")), \
             patch.object(RunReport, "library", side_effect=PermissionError("cannot inspect")):
            result = pipeline.run(stop_after="prepare")
        self.assertEqual(result[0].status, "cached")
        report = next((pipeline.run_dir / "reports").glob("*/report.json"))
        self.assertEqual(json.loads(report.read_text())["status"], "completed")


if __name__ == "__main__":
    unittest.main()
