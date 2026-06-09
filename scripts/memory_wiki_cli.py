#!/usr/bin/env python3
"""Standalone CLI for Hermes memory-wiki plugin."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"


def load_provider(home: str | None = None):
    spec = importlib.util.spec_from_file_location("memory_wiki_plugin", PLUGIN)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load plugin from {PLUGIN}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    if home:
        os.environ["HERMES_HOME"] = str(Path(home).expanduser())
    provider = mod.MemoryWikiProvider()
    provider.initialize("cli")
    return provider


def print_json(obj: Any, *, compact: bool = False) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=None if compact else 2))


def call(provider, name: str, args: Dict[str, Any], *, compact: bool = False) -> int:
    raw = provider.handle_tool_call(name, args)
    try:
        obj = json.loads(raw)
    except Exception:
        print(raw)
        return 1
    print_json(obj, compact=compact)
    return 0 if obj.get("success", True) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="memory-wiki CLI")
    parser.add_argument("--home", default=os.environ.get("HERMES_HOME", "~/.hermes"))
    parser.add_argument("--compact", action="store_true", help="print single-line JSON")
    sub = parser.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("query")
    q.add_argument("query")
    q.add_argument("--limit", type=int, default=10)

    s = sub.add_parser("secrets")
    s.add_argument("query")
    s.add_argument("--reveal", action="store_true")
    s.add_argument("--limit", type=int, default=10)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--repair", action="store_true")

    repair = sub.add_parser("repair")
    repair.add_argument("--target", choices=["fts", "dashboards", "integrity", "all"], default="all")
    repair.add_argument("--apply", action="store_true", help="apply repair; default is dry-run")

    audit = sub.add_parser("audit-log")
    audit.add_argument("--limit", type=int, default=50)

    export = sub.add_parser("export")
    export.add_argument("--limit", type=int, default=200)

    b = sub.add_parser("backup")
    b.add_argument("--reason", default="cli")

    lb = sub.add_parser("list-backups")
    lb.add_argument("--limit", type=int, default=20)

    r = sub.add_parser("restore")
    r.add_argument("backup")
    r.add_argument("--yes", action="store_true", help="confirm destructive restore")

    sub.add_parser("dashboard")

    snap = sub.add_parser("snapshot")
    snap.add_argument("--name", default="")

    pc = sub.add_parser("pack")
    pc.add_argument("query")
    pc.add_argument("--max-chars", type=int, default=3800)

    sub.add_parser("schemas")
    return parser


def main(argv: list[str] | None = None) -> int:
    # argparse global options normally must appear before the subcommand.
    # Accept --compact anywhere because humans often append it at the end.
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    compact_anywhere = "--compact" in raw_argv
    if compact_anywhere:
        raw_argv = [x for x in raw_argv if x != "--compact"]

    parser = build_parser()
    args = parser.parse_args(raw_argv)
    provider = load_provider(args.home)
    compact = bool(args.compact or compact_anywhere)

    if args.cmd == "query":
        return call(provider, "memory_wiki_query", {"query": args.query, "limit": args.limit}, compact=compact)
    if args.cmd == "secrets":
        return call(
            provider,
            "memory_wiki_query_secrets",
            {"query": args.query, "limit": args.limit, "reveal": args.reveal},
            compact=compact,
        )
    if args.cmd == "doctor":
        return call(provider, "memory_wiki_doctor", {"repair": args.repair}, compact=compact)
    if args.cmd == "repair":
        return call(provider, "memory_wiki_repair", {"target": args.target, "dry_run": not args.apply}, compact=compact)
    if args.cmd == "audit-log":
        return call(provider, "memory_wiki_audit_log", {"limit": args.limit}, compact=compact)
    if args.cmd == "export":
        return call(provider, "memory_wiki_export", {"limit": args.limit}, compact=compact)
    if args.cmd == "backup":
        return call(provider, "memory_wiki_backup", {"reason": args.reason}, compact=compact)
    if args.cmd == "list-backups":
        return call(provider, "memory_wiki_list_backups", {"limit": args.limit}, compact=compact)
    if args.cmd == "restore":
        if not args.yes:
            print("Refusing restore without --yes", file=sys.stderr)
            return 2
        return call(provider, "memory_wiki_restore", {"backup": args.backup}, compact=compact)
    if args.cmd == "dashboard":
        return call(provider, "memory_wiki_active_dashboard", {}, compact=compact)
    if args.cmd == "snapshot":
        return call(provider, "memory_wiki_snapshot", {"name": args.name}, compact=compact)
    if args.cmd == "pack":
        return call(provider, "memory_wiki_pack_context", {"query": args.query, "max_chars": args.max_chars}, compact=compact)
    if args.cmd == "schemas":
        print_json(provider.get_tool_schemas(), compact=compact)
        return 0

    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
