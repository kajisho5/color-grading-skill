"""color-grading CLI.

stdout: with --json exactly one JSON document (contract / doctor / response), on success and failure alike.
stderr: diagnostics only. Exit code: 0 on success, otherwise errors.EXIT_CODES[error.code].

  color-grading skill --json               contract (alias: contract --json)
  color-grading doctor --json              environment vs. contract
  color-grading validate - --json          validate a request document, run nothing
  color-grading plan - --json              dry run: plan, graph, tool selection, expected geometry; writes no media
  color-grading run - --json [--dry-run]"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from typing import Any, List, Optional

from . import PACKAGE_NAME, SKILL_ID, VERSION
from .contract import skill_contract
from .doctor import doctor_report, runtime_context
from .errors import EXIT_CODES, ColorError
from .executor import RESPONSE_SCHEMA_ID, Executor
from .security import PathPolicy


def _add_common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--json", action="store_true", help="machine-readable JSON on stdout (exactly one document)")


def _add_run_opts(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("request", help="path to a request document, or - for stdin")
    ap.add_argument("--workspace", help="directory writes are confined to (default: current directory)")
    ap.add_argument("--allowed-input", action="append", help="restrict the source to this root (repeatable)")
    ap.add_argument("--allowed-lut", action="append", help="restrict LUT files to this root (repeatable; default: same as --allowed-input)")
    ap.add_argument("--ffmpeg-skill", help="ffmpeg-skill checkout directory (default: env / ~/.claude/skills/ffmpeg-skill / ./vendor / ..)")
    ap.add_argument("--timeout", type=float, default=900.0, help="seconds per tool invocation (default 900)")
    ap.add_argument("--no-reuse", action="store_true", help="do not reuse intermediates with a matching operation id")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="color-grading", description=f"{PACKAGE_NAME} {VERSION}: deterministic colour grading execution (not an AI agent)")
    ap.add_argument("--version", action="version", version=f"{PACKAGE_NAME} {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("skill", "contract"):
        c = sub.add_parser(name, help="print the Skill / Capability / Tool contract")
        _add_common(c)
    d = sub.add_parser("doctor", help="diagnose the environment against the contract")
    d.add_argument("--workspace")
    d.add_argument("--allowed-input", action="append")
    d.add_argument("--allowed-lut", action="append")
    d.add_argument("--ffmpeg-skill")
    _add_common(d)
    v = sub.add_parser("validate", help="validate a request document; runs nothing, reads no media")
    v.add_argument("request")
    _add_common(v)
    p = sub.add_parser("plan", help="dry run: plan without writing media (the source and any LUTs are read read-only)")
    _add_run_opts(p)
    _add_common(p)
    r = sub.add_parser("run", help="execute a request document")
    _add_run_opts(r)
    r.add_argument("--dry-run", action="store_true", help="same as plan")
    _add_common(r)
    return ap


def _read_document(spec: str) -> Any:
    try:
        text = sys.stdin.read() if spec == "-" else open(spec, "r", encoding="utf-8").read()
    except OSError as e:
        raise ColorError("INVALID_REQUEST", f"cannot read request document: {e}")
    if len(text) > 16 * 1024 * 1024:
        raise ColorError("INVALID_REQUEST", "request document is larger than 16 MiB")
    try:
        return json.loads(text)
    except ValueError as e:
        raise ColorError("INVALID_REQUEST", f"request document is not valid JSON: {e}")


def _error_document(e: ColorError, dry_run: bool) -> dict:
    return {"schema": RESPONSE_SCHEMA_ID, "skill": {"id": SKILL_ID, "version": VERSION}, "ok": False,
            "status": "cancelled" if e.code == "CANCELLED" else "error", "dry_run": dry_run, "error": e.to_dict(), "warnings": []}


def _emit(doc: dict, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    else:
        _human(doc)
    sys.stdout.flush()


def _human(doc: dict) -> None:
    if not doc.get("ok", True) and doc.get("error"):
        e = doc["error"]
        sys.stderr.write(f"error [{e['code']}]: {e['message']}" + (f" {json.dumps(e.get('details'))}" if e.get("details") else "") + "\n")
    if "results" in doc:
        for r in doc["results"]:
            if r["type"] == "SOURCE":
                continue
            art = r.get("artifact") or {}
            print(f"{r['node_id']:20} {r['type']:12} {r['status']:10} {r.get('tool') or '':20} dur={art.get('duration')} sha={str(art.get('sha256'))[:12]}")
        for o in doc.get("outputs", []):
            print(f"output {o['output_id']}: {o.get('status', 'planned')} {o.get('path')}")
    elif "validation" in doc:
        print("validation: ok")
    elif "checks" in doc:
        print(f"{PACKAGE_NAME} {VERSION}: {doc['status']}")
        for k in ("ffmpeg_skill", "ffmpeg", "ffprobe"):
            print(f"{k}: {json.dumps(doc['checks'].get(k))}")
        for t, o in doc["checks"]["operations"].items():
            print(f"  {t:12} {o['status']:12} {o['tool']}" + (f"  missing: {o['missing']}" if o["missing"] else ""))
        for p in doc["problems"]:
            print(f"problem: {p}")
    elif "operations" in doc:
        print(f"{doc['skill_id']} {doc['version']}: " + ", ".join(o["type"] for o in doc["operations"]))


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    if args.cmd in ("skill", "contract"):
        _emit(skill_contract(), as_json)
        return 0
    if args.cmd == "doctor":
        doc = doctor_report(args.ffmpeg_skill, args.workspace, args.allowed_input, args.allowed_lut)
        _emit(doc, as_json)
        return 0 if doc["status"] != "fail" else 1
    dry_run = args.cmd == "plan" or bool(getattr(args, "dry_run", False))
    try:
        document = _read_document(args.request)
        if args.cmd == "validate":
            from .graph import OperationGraph
            from .model import parse_request
            req = parse_request(document)
            graph = OperationGraph(req.project)
            doc = {"schema": RESPONSE_SCHEMA_ID, "skill": {"id": SKILL_ID, "version": VERSION}, "ok": True, "status": "ok", "dry_run": True,
                   "validation": {"ok": True, "graph": graph.to_dict()}, "warnings": []}
        else:
            policy = PathPolicy(args.workspace, args.allowed_input, args.allowed_lut)
            skill, versions, caps = runtime_context(args.ffmpeg_skill, args.timeout)
            executor = Executor(policy, skill, dry_run=dry_run, reuse=not args.no_reuse, timeout=args.timeout, tool_versions=versions, capabilities=caps)

            def _cancel(signum: int, frame: Any) -> None:
                skill.cancel()
                raise KeyboardInterrupt()

            for sig in [signal.SIGINT, signal.SIGTERM] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else []):  # type: ignore[attr-defined]
                try:
                    signal.signal(sig, _cancel)
                except (ValueError, OSError):
                    pass
            try:
                doc = executor.response(document)
            except KeyboardInterrupt:
                doc = _error_document(ColorError("CANCELLED", "interrupted", {"reason": "signal"}), dry_run)
    except ColorError as e:
        doc = _error_document(e, dry_run)
    _emit(doc, as_json)
    if doc.get("ok"):
        return 0
    return EXIT_CODES.get((doc.get("error") or {}).get("code", "INTERNAL_ERROR"), EXIT_CODES["INTERNAL_ERROR"])


if __name__ == "__main__":
    sys.exit(main())
