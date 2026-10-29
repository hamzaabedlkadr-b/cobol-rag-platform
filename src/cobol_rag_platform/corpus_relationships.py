"""Synthesize a provenance-preserving corpus graph from published program artifacts."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any


ARTIFACTS = (
    "architecture.call_parameters.json",
    "architecture.copybooks.json",
    "jcl.file_io.json",
)


def _read(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.is_file():
        return None, "missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "invalid"
    return (value, "available") if isinstance(value, dict) else (None, "invalid")


def _edge(kind: str, source: str, target: str, *, program: str, artifact: str,
          evidence: dict[str, Any]) -> dict[str, Any]:
    identity = json.dumps([kind, source, target, program, evidence], sort_keys=True, default=str)
    return {
        "id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
        "kind": kind, "source": source, "target": target, "program": program,
        "source_artifact": artifact, "evidence": evidence,
    }


def build_relationships(root: Path) -> dict[str, Any]:
    """Only explicit analyzed facts become edges; missing evidence stays visible."""
    program_dirs = {p.name.upper(): p for p in root.iterdir() if p.is_dir()}
    programs = sorted(program_dirs)
    known = set(programs)
    edges: list[dict[str, Any]] = []
    coverage: dict[str, dict[str, str]] = {}
    known_jobs: set[str] = set()
    linked_jobs: set[str] = set()
    for program in programs:
        folder = program_dirs[program]
        payloads = {name: _read(folder / name) for name in ARTIFACTS}
        coverage[program] = {name: status for name, (_, status) in payloads.items()}

        calls = payloads[ARTIFACTS[0]][0]
        for call in calls.get("calls", []) if calls else []:
            if not isinstance(call, dict) or not call.get("target"):
                continue
            caller = str(call.get("caller") or program).upper()
            target = str(call["target"]).upper()
            edges.append(_edge("program_call", caller, target, program=program, artifact=ARTIFACTS[0],
                evidence={"call_type": call.get("call_type"), "paragraph": call.get("paragraph"),
                          "line_start": call.get("line_start"), "statement": call.get("statement"),
                          "parameters": call.get("parameters", []), "commarea": call.get("commarea"),
                          "target_analyzed": target in known}))

        copybooks = payloads[ARTIFACTS[1]][0]
        for inclusion in (copybooks.get("content", {}).get("inclusions", []) if copybooks else []):
            if not isinstance(inclusion, dict) or not inclusion.get("copybook"):
                continue
            target = str(inclusion["copybook"]).upper()
            edges.append(_edge("copybook_include", program, target, program=program, artifact=ARTIFACTS[1],
                evidence={"line": inclusion.get("line"), "section": inclusion.get("section"),
                          "division": inclusion.get("division")}))

        jcl = payloads[ARTIFACTS[2]][0]
        content = jcl.get("content", {}) if jcl else {}
        known_jobs.update(str(job).upper() for job in content.get("known_jobs", []) if job)
        for step in content.get("matching_steps", []):
            if not isinstance(step, dict) or not step.get("job") or not step.get("step"):
                continue
            job_step = f"{step['job']}/{step['step']}".upper()
            linked_jobs.add(str(step["job"]).upper())
            edges.append(_edge("jcl_exec", job_step, program, program=program, artifact=ARTIFACTS[2],
                evidence={"job": step["job"], "step": step["step"], "source_file": step.get("source"),
                          "source_lines": step.get("source_lines"), "conditions": step.get("conditions", [])}))
        for field, kind in (("reads", "jcl_read"), ("writes", "jcl_write"),
                            ("sysout", "jcl_sysout"), ("unknowns", "jcl_unknown"),
                            ("deletes", "jcl_delete")):
            for dd in content.get(field, []):
                if not isinstance(dd, dict) or not dd.get("job") or not dd.get("step"):
                    continue
                destination = dd.get("dsn") or dd.get("output") or dd.get("sysout")
                if not destination:
                    continue
                job_step = f"{dd['job']}/{dd['step']}".upper()
                edges.append(_edge(kind, job_step, str(destination).upper(), program=program,
                    artifact=ARTIFACTS[2], evidence={"ddname": dd.get("ddname"), "dsn": dd.get("dsn"),
                        "access_type": dd.get("access_type"), "access_reason": dd.get("access_reason"),
                        "source_lines": dd.get("source_lines"), "citation": dd.get("citation")}))

    unique = {edge["id"]: edge for edge in edges}
    ordered = sorted(unique.values(), key=lambda e: (e["kind"], e["source"], e["target"], e["id"]))
    return {"schema_version": 1, "programs": programs, "coverage": coverage, "edges": ordered,
            "known_jcl_jobs": sorted(known_jobs), "jobs_linked_to_analyzed_programs": sorted(linked_jobs),
            "jobs_without_analyzed_program_link": sorted(known_jobs - linked_jobs),
            "counts": {"programs": len(programs), "edges": len(ordered),
                       "by_kind": {kind: sum(e["kind"] == kind for e in ordered)
                                   for kind in sorted({e["kind"] for e in ordered})}},
            "limitations": ["Only published analyzed programs are covered.",
                            "An unindexed call target is not proof that its source is absent elsewhere.",
                            "No CICS transaction definition is inferred from an uncalled program.",
                            "JCL DD access marked unknown is not reclassified as read or write."]}


def publish_relationships(root: Path) -> Path:
    result = build_relationships(root)
    target = root / "corpus.relationships.json"
    temporary = root / f"corpus.relationships.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
