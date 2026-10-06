"""Read-only input audits and human-readable run reports (standard library only)."""
from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def cobol_text(text):
    """Best-effort COPY scanning, not a compiler or COPY REPLACING expansion."""
    lines = []
    for line in text.splitlines():
        fixed = len(line) > 6 and (line[:6].isspace() or line[:6].isdigit()) and line[6] in " */-Dd"
        if fixed and line[6] in "*/":
            continue
        lines.append((line[7:72] if fixed else line).split("*>", 1)[0])
    return "\n".join(lines)


class RunReport:
    def __init__(self, directory, kind, metadata=None):
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        self.directory = Path(directory) / run_id
        self.data = dict(schema_version=1, run_id=run_id, kind=kind, started_at=now(),
                         status="running", metadata=metadata or {}, files=[], findings=[],
                         dependencies=[], stages=[], artifacts=[], commands=[])

    def issue(self, severity, subject, detail):
        self.data["findings"].append(dict(severity=severity, subject=str(subject), detail=str(detail)))

    def inspect_file(self, path, role):
        path = Path(path)
        row = dict(path=str(path), role=role, status="missing")
        self.data["files"].append(row)
        try:
            if not path.is_file():
                self.issue("error", path, "Required file is missing or is not a regular file.")
                return ""
            stat = path.stat()
            row.update(bytes=stat.st_size, modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat())
            raw = path.read_bytes()
            row["sha256"] = hashlib.sha256(raw).hexdigest()
            row["status"] = "present" if raw.strip() else "empty"
            if not raw.strip():
                self.issue("error", path, "File is empty or contains only whitespace.")
            text = raw.decode("utf-8-sig", errors="replace")
            if "\ufffd" in text or b"\0" in raw:
                self.issue("warning", path, "Not clean UTF-8 text (replacement characters or NUL bytes). Check source encoding; no conversion was performed.")
            if role == "controlflow":
                try:
                    value = json.loads(text)
                    if not isinstance(value, (dict, list)) or not value:
                        self.issue("warning", path, "Control-flow JSON is empty or not an object/list.")
                except ValueError as error:
                    if re.search(r"\bdigraph\s+[A-Za-z0-9_]+\s*\{", text, re.I):
                        row["format"] = "DOT (detected, syntax not validated)"
                        self.issue("info", path, "Graphviz DOT detected instead of JSON. The analyzer supports DOT conversion; full syntax is checked during analysis.")
                    else:
                        row["status"] = "invalid"
                        self.issue("error", path, f"Invalid JSON and no supported DOT header detected: {error}")
            return text
        except OSError as error:
            row["status"] = "unreadable"
            self.issue("error", path, error)
            return ""

    def library(self, path, role):
        if path is None:
            self.issue("info", role, "Not supplied (optional).")
            return {}
        path = Path(path)
        if not path.is_dir():
            self.issue("error", path, f"{role} directory is missing.")
            return {}
        members = {}
        names = defaultdict(list)
        for item in sorted(path.rglob("*")):
            if not item.is_file() or any(part.startswith(".") for part in item.relative_to(path).parts):
                continue
            text = self.inspect_file(item, role)
            members[item] = text
            names[item.stem.upper()].append(item)
            if role == "jcl" and item.suffix.lower() not in {".jcl", ".txt", ".proc", ".prc"}:
                self.issue("warning", item, "Extension is not imported by the JCL setup helper.")
        if not members:
            self.issue("warning", path, f"No usable {role} files found.")
        for name, paths in names.items():
            if len(paths) > 1:
                self.issue("warning", name, "Ambiguous library member name: " + ", ".join(map(str, paths)))
        return members

    def program(self, name, source, mapa, controlflow, copybooks):
        text = self.inspect_file(source, "cobol")
        self.inspect_file(mapa, "mapa")
        self.inspect_file(controlflow, "controlflow")
        match = re.search(r"\bPROGRAM-ID\s*\.\s*['\"]?([\w@#$-]+)", cobol_text(text), re.I)
        if not match:
            self.issue("warning", source, "PROGRAM-ID was not recognized by the input check.")
        elif match.group(1).upper() != name.upper():
            self.issue("warning", source, f"PROGRAM-ID {match.group(1)} differs from folder/manifest name {name}.")
        index = defaultdict(list)
        for path in copybooks:
            index[path.stem.upper()].append(path)
        pending, visited = [(Path(source), text)], set()
        while pending:
            owner, body = pending.pop()
            if owner in visited:
                continue
            visited.add(owner)
            for target in sorted(set(re.findall(r"\bCOPY\s+['\"]?([\w@#$-]+)", cobol_text(body), re.I))):
                candidates = index.get(target.upper(), [])
                status = "found" if len(candidates) == 1 else "missing" if not candidates else "ambiguous"
                self.data["dependencies"].append(dict(program=name, source=str(owner), member=target,
                                                     status=status, candidates=list(map(str, candidates))))
                if status != "found":
                    self.issue("warning", owner, f"COPY {target}: {status} in supplied library. System/compiler libraries are not checked.")
                else:
                    pending.append((candidates[0], copybooks[candidates[0]]))

    def input_tree(self, root, copybooks, jcl=None):
        members = self.library(copybooks, "copybook")
        self.library(jcl, "jcl")
        root = Path(root)
        if not root.is_dir():
            self.issue("error", root, "Program input directory is missing.")
            return
        folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
        if not folders:
            self.issue("error", root, "No program subfolders found.")
        seen = set()
        for folder in folders:
            name = folder.name.upper()
            if name in seen or not re.fullmatch(r"[A-Z][A-Z0-9-]*", name):
                self.issue("error", folder, "Duplicate or invalid program directory name; setup skips this folder.")
            seen.add(name)
            files = defaultdict(list)
            for path in folder.iterdir():
                if path.is_file():
                    files[path.name.upper()].append(path)
            expected = [f"{name}.CBL", f"{name}_RESULT.TXT", f"{name}_CONTROLFLOW.JSON"]
            chosen = []
            for filename in expected:
                candidates = sorted(files.get(filename, []))
                if len(candidates) > 1:
                    self.issue("error", folder, f"Ambiguous filename: {filename}")
                chosen.append(candidates[0] if candidates else folder / filename)
            if any(not path.is_file() for path in chosen):
                self.issue("error", folder, "Incomplete input bundle; setup skips this program.")
            self.program(name, *chosen, members)
            for filename, paths in files.items():
                if filename not in expected and not filename.startswith("."):
                    for path in paths:
                        self.inspect_file(path, "extra/not imported")
                        self.issue("info", path, "Not one of the three expected program input files.")

    def collect_analysis(self, root):
        """Inventory diagnostic outputs without treating old artifacts as fresh results."""
        root = Path(root)
        for path in sorted(root.rglob("*.json")) if root.exists() else []:
            if not any(token in path.name for token in ("validation", "reconciliation", "jcl.summary", "manifest",
                                                       "program.summary", "quality.dead_code", "architecture.unused_copybooks", "jcl.file_io")):
                continue
            stat = path.stat()
            row = dict(path=str(path), bytes=stat.st_size,
                       modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat())
            self.data["artifacts"].append(row)
            if stat.st_size > 5_000_000:
                row["note"] = "Large diagnostic: open original file for details."
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                row["diagnostics"] = payload
                def notices(value, location=""):
                    if isinstance(value, dict):
                        for key, child in value.items():
                            if key.lower() in {"warnings", "errors", "issues", "discrepancies"} and child:
                                detail = child if isinstance(child, str) else json.dumps(child, ensure_ascii=False)
                                self.issue("warning", path, f"Analyzer diagnostic {location}/{key}: {detail[:2000]} (see full artifact below; may be cached)")
                            else:
                                notices(child, location + "/" + key)
                    elif isinstance(value, list):
                        for index, child in enumerate(value):
                            notices(child, location + f"/{index}")
                notices(payload)
            except (OSError, ValueError) as error:
                self.issue("warning", path, f"Cannot read analysis diagnostic: {error}")

    def attempt(self, method, *args):
        try:
            return method(*args)
        except Exception as error:
            self.issue("warning", "report audit", f"Audit incomplete: {type(error).__name__}: {error}")

    def command(self, command, *, cwd, env=None):
        self.directory.mkdir(parents=True, exist_ok=True)
        log = self.directory / f"command-{len(self.data['commands']) + 1:03}.log"
        entry = dict(command=list(command), cwd=str(cwd), started_at=now(), log=str(log))
        self.data["commands"].append(entry)
        try:
            with log.open("w", encoding="utf-8") as handle:
                with subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace") as process:
                    for line in process.stdout:
                        handle.write(line)
                        handle.flush()
                        print(line, end="", flush=True)
                    code = process.wait()
                    entry["exit_code"] = code
            if code:
                raise subprocess.CalledProcessError(code, command)
        except BaseException as error:
            entry["error"] = str(error)
            raise
        finally:
            entry["finished_at"] = now()

    def finish(self, status, error=None):
        self.data.update(status=status, finished_at=now())
        if error is not None:
            self.issue("error", "execution", f"{type(error).__name__}: {error}")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.data["finding_counts"] = dict(Counter(item["severity"] for item in self.data["findings"]))
            (self.directory / "report.json").write_text(json.dumps(self.data, indent=2, default=str) + "\n", encoding="utf-8")
            (self.directory / "report.md").write_text(self.markdown(), encoding="utf-8")
            (self.directory / "report.html").write_text(self.html(), encoding="utf-8")
            print(f"\nRun report: {self.directory / 'report.md'}", flush=True)
            print(f"Browser report: {self.directory / 'report.html'}", flush=True)
        except OSError as failure:
            print(f"Warning: could not save run report: {failure}", file=sys.stderr)

    def markdown(self):
        data = self.data
        lines = [f"# COBOL RAG — {data['kind']} report", "", f"Run: `{data['run_id']}`",
                 f"Started (UTC): {data['started_at']}", f"Finished (UTC): {data.get('finished_at', 'in progress')}",
                 f"Execution status: **{data['status']}**", "",
                 "Execution success does not mean all input or analysis checks passed. This is a static input audit, not a COBOL/JCL compiler.",
                 "COPY checks are best-effort (including nested COPY); system libraries, SQL INCLUDE and COPY REPLACING semantics are not validated.",
                 "Analysis artifacts below are files found on disk, possibly cached or from earlier runs; check timestamps and stage results.",
                 "Reports/logs can contain internal paths, dataset names and tool output. Review before sharing.", "",
                 "## Configuration", "", *[f"- {key}: `{value}`" for key, value in data['metadata'].items()], "",
                 "## Findings and required attention", ""]
        for item in data["findings"]:
            lines.append(f"- **{item['severity'].upper()}** — `{item['subject']}`: {item['detail']}")
        if not data["findings"]:
            lines.append("No issues detected by the available checks; this is not proof of complete analysis.")
        def cell(value):
            return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

        for title, key, columns in (
            ("Pipeline stages", "stages", ("name", "status", "started_at", "finished_at", "note", "error")),
            ("Commands and log files", "commands", ("command", "exit_code", "started_at", "finished_at", "log", "error")),
            ("Input file inventory", "files", ("role", "path", "status", "bytes", "modified_at", "sha256")),
            ("COPY dependency checks", "dependencies", ("program", "source", "member", "status", "candidates")),
        ):
            lines += ["", f"## {title}", ""]
            if not data[key]:
                lines.append("None recorded / not reached.")
                continue
            lines += ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
            for item in data[key]:
                lines.append("| " + " | ".join(cell(item.get(column, "")) for column in columns) + " |")
        lines += ["", "## Analysis diagnostics", ""]
        if not data["artifacts"]:
            lines.append("No diagnostic artifacts found / analysis not reached.")
        for item in data["artifacts"]:
            lines += [f"### {item['path']}", "", f"Modified (UTC): {item['modified_at']}", "",
                      "```json", json.dumps(item.get("diagnostics", item.get("note", "")), indent=2, default=str), "```", ""]
        return "\n".join(lines) + "\n"

    def html(self):
        escape = lambda value: html.escape(str(value), quote=True)
        data = self.data
        body = ["<!doctype html><html lang='en'><meta charset='utf-8'>",
                "<meta name='viewport' content='width=device-width, initial-scale=1'>",
                "<title>COBOL RAG run report</title><style>",
                "body{font:16px/1.5 system-ui,sans-serif;margin:32px;color:#172b3a;background:#fafbfc}"
                "h1,h2{color:#123d58}table{border-collapse:collapse;width:100%;font-size:14px}"
                "td,th{border:1px solid #ccd5dc;padding:8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}"
                "th{background:#eaf0f5}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef2f5;padding:16px}"
                ".scroll{overflow:auto}.error{color:#a21c1c}.warning{color:#825b00}details{margin:12px 0}"
                "summary{cursor:pointer;font-weight:600}.note{border-left:4px solid #2680a8;padding:12px;background:#eaf4fa}",
                "</style><h1>COBOL RAG — " + escape(data["kind"]) + "</h1>",
                "<p>Run: " + escape(data["run_id"]) + "<br>Started (UTC): " + escape(data["started_at"]) +
                "<br>Finished (UTC): " + escape(data.get("finished_at", "in progress")) +
                "<br>Execution: <strong>" + escape(data["status"]) + "</strong></p>",
                "<p class='note'>Execution success is not proof of complete analysis. COPY checks are heuristic; "
                "system libraries, SQL INCLUDE and COPY REPLACING are not validated. Analysis files may be cached "
                "or from earlier runs; check timestamps and stage outcomes. Reports contain internal paths and "
                "tool output: review before sharing.</p>"]

        def table(title, rows, columns):
            body.append("<h2>" + escape(title) + "</h2>")
            if not rows:
                body.append("<p>None recorded / not reached.</p>")
                return
            body.append("<div class='scroll'><table><thead><tr>" + "".join("<th>" + escape(c) + "</th>" for c in columns) + "</tr></thead><tbody>")
            for row in rows:
                body.append("<tr>" + "".join("<td>" + escape(row.get(c, "")) + "</td>" for c in columns) + "</tr>")
            body.append("</tbody></table></div>")

        body.append("<h2>Findings and required attention</h2><ul>")
        for finding in data["findings"]:
            severity = finding["severity"]
            body.append("<li class='" + escape(severity) + "'><strong>" + escape(severity.upper()) + " — " +
                        escape(finding["subject"]) + ":</strong> " + escape(finding["detail"]) + "</li>")
        body.append("</ul>")
        table("Configuration", [dict(setting=k, value=v) for k, v in data["metadata"].items()], ("setting", "value"))
        table("Pipeline stages", data["stages"], ("name", "status", "started_at", "finished_at", "note", "error"))
        table("Commands and logs", data["commands"], ("command", "exit_code", "log", "error"))
        table("Input inventory", data["files"], ("role", "path", "status", "bytes", "modified_at"))
        table("COPY dependencies", data["dependencies"], ("program", "source", "member", "status", "candidates"))
        body.append("<h2>Analysis diagnostics</h2>")
        if not data["artifacts"]:
            body.append("<p>No diagnostic artifacts found / analysis not reached.</p>")
        for item in data["artifacts"]:
            body.append("<details><summary>" + escape(item["path"]) + " — " + escape(item["modified_at"]) +
                        "</summary><pre>" + escape(json.dumps(item.get("diagnostics", item.get("note", "")), indent=2)) + "</pre></details>")
        body.append("<details><summary>Full file fingerprints</summary><pre>" + escape(json.dumps(data["files"], indent=2)) + "</pre></details></html>")
        return "\n".join(body)
