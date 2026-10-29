import json
import tempfile
import unittest
from pathlib import Path

from cobol_rag_platform.corpus_relationships import build_relationships, publish_relationships


class CorpusRelationshipTests(unittest.TestCase):
    def test_directed_calls_jcl_and_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a = root / "A"
            b = root / "B"
            a.mkdir()
            b.mkdir()
            (a / "architecture.call_parameters.json").write_text(json.dumps({"calls": [
                {"caller": "A", "target": "B", "call_type": "CICSLINK", "line_start": 42,
                 "parameters": ["AREA"], "commarea": "AREA"},
                {"caller": "A", "target": "EXTERNAL", "call_type": "CALL", "line_start": 43}]}))
            (a / "architecture.copybooks.json").write_text(json.dumps({"content": {"inclusions": [
                {"copybook": "COPYA", "line": 10}]}}))
            (a / "jcl.file_io.json").write_text(json.dumps({"content": {
                "known_jobs": ["JOB1", "UNLINKED"],
                "matching_steps": [{"job": "JOB1", "step": "RUN", "program": "A", "source": "JOB1.JCL"}],
                "reads": [{"job": "JOB1", "step": "RUN", "ddname": "IN", "dsn": "DATA.IN",
                           "access_type": "read"}],
                "unknowns": [{"job": "JOB1", "step": "RUN", "ddname": "MAYBE", "dsn": "DATA.MAYBE",
                              "access_type": "unknown"}]}}))
            graph = build_relationships(root)
            calls = [e for e in graph["edges"] if e["kind"] == "program_call"]
            self.assertEqual({(e["source"], e["target"]) for e in calls},
                             {("A", "B"), ("A", "EXTERNAL")})
            self.assertTrue(next(e for e in calls if e["target"] == "B")["evidence"]["target_analyzed"])
            self.assertFalse(next(e for e in calls if e["target"] == "EXTERNAL")["evidence"]["target_analyzed"])
            self.assertIn(("jcl_exec", "JOB1/RUN", "A"),
                          {(e["kind"], e["source"], e["target"]) for e in graph["edges"]})
            self.assertIn(("jcl_unknown", "JOB1/RUN", "DATA.MAYBE"),
                          {(e["kind"], e["source"], e["target"]) for e in graph["edges"]})
            self.assertEqual(graph["coverage"]["B"]["architecture.call_parameters.json"], "missing")
            self.assertEqual(graph["jobs_without_analyzed_program_link"], ["UNLINKED"])
            path = publish_relationships(root)
            self.assertEqual(json.loads(path.read_text())["counts"]["edges"], len(graph["edges"]))


if __name__ == "__main__":
    unittest.main()
