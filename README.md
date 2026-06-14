# hermes-memory-wiki

**hermes-memory-wiki** is a source-only Hermes Agent plugin that turns assistant memory into a local, inspectable **Memory OS**: a SQLite + Markdown wiki vault with ranked recall, evidence, provenance, redaction, curation, project profiles, graph relations, transactions, recovery tools and operational dashboards.

It is designed for long-running AI-agent installations where memory must be durable, searchable, auditable, rollbackable and safe to maintain over months of work.

> This public repository contains only plugin source, metadata and helper scripts. It intentionally excludes runtime databases, user memory, secrets, sessions, logs, snapshots, backups, vault files and other local artifacts.

## Current version

`plugin.yaml`: **1.3.0**

Version 1.3 turns memory-wiki into a stronger Memory OS layer:

- **Memory Diff Before Answer** via `memory_wiki_memory_diff`: compare recalled memory against verified/current facts before answering.
- **Write Firewall 2.0** via `memory_wiki_write_firewall`: source policy, quality linting, artifact detection and secret screening before durable writes.
- **Project Autobiography** via first-class `project_profiles` and `memory_wiki_get_project_context`.
- **Mutation Log + Undo** via `memory_wiki_mutation_log`, `memory_wiki_undo_last` and transactional batch operations.
- **Policy-based contradiction resolution** via `memory_wiki_resolve_by_policy`.
- **Recall unit tests** via `memory_wiki_evaluate_retrieval` golden queries.
- **Sectioned context packing** via `memory_wiki_pack_context`, now including memory-diff and preference-priority sections.
- **Graph Memory** via `memory_wiki_add_entity`, `memory_wiki_add_relation` and `memory_wiki_graph_query`.
- **Provenance cards** via `memory_wiki_why_believe`.
- **Preference Priority Layer** via `preference_rules`, `memory_wiki_preference_layer` and `memory_wiki_add_preference_rule`.

## What it stores

memory-wiki stores structured operational memory, including:

- claims/facts with confidence, salience, freshness, trust, quality and access counts;
- evidence and provenance for claims;
- decisions, mistakes/lessons and user corrections;
- project profiles and task capsules;
- typed entity graph relations;
- redacted secret index metadata;
- review queues, lint results and curation actions;
- preference-priority rules;
- audit logs, mutation logs and maintenance reports.

Runtime data is stored under:

```text
$HERMES_HOME/memory-wiki/
```

and is ignored by this repository.

## Feature overview

### Recall and context packing

- FTS5-backed search over active memory.
- Freshness/salience/confidence/trust ranking.
- Budget-aware `memory_wiki_pack_context` for injecting relevant context into agent turns.
- Sectioned context with preferences, procedures, projects, task outcomes, graph relations, contradictions, memory diff and source-policy metadata.
- Recall explanations for debugging why a claim was selected.
- Feedback loop via `memory_wiki_mark_used`.

### Memory quality control

- Candidate review queue.
- Claim linting and in-place rewrite.
- Topic normalization and topic alias cleanup.
- Deduplication, vacuum and merge tools.
- Contradiction recording, policy-based resolution and manual resolution.
- Synthetic topic compression and curated topic compilation.
- Golden-query retrieval evaluation.

### Safety and redaction

- Secret-like content detection.
- Secret index separate from normal claims.
- Redacted recall by default.
- Secret quarantine and scrub workflows.
- Source policy and write firewall before durable writes.
- Redacted export/import bundles for sync.
- Last-mile public row sanitizer for query/export/graph surfaces.

### Operations and recovery

- SQLite `doctor`, `health`, `repair`, FTS rebuild, metadata integrity and dashboard render checks.
- Full backup/list/restore helpers.
- Human-readable snapshots.
- Audit log and mutation log.
- Undo for reversible memory mutations.
- Transactional dry-run/apply/apply-with-backup batches.

## Repository layout

