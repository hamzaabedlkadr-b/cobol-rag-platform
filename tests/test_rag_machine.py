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


if __name__ == "__main__":
    unittest.main()
