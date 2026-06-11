# memory-wiki

**memory-wiki** is a source-only Hermes Agent plugin that turns assistant memory into a local, inspectable **Memory OS**: a SQLite + Markdown wiki vault with ranked recall, evidence, provenance, redaction, curation, recovery, transactions and operational dashboards.

It is designed for long-running AI-agent installations where memory must be durable, searchable, auditable and safe to maintain over months of work.

> This public repository contains only plugin source, metadata and helper scripts. It intentionally excludes runtime databases, user memory, secrets, sessions, logs, snapshots, backups, vault files and other local artifacts.

## Current version

`plugin.yaml`: **1.2.0**

Version 1.2 focuses on making memory-wiki behave like a real operational memory layer rather than a loose note store:

- write firewall and source policy checks before durable writes;
- sectioned `pack_context` without rigid per-section budgets;
- mutation log, undo and transactional batch operations;
- typed project/graph context;
- provenance cards and `why_believe` inspection;
- redacted sync bundles for cross-profile/import workflows;
- retrieval-quality evaluation;
- claim compiler/topic summarizer;
- stronger secret firewall, quarantine and scrub tooling;
- doctor/repair/self-healing workflows for SQLite/FTS/dashboard issues.

## What it stores

memory-wiki stores structured operational memory, including:

- claims/facts with confidence, salience, freshness, trust and access counts;
- evidence and provenance for claims;
- decisions, mistakes/lessons and user corrections;
- project profiles and task capsules;
- typed entity graph relations;
- redacted secret index metadata;
- review queues, lint results and curation actions;
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
- Recall explanations for debugging why a claim was selected.
- Feedback loop via `memory_wiki_mark_used`.

### Memory quality control

- Candidate review queue.
- Claim linting and in-place rewrite.
- Topic normalization and topic alias cleanup.
- Deduplication, vacuum and merge tools.
- Contradiction recording, policy-based resolution and manual resolution.
- Synthetic topic compression and curated topic compilation.

### Safety and redaction

- Secret-like content detection.
- Secret index separate from normal claims.
- Redacted recall by default.
- Secret quarantine and scrub workflows.
- Source policy and write firewall before durable writes.
- Redacted export/import bundles for sync.

### Operations and recovery

- SQLite `doctor`, `health`, `repair`, FTS rebuild and dashboard render checks.
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

## Installation in Hermes

Clone this repository into your Hermes plugins directory:

```bash
cd ~/.hermes/plugins
git clone https://github.com/sbrejnev988-coder/memory-wiki.git memory-wiki
```

Enable the plugin in your Hermes config:

```yaml
plugins:
  enabled:
    - memory-wiki

memory:
  provider: memory-wiki
```

Then restart/reload Hermes so the plugin registry is rebuilt.

## Standalone CLI

The helper CLI loads the provider directly and is useful for local maintenance or debugging outside a running Hermes process:

```bash
python3 scripts/memory_wiki_cli.py --home ~/.hermes dashboard
python3 scripts/memory_wiki_cli.py --home ~/.hermes query "project configuration" --limit 10
python3 scripts/memory_wiki_cli.py --home ~/.hermes pack "what context is relevant for this task" --max-chars 3800
python3 scripts/memory_wiki_cli.py --home ~/.hermes doctor
python3 scripts/memory_wiki_cli.py --home ~/.hermes repair --target fts --apply
python3 scripts/memory_wiki_cli.py --home ~/.hermes backup --reason manual
```

Use an isolated home for testing:

```bash
tmp_home=$(mktemp -d)
python3 scripts/memory_wiki_cli.py --home "$tmp_home" dashboard
```

## Smoke tests

Run syntax checks and the full smoke suite:

```bash
python3 -m py_compile __init__.py scripts/memory_wiki_cli.py scripts/smoke_test.py
MEMORY_WIKI_LLM_PACK=0 python3 scripts/smoke_test.py
```

The smoke test creates a temporary `$HERMES_HOME`, verifies registered tool schemas, dispatch parity and core memory operations, then removes the temporary home.

To keep the temporary home for inspection:

```bash
MEMORY_WIKI_KEEP_SMOKE_HOME=1 MEMORY_WIKI_LLM_PACK=0 python3 scripts/smoke_test.py
```