```text
.
├── __init__.py                     # Hermes MemoryProvider plugin implementation
├── plugin.yaml                     # Hermes plugin metadata
├── scripts/
│   ├── memory_wiki_cli.py          # Standalone local maintenance CLI
│   └── smoke_test.py               # End-to-end schema/dispatch/provider smoke tests
├── .gitignore                      # Blocks runtime DBs, secrets, backups and local artifacts
├── LICENSE
└── README.md
```

## Requirements

- Python 3.10+ recommended.
- SQLite with FTS5 enabled.
- Hermes Agent plugin runtime for normal in-agent usage.

The plugin itself uses the Python standard library only.

## Install in Hermes

Clone or copy this plugin into a Hermes profile:

```bash
mkdir -p ~/.hermes/plugins
# Example path after clone/copy:
# ~/.hermes/plugins/memory-wiki/
```

Enable it in `~/.hermes/config.yaml`:

```yaml
memory:
  provider: memory-wiki

plugins:
  enabled:
    - memory-wiki
```

Restart Hermes after editing plugin/config files so the plugin registry and provider instance reload.

## Verification

Run syntax checks:

```bash
python3 -m py_compile __init__.py scripts/smoke_test.py
```

Run the smoke suite with optional LLM refinement disabled:

```bash
MEMORY_WIKI_LLM_PACK=0 python3 scripts/smoke_test.py
```

Expected output includes:

```json
{
  "ok": true,
  "schemas": 64
}
```

## Main tool surface

The plugin exposes tools for:

- claims: `memory_wiki_query`, `memory_wiki_add_claim`, `memory_wiki_update_claim`, `memory_wiki_rewrite_claim`, `memory_wiki_merge_claims`, `memory_wiki_pin_claim`;
- evidence/provenance: `memory_wiki_add_evidence`, `memory_wiki_why_believe`, `memory_wiki_explain_recall`;
- context: `memory_wiki_pack_context`, `memory_wiki_recall_plan`, `memory_wiki_memory_diff`;
- preference priority: `memory_wiki_preference_layer`, `memory_wiki_add_preference_rule`;
- projects/tasks: `memory_wiki_add_project_profile`, `memory_wiki_get_project_context`, `memory_wiki_add_task_capsule`, `memory_wiki_post_task`;
- graph: `memory_wiki_add_entity`, `memory_wiki_add_relation`, `memory_wiki_graph_query`;
- quality: `memory_wiki_lint_claim`, `memory_wiki_review_queue`, `memory_wiki_curate`, `memory_wiki_vacuum`, `memory_wiki_immune_scan`, `memory_wiki_evaluate_retrieval`;
- contradictions: `memory_wiki_contradict`, `memory_wiki_resolve_contradiction`, `memory_wiki_resolve_by_policy`;
- operations: `memory_wiki_doctor`, `memory_wiki_health`, `memory_wiki_repair`, `memory_wiki_maintenance`, `memory_wiki_dashboard`, `memory_wiki_active_dashboard`;
- backup/sync: `memory_wiki_backup`, `memory_wiki_restore`, `memory_wiki_snapshot`, `memory_wiki_export_bundle`, `memory_wiki_import_bundle`;
- audit/rollback: `memory_wiki_audit_log`, `memory_wiki_recent_changes`, `memory_wiki_mutation_log`, `memory_wiki_undo_last`, `memory_wiki_transaction`;
- secrets metadata: `memory_wiki_add_secret`, `memory_wiki_query_secrets`, `memory_wiki_scrub_secrets`, `memory_wiki_secret_quarantine`.

## Safety boundary

This repo is intentionally source-only. Do not commit:

- `memory_wiki.sqlite3`, WAL/SHM/journal files or backup zips;
- `pages/`, `dashboards/`, `snapshots/`, `spool/`, `recovery/` runtime output;
- `.env`, credentials, vault files, session logs or real user memory;
- `__pycache__/`, `.pytest_cache/`, local build artifacts.

## License

MIT. See `LICENSE`.
