# memory-wiki

`memory-wiki` is a native Hermes memory provider plugin that turns long-term assistant memory into a local SQLite + Markdown wiki vault.

It stores structured claims, evidence, decisions, lessons, project profiles, task capsules, entity graph relations, recall feedback, and redacted secret index metadata. It also provides query, ranking, curation, audit, backup, restore, and health tools for maintaining a clean operational memory layer.

The repository contains only the plugin source code and helper scripts. It does **not** include any runtime database, user memory, secrets, sessions, logs, snapshots, backups, or vault data.

## Features

- SQLite-backed durable memory with FTS5 search.
- Markdown wiki page rendering by topic.
- Typed claims with confidence, salience, freshness, quality and trust scoring.
- Evidence tracking and `why_believe` provenance inspection.
- Contradiction recording and resolution.
- Review queue, linting, curation, topic normalization and deduplication tools.
- Secret-aware storage: secret values are redacted in recall and indexed separately from normal claims.
- Secret quarantine and scrub tools for old/raw memory fields.
- Task capsules, post-task logs, decisions, mistakes and project profiles.
- Lightweight entity graph with relations.
- Backup, restore, export, audit log, dashboard, health and repair tools.
- Optional local/OpenAI-compatible LLM pass for context packing.
- Stdlib-only Python implementation; designed to work in minimal Linux, Android/proot and Hermes environments.

## Repository layout

```text
.
├── __init__.py                     # Hermes MemoryProvider plugin implementation
├── plugin.yaml                     # Hermes plugin metadata
├── scripts/
│   ├── memory_wiki_cli.py          # Standalone CLI wrapper
│   └── smoke_test.py               # End-to-end smoke test suite
├── .gitignore
├── LICENSE
└── README.md
```

Runtime files are created under `$HERMES_HOME/memory-wiki` and are intentionally ignored by git.

## Requirements

- Python 3.10+ recommended.
- SQLite with FTS5 support.
- Hermes agent/plugin runtime for normal provider usage.

No third-party Python packages are required for the plugin itself.

## Installation in Hermes

Clone or copy this repository into your Hermes plugins directory:

```bash
cd ~/.hermes/plugins
git clone https://github.com/sbrejnev988-coder/memory-wiki.git memory-wiki
```

Then restart or reload Hermes so it discovers `plugin.yaml`.

The plugin stores runtime data in:

```text
$HERMES_HOME/memory-wiki/
```

Typical files/directories created there include:

- `memory_wiki.sqlite3`
- `pages/`
- `dashboards/`
- `backups/`
- `snapshots/`
- `spool/`

These are local runtime data and should not be committed.

## Standalone CLI

The CLI loads the provider directly and is useful for local maintenance:

```bash
python3 scripts/memory_wiki_cli.py --home ~/.hermes dashboard
python3 scripts/memory_wiki_cli.py --home ~/.hermes query "project configuration" --limit 10
python3 scripts/memory_wiki_cli.py --home ~/.hermes pack "what should I remember for this task" --max-chars 3800
python3 scripts/memory_wiki_cli.py --home ~/.hermes doctor
python3 scripts/memory_wiki_cli.py --home ~/.hermes backup --reason manual
```

Use an isolated home for tests:

```bash
tmp_home=$(mktemp -d)
python3 scripts/memory_wiki_cli.py --home "$tmp_home" dashboard
```

## Smoke tests

Run syntax checks and the full smoke suite:

```bash
python3 -m py_compile __init__.py scripts/memory_wiki_cli.py scripts/smoke_test.py
python3 scripts/smoke_test.py
```

The smoke test creates a temporary `$HERMES_HOME`, exercises the tool schemas and core memory operations, then removes the temporary home.

To keep the temporary home for inspection:

```bash
MEMORY_WIKI_KEEP_SMOKE_HOME=1 python3 scripts/smoke_test.py
```

## Main Hermes tools

The plugin exposes `memory_wiki_*` tools through Hermes, including:

- `memory_wiki_query`
- `memory_wiki_add_claim`
- `memory_wiki_add_evidence`
- `memory_wiki_update_claim`
- `memory_wiki_why_believe`
- `memory_wiki_pack_context`
- `memory_wiki_recall_plan`
- `memory_wiki_add_secret`
- `memory_wiki_query_secrets`
- `memory_wiki_scrub_secrets`
- `memory_wiki_secret_quarantine`
- `memory_wiki_add_decision`
- `memory_wiki_add_mistake`
- `memory_wiki_add_project_profile`
- `memory_wiki_add_task_capsule`
- `memory_wiki_add_entity`
- `memory_wiki_add_relation`
- `memory_wiki_graph_query`
- `memory_wiki_review_queue`
- `memory_wiki_lint_claim`
- `memory_wiki_health`
- `memory_wiki_doctor`
- `memory_wiki_backup`
- `memory_wiki_restore`
- `memory_wiki_export`
- `memory_wiki_audit_log`

The smoke test verifies that registered schemas match dispatch handlers.

## Optional LLM context packing

`memory_wiki_pack_context` can optionally call a local/OpenAI-compatible chat completions endpoint for secondary context selection.

Environment variables:

```bash
export MEMORY_WIKI_LLM_PACK=1
export MEMORY_WIKI_LLM_BASE_URL=http://127.0.0.1:18646/v1
export MEMORY_WIKI_LLM_API_KEY=example-api-key
export MEMORY_WIKI_LLM_MODEL=local-model
export MEMORY_WIKI_LLM_TIMEOUT=45
```

If `MEMORY_WIKI_LLM_PACK` is unset or false, the plugin uses rule-based packing only.

## Data and security model

- Runtime memory is local to `$HERMES_HOME/memory-wiki`.
- Normal claims should contain clean durable facts, procedures and preferences, not raw logs.
- Secret values should be stored via the secret index, not normal claims.
- Recall output redacts secret values by default.
- `query_secrets(..., reveal=true)` is explicit and audited.
- Scrubbing and quarantine tools help migrate accidental secret-like material out of normal memory fields.

This public repository is source-only. Before publishing a fork, keep `.gitignore` rules that exclude local runtime data.

## Development checklist

Before committing plugin changes:

```bash
python3 -m py_compile __init__.py scripts/memory_wiki_cli.py scripts/smoke_test.py
python3 scripts/smoke_test.py
```

For behavior changes, update all affected areas together:

- schema definitions
- dispatch handlers
- core provider logic
- smoke tests
- README or skill references

## License

MIT License. See [LICENSE](LICENSE).
