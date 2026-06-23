# `dev/` — developer-only tooling (NOT shipped)

Everything under `dev/` is for maintainers of this sample. It is **excluded from
both deploy paths**: the demo packager (`infrastructure/scripts/package_and_upload.sh`)
never bundles it, and the workshop packager (`infrastructure/scripts/package_for_workshop.sh`)
excludes `*dev/*` from the participant zip. None of it runs at deploy time or at
runtime — the live system is built entirely from CloudFormation + the curated
`agent_code.zip`.

## `dev/evaluation/` — Strands Evals harness (was `app/agentcore_strands/evaluation/`)

Offline evaluation of the deployed agent. Moved here because it is authoring/QA
tooling, not part of the agent runtime package.

- `generate_ground_truth.py` — query a local PostgreSQL `timely_unicorn` DB with
  explicit `account_id` filters to produce `dataset/validation/ground_truth.json`.
- `build_experiment.py` — expand the ground truth into a Strands Evals experiment
  (`dataset/validation/experiment.json`), ~100 cases across SQL/SOP/guardrail/RLS/RBAC.
- `run_evaluation.py` — run the experiment against the deployed AgentCore Runtime
  via `agentcore invoke`, authenticating per-persona through Cognito, and score
  with an LLM-as-judge. (This is the Step-10 "evaluation" exercise's reference
  implementation; the workshop page itself drives the `agentcore eval` CLI.)

Paths inside these scripts are relative to this folder: `../../dataset/validation/...`
and `../../app/agentcore_strands/gateway_config.json`. Run them from anywhere
(`python3 dev/evaluation/run_evaluation.py`) — they resolve paths from `__file__`.

## `dev/legacy-tests/` — archived unit tests for the retired bootstrap scripts

When the one-off data-bootstrap scripts (`init_database.py`, `register_glue_tables.py`,
`generate_embeddings.py`) were retired in favour of CloudFormation custom resources
(`custom-resource-lambdas/{database_init,glue_crawler_trigger,bedrock_kb_ingestion}`),
their unit tests were preserved here rather than deleted.

- `test_init_database.py`, `test_generate_embeddings.py`

> ⚠️ These tests `import` the now-removed scripts and will **not** collect as-is.
> They are retained for reference / future revival only. The shipped suite under
> `infrastructure/tests/` (`test_integration.py`, `test_workshop_deployment.py`)
> no longer depends on the bootstrap scripts.
