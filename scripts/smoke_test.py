#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"
KEEP_HOME = os.environ.get("MEMORY_WIKI_KEEP_SMOKE_HOME") == "1"


def load_plugin():
    spec = importlib.util.spec_from_file_location("memory_wiki_plugin", PLUGIN)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load plugin from {PLUGIN}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def assert_success(data: Dict[str, Any], context: str = "") -> Dict[str, Any]:
    assert data.get("success", True), {"context": context, "data": data}
    return data


def assert_no_secret_leak(obj: Any, secret: str, context: str) -> None:
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    assert secret not in text, {"context": context, "error": "secret leaked"}


def extract_dispatch_tools(mod: Any) -> set[str]:
    source = inspect.getsource(mod.MemoryWikiProvider.handle_tool_call)
    return set(re.findall(r'tool_name\s*==\s*["\'](memory_wiki_[^"\']+)["\']', source))


def main() -> int:
    home = tempfile.mkdtemp(prefix="memorywiki_smoke_")
    try:
        os.environ["HERMES_HOME"] = home
        mod = load_plugin()
        provider = mod.MemoryWikiProvider()
        provider.initialize("smoke")

        def call_raw(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
            raw = provider.handle_tool_call(name, args)
            try:
                return json.loads(raw)
            except Exception as exc:
                raise AssertionError({"tool": name, "raw": raw}) from exc

        def call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
            return assert_success(call_raw(name, args), name)

        schema_defs = provider.get_tool_schemas()
        schemas = [x["name"] for x in schema_defs]
        assert len(schemas) == len(set(schemas)), {"duplicate_schemas": sorted({x for x in schemas if schemas.count(x) > 1})}
        dispatch_tools = extract_dispatch_tools(mod)
        assert set(schemas) == dispatch_tools, {
            "schemas_without_dispatch": sorted(set(schemas) - dispatch_tools),
            "dispatch_without_schema": sorted(dispatch_tools - set(schemas)),
        }
        required = [
            "memory_wiki_doctor",
            "memory_wiki_backup",
            "memory_wiki_restore",
            "memory_wiki_repair",
            "memory_wiki_audit_log",
            "memory_wiki_export",
            "memory_wiki_add_claim",
            "memory_wiki_add_evidence",
            "memory_wiki_update_claim",
            "memory_wiki_lint_claim",
            "memory_wiki_mark_used",
            "memory_wiki_why_believe",
            "memory_wiki_add_decision",
            "memory_wiki_add_mistake",
            "memory_wiki_add_project_profile",
            "memory_wiki_add_task_capsule",
            "memory_wiki_add_entity",
            "memory_wiki_add_relation",
            "memory_wiki_graph_query",
            "memory_wiki_pack_context",
            "memory_wiki_snapshot",
            "memory_wiki_review_queue",
            "memory_wiki_contradict",
            "memory_wiki_resolve_contradiction",
            "memory_wiki_merge_claims",
            "memory_wiki_pin_claim",
            "memory_wiki_write_firewall",
            "memory_wiki_mutation_log",
            "memory_wiki_undo_last",
            "memory_wiki_transaction",
            "memory_wiki_compile_topic",
            "memory_wiki_get_project_context",
            "memory_wiki_source_policy",
            "memory_wiki_export_bundle",
            "memory_wiki_import_bundle",
        ]
        missing = [name for name in required if name not in schemas]
        assert not missing, {"missing_schemas": missing, "schemas": schemas}

        secret_value = "dummy-secret-value-should-not-leak"
        secret_result = call(
            "memory_wiki_add_secret",
            {
                "subject": "Demo Server",
                "scope": "VPS SSH",
                "secret_type": "password",
                "locator": "ssh://demo.example.invalid/user",
                "value": secret_value,
                "purpose": "test",
            },
        )
        assert secret_result.get("id"), secret_result
        assert_no_secret_leak(secret_result, secret_value, "add_secret response")
        redacted_secrets = call("memory_wiki_query_secrets", {"query": "Demo Server", "limit": 5, "reveal": False})
        assert redacted_secrets.get("secrets"), redacted_secrets
        assert_no_secret_leak(redacted_secrets, secret_value, "redacted secret query")
        revealed_secrets = call("memory_wiki_query_secrets", {"query": "Demo Server", "limit": 5, "reveal": True})
        assert secret_value in json.dumps(revealed_secrets, ensure_ascii=False), revealed_secrets

        alpha = call(
            "memory_wiki_add_claim",
            {"claim": "Memory wiki smoke alpha claim stores searchable context", "topic": "Smoke Topic", "evidence": "alpha evidence"},
        )["id"]
        beta = call(
            "memory_wiki_add_claim",
            {"claim": "Memory wiki smoke beta claim stores searchable context", "topic": "Smoke Topic", "evidence": "beta evidence"},
        )["id"]
        query = call("memory_wiki_query", {"query": "smoke alpha searchable", "limit": 5, "include_stale": False})
        assert any(r.get("id") == alpha for r in query.get("claims", [])), query
        evidence = call(
            "memory_wiki_add_evidence",
            {"claim_id": alpha, "text": "fresh supporting evidence for smoke alpha", "kind": "support", "source": "smoke"},
        )
        assert evidence.get("id", "").startswith("e_"), evidence
        update = call(
            "memory_wiki_update_claim",
            {"claim_id": alpha, "salience": 0.91, "confidence": 0.88, "refresh": True},
        )
        assert update.get("updated"), update
        call("memory_wiki_mark_used", {"claim_ids": [alpha], "usefulness": 0.9, "query": "smoke alpha"})
        why = call("memory_wiki_why_believe", {"claim_id": alpha})
        assert why.get("claim") and why.get("evidence") is not None, why
        call("memory_wiki_pin_claim", {"claim_id": alpha, "pinned": True})
        merge = call(
            "memory_wiki_merge_claims",
            {"keep_id": alpha, "merge_ids": [beta], "resolution": "smoke duplicate merge", "loser_status": "superseded"},
        )
        assert beta in merge.get("merged", []), merge

        no_remote = call("memory_wiki_add_claim", {"claim": "Smoke service does not use remote storage", "topic": "Smoke Topic"})["id"]
        yes_remote = call("memory_wiki_add_claim", {"claim": "Smoke service use remote storage", "topic": "Smoke Topic"})["id"]
        contradiction = call(
            "memory_wiki_contradict",
            {"claim_a": no_remote, "claim_b": yes_remote, "reason": "manual smoke contradiction"},
        )["id"]
        resolved = call(
            "memory_wiki_resolve_contradiction",
            {"contradiction_id": contradiction, "resolution": "smoke prefers no_remote", "winner_claim_id": no_remote, "loser_status": "uncertain"},
        )
        assert resolved.get("resolved"), resolved

        lint = call("memory_wiki_lint_claim", {"claim": "ok", "topic": "misc"})
        assert "suggestions" in lint, lint
        review_before = call("memory_wiki_review_queue", {"mode": "list", "limit": 10})
        assert isinstance(review_before.get("items"), list), review_before

        call(
            "memory_wiki_add_decision",
            {"decision": "Use SQLite memory wiki", "rationale": "stdlib friendly", "alternatives": ["postgres"]},
        )
        call(
            "memory_wiki_add_mistake",
            {
                "trigger": "editing plugin",
                "mistake": "forget smoke tests",
                "fix": "run smoke_test.py",
                "prevention": "task capsule",
            },
        )
        call(
            "memory_wiki_add_project_profile",
            {
                "project_id": "memory-wiki",
                "root": str(PLUGIN.parent),
                "commands": ["python3 -m py_compile __init__.py"],
                "services": ["Hermes"],
            },
        )
        call(
            "memory_wiki_add_task_capsule",
            {
                "intent": "smoke v1",
                "plan": "exercise tools",
                "files": [str(PLUGIN)],
                "commands": ["smoke_test.py"],
                "verification": "ok",
            },
        )
        call("memory_wiki_add_entity", {"name": "Demo Server", "entity_type": "server", "aliases": ["agent server"]})
        call("memory_wiki_add_relation", {"subject": "Demo Server", "predicate": "hosts", "object": "Agent Runtime", "evidence": "smoke"})
        graph = call("memory_wiki_graph_query", {"query": "Demo", "limit": 10})
        assert graph.get("entities") or graph.get("relations"), graph
        packed = call("memory_wiki_pack_context", {"query": "Demo Agent secret", "max_chars": 2500})
        assert packed.get("context") is not None, packed
        assert_no_secret_leak(packed, secret_value, "packed context")

        firewall = call("memory_wiki_write_firewall", {"claim": "Memory wiki smoke firewall stores clean structured context", "topic": "Smoke Topic", "source": "tool", "mode": "apply"})
        assert firewall.get("claim_id") or firewall.get("review_id"), firewall
        policy = call("memory_wiki_source_policy", {"source": "session_end:smoke", "claim": "previous turn was interrupted and tool output leaked", "topic": "Smoke Topic"})
        assert policy.get("policy", {}).get("candidate_only") is True, policy
        mutation_log = call("memory_wiki_mutation_log", {"limit": 20})
        assert isinstance(mutation_log.get("events"), list), mutation_log
        undo_dry = call("memory_wiki_undo_last", {"dry_run": True})
        assert "found" in undo_dry, undo_dry
        tx = call("memory_wiki_transaction", {"mode": "suggest", "operations": [{"operation": "memory_wiki_compile_topic", "args": {"topic": "Smoke Topic", "limit": 10}}], "reason": "smoke dry-run"})
        assert tx.get("results"), tx
        compiled = call("memory_wiki_compile_topic", {"topic": "Smoke Topic", "mode": "apply", "limit": 20, "summary_type": "summary"})
        assert compiled.get("claim_id"), compiled
        project_ctx = call("memory_wiki_get_project_context", {"project_id": "memory-wiki", "query": "smoke", "limit": 10})
        assert project_ctx.get("profile"), project_ctx
        bundle = call("memory_wiki_export_bundle", {"topic": "Smoke Topic", "limit": 30, "write_file": False})
        assert bundle.get("payload", {}).get("claims") is not None, bundle
        assert_no_secret_leak(bundle, secret_value, "sync bundle")
        import_suggest = call("memory_wiki_import_bundle", {"payload": bundle["payload"], "mode": "suggest"})
        assert import_suggest.get("would_import"), import_suggest

        snapshot = call("memory_wiki_snapshot", {"name": "smoke"})
        assert Path(snapshot.get("path", "")).exists(), snapshot

        dashboard = call("memory_wiki_dashboard", {"limit": 10})
        assert "counts" in dashboard, dashboard
        page = call("memory_wiki_get_page", {"topic": "smoke-topic"})
        assert page.get("content"), page
        health = call("memory_wiki_health", {"limit": 20})
        assert "issues" in health, health
        recent = call("memory_wiki_recent_changes", {"since_seconds": 3600, "limit": 20})
        assert isinstance(recent.get("changes"), list), recent

        repair_dry = call("memory_wiki_repair", {"target": "all", "dry_run": True})
        assert repair_dry["dry_run"] is True, repair_dry
        repair_fts = call("memory_wiki_repair", {"target": "fts", "dry_run": False})
        assert any(a["action"] == "rebuild_claims_fts" and a["applied"] for a in repair_fts["actions"]), repair_fts

        backup = call("memory_wiki_backup", {"reason": "smoke"})
        call("memory_wiki_list_backups", {})
        doctor = call("memory_wiki_doctor", {"repair": True})
        assert doctor["ok"], doctor

        exported = call("memory_wiki_export", {"limit": 50})
        assert_no_secret_leak(exported, secret_value, "export")
        assert "audit" in exported, exported.keys()

        maintenance = call("memory_wiki_maintenance", {"prune_retired_days": 0})
        assert maintenance.get("checked", 0) > 0 and maintenance.get("dashboard"), maintenance
        exported_after_maintenance = call("memory_wiki_export", {"limit": 50})
        assert_no_secret_leak(exported_after_maintenance, secret_value, "export after maintenance")

        audit = call("memory_wiki_audit_log", {"limit": 20})
        assert isinstance(audit.get("events"), list), audit

        bad_zip = Path(home) / "memory-wiki" / "backups" / "bad.zip"
        bad_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(bad_zip, "w") as z:
            z.writestr("../evil.txt", "bad")
        bad_restore = call_raw("memory_wiki_restore", {"backup": str(bad_zip)})
        assert not bad_restore.get("success", True), bad_restore
        assert "unsafe zip member" in bad_restore.get("error", "").lower(), bad_restore
        assert not (Path(home) / "evil.txt").exists(), "zip slip wrote outside memory-wiki root"

        result = {
            "ok": True,
            "home": home if KEEP_HOME else "removed",
            "backup": backup.get("path"),
            "schemas": len(schemas),
            "audit_events": len(audit.get("events", [])),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        if not KEEP_HOME:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