## Main Hermes tools

The plugin exposes 60+ `memory_wiki_*` tools. Major groups:

### Query and recall

- `memory_wiki_query`
- `memory_wiki_pack_context`
- `memory_wiki_recall_plan`
- `memory_wiki_explain_recall`
- `memory_wiki_mark_used`
- `memory_wiki_why_believe`

### Claims, evidence and corrections

- `memory_wiki_add_claim`
- `memory_wiki_add_evidence`
- `memory_wiki_update_claim`
- `memory_wiki_rewrite_claim`
- `memory_wiki_apply_user_correction`

### Secrets and redaction

- `memory_wiki_add_secret`
- `memory_wiki_query_secrets`
- `memory_wiki_migrate_secrets_from_claims`
- `memory_wiki_scrub_secrets`
- `memory_wiki_secret_quarantine`
- `memory_wiki_write_firewall`
- `memory_wiki_source_policy`

### Typed operational memory

- `memory_wiki_add_decision`
- `memory_wiki_add_mistake`
- `memory_wiki_add_project_profile`
- `memory_wiki_get_project_context`
- `memory_wiki_add_task_capsule`
- `memory_wiki_post_task`

### Graph memory

- `memory_wiki_add_entity`
- `memory_wiki_add_relation`
- `memory_wiki_graph_query`

### Maintenance and quality

- `memory_wiki_review_queue`
- `memory_wiki_lint_claim`
- `memory_wiki_health`
- `memory_wiki_evaluate_retrieval`
- `memory_wiki_curate`
- `memory_wiki_vacuum`
- `memory_wiki_normalize_topics`
- `memory_wiki_immune_scan`
- `memory_wiki_compress_topic`
- `memory_wiki_compile_topic`

### Contradictions and merges

- `memory_wiki_contradict`
- `memory_wiki_resolve_contradiction`
- `memory_wiki_resolve_by_policy`
- `memory_wiki_merge_claims`

### Backups, exports and recovery

- `memory_wiki_doctor`
- `memory_wiki_repair`
- `memory_wiki_backup`
- `memory_wiki_list_backups`
- `memory_wiki_restore`
- `memory_wiki_snapshot`
- `memory_wiki_export`
- `memory_wiki_export_bundle`
- `memory_wiki_import_bundle`
- `memory_wiki_audit_log`
- `memory_wiki_mutation_log`
- `memory_wiki_undo_last`
- `memory_wiki_transaction`

## Optional LLM context packing

`memory_wiki_pack_context` can optionally call a local/OpenAI-compatible chat completions endpoint for secondary context selection.

```bash
export MEMORY_WIKI_LLM_PACK=1
export MEMORY_WIKI_LLM_BASE_URL=http://127.0.0.1:18646/v1
export MEMORY_WIKI_LLM_MODEL=local-model
export MEMORY_WIKI_LLM_TIMEOUT=45
# If the endpoint requires auth, set MEMORY_WIKI_LLM_API_KEY in your shell.
```

If `MEMORY_WIKI_LLM_PACK` is unset or false, the plugin uses deterministic/rule-based packing only.

## Data and publication model

- Runtime memory is local to `$HERMES_HOME/memory-wiki`.
- Normal claims should contain compact durable facts, not raw logs.
- Procedures belong in skills/docs; task outcomes belong in task capsules or post-task summaries.
- Secret values belong in a secret vault/index, not normal claims.
- Recall and export paths redact secret-like values by default.
- Public forks should keep `.gitignore` rules that exclude local runtime data.

## Development checklist

Before committing plugin changes:

```bash
python3 -m py_compile __init__.py scripts/memory_wiki_cli.py scripts/smoke_test.py
MEMORY_WIKI_LLM_PACK=0 python3 scripts/smoke_test.py
git status --short
```

For behavior changes, update all affected areas together:

- plugin metadata;
- tool schemas;
- dispatch handlers;
- provider/storage logic;
- smoke tests;
- README and operational docs.

Before publishing a public snapshot, scan the export for local runtime artifacts and secret-like strings.

## License

MIT License. See [LICENSE](LICENSE).
