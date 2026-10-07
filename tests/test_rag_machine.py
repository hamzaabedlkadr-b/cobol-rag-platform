import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import rag_machine


class MachineSetupTests(unittest.TestCase):
    def test_discovery_skips_incomplete_programs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = root / "PDCBVC"
            good.mkdir()
            for name in ("PDCBVC.CBL", "PDCBVC_result.txt", "PDCBVC_controlflow.json"):
                (good / name).write_text("x")
            (root / "EMPTY").mkdir()
            found, skipped = rag_machine.discover_programs(root)
            self.assertEqual([item.name for item in found], ["PDCBVC"])
            self.assertIn("EMPTY", skipped[0])

    def test_import_preflight_does_not_partially_copy_on_manifest_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            platform_root = root / "cobol-rag-platform"
            platform_root.mkdir()
            manifest = platform_root / "programs" / "PDCBVC" / "program.toml"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("old manifest")
            source_root = root / "bundle"
            source_root.mkdir()
            source = source_root / "PDCBVC.CBL"
            mapa = source_root / "PDCBVC_result.txt"
            controlflow = source_root / "PDCBVC_controlflow.json"
            for file in (source, mapa, controlflow):
                file.write_text("input")
            copybooks = root / "copybooks"
            copybooks.mkdir()
            (copybooks / "X.CPY").write_text("copybook")
            program = rag_machine.ProgramInput("PDCBVC", source, mapa, controlflow)
            with patch.object(rag_machine, "ROOT", platform_root):
                with self.assertRaisesRegex(RuntimeError, "Different manifest"):
                    rag_machine.install_inputs([program], copybooks, replace=False, dry_run=False)
            self.assertFalse((root / "control_flow" / "input" / "PDCBVC").exists())

    def test_env_update_keeps_unrelated_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text("CUSTOM_SETTING=keep\nCOBOL_RAG_LLM_MODEL=old\n")
            with patch.object(rag_machine, "ROOT", root):
                rag_machine.update_env({"COBOL_RAG_LLM_MODEL": "new"}, dry_run=False)
            contents = (root / ".env").read_text()
            self.assertIn("CUSTOM_SETTING=keep", contents)
            self.assertIn("COBOL_RAG_LLM_MODEL=new", contents)

    def test_optional_jcl_import_and_existing_manifest_preservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            platform_root = root / "platform"
            source = root / "inputs"
            source.mkdir()
            files = [source / name for name in ("A.CBL", "A_result.txt", "A_controlflow.json")]
            for file in files:
                file.write_text("input")
            copybooks = root / "copybooks"
            copybooks.mkdir()
            (copybooks / "X.CPY").write_text("copybook")
            jobs = root / "jobs"
            (jobs / "nested").mkdir(parents=True)
            (jobs / "nested" / "JOB.JCL").write_text("//STEP EXEC PGM=A\n")
            program = rag_machine.ProgramInput("A", *files)
            with patch.object(rag_machine, "ROOT", platform_root):
                rag_machine.install_inputs([program], copybooks, replace=False, dry_run=False)
                manifest = platform_root / "programs" / "A" / "program.toml"
                self.assertNotIn("jcl =", manifest.read_text())
                original = manifest.read_text() + '# retain this comment\nrekt_bundle = "bundle"\n'
                manifest.write_text(original)
                rag_machine.install_inputs([program], copybooks, replace=False, dry_run=True, jcl_dir=jobs)
                self.assertEqual(manifest.read_text(), original)
                self.assertFalse((root / "control_flow/input/A/jcl").exists())
                rag_machine.install_inputs([program], copybooks, replace=False, dry_run=False, jcl_dir=jobs)
                updated = manifest.read_text()
                self.assertIn('jcl = "input/A/jcl"', updated)
                self.assertIn('# retain this comment\nrekt_bundle = "bundle"', updated)
                self.assertEqual((root / "control_flow/input/A/jcl/JOB.JCL").read_text(), "//STEP EXEC PGM=A\n")
                rag_machine.install_inputs([program], copybooks, replace=False, dry_run=False)
                self.assertEqual(manifest.read_text(), updated)

    def test_jcl_validation_and_path_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, "No JCL/procedure files"):
                rag_machine.jcl_files_in(root)
            (root / "JOB.JCL").write_text("job")
            (root / "nested").mkdir()
            (root / "nested/job.jcl").write_text("different job")
            with self.assertRaisesRegex(RuntimeError, "Duplicate JCL"):
                rag_machine.jcl_files_in(root)
        content = '[program]\nname = "A"\njcl = "custom/jobs"\n[other]\nvalue = 1\n'
        with self.assertRaisesRegex(RuntimeError, "different JCL"):
            rag_machine.add_jcl_setting(content, "input/A/jcl", replace=False)
        updated = rag_machine.add_jcl_setting(content, "input/A/jcl", replace=True)
        self.assertIn('jcl = "input/A/jcl"\n[other]\nvalue = 1', updated)

    def test_program_local_jcl_is_imported_only_for_its_program(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            platform_root = root / "platform"
            programs = []
            for name in ("A", "B"):
                source_dir = root / "inputs" / name
                source_dir.mkdir(parents=True)
                files = [source_dir / filename for filename in (
                    f"{name}.CBL", f"{name}_result.txt", f"{name}_controlflow.json"
                )]
                for file in files:
                    file.write_text("input")
                programs.append(rag_machine.ProgramInput(name, *files))
            jobs = root / "inputs" / "A" / "jcl"
            jobs.mkdir()
            (jobs / "JOB.JCL").write_text("//STEP EXEC PGM=A\n")
            copybooks = root / "copybooks"
            copybooks.mkdir()
            (copybooks / "X.CPY").write_text("copybook")
            with patch.object(rag_machine, "ROOT", platform_root):
                rag_machine.install_inputs(programs, copybooks, replace=False, dry_run=False)
            self.assertEqual((root / "control_flow/input/A/jcl/JOB.JCL").read_text(),
                             "//STEP EXEC PGM=A\n")
            self.assertFalse((root / "control_flow/input/B/jcl").exists())
            self.assertIn('jcl = "input/A/jcl"',
                          (platform_root / "programs/A/program.toml").read_text())
            self.assertNotIn('jcl =', (platform_root / "programs/B/program.toml").read_text())


if __name__ == "__main__":
    unittest.main()
