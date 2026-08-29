# Engineering standards

The production practices this project is built to. Each entry states **the standard**, then
**how it is implemented here**. Where a free-tier constraint forces a deviation, the deviation
is written down rather than quietly absorbed — an undocumented compromise is a defect; a
documented one is a decision.

A consolidated checklist is at the end.

---

## 1. Environments and isolation

**Standard.** Dev, staging and production are separate, with identical code and different
data. Nothing is promoted by hand. Production is never edited in a UI.

**Here.** Three Unity Catalog catalogs — `ceylon_dev`, `ceylon_stg`, `ceylon_prod` — each with
`bronze` / `silver` / `gold` schemas and a `raw` volume. The catalog name is a bundle variable,
never a literal in code:

```yaml
targets:
  dev:  { variables: { catalog: ceylon_dev  }, mode: development }
  stg:  { variables: { catalog: ceylon_stg  } }
  prod: { variables: { catalog: ceylon_prod }, mode: production }
```

`mode: production` makes the bundle refuse to deploy with a personal identity and pins the
run-as service principal — the guardrail that stops "I'll just fix it in the workspace."

> **Deviation.** Free Edition allows one workspace and one metastore, so the three environments
> are three catalogs in one workspace rather than three workspaces. Real isolation needs
> separate workspaces per environment; the bundle targets are structured so that becomes a host
> change, not a rewrite. Write this up as an ADR when the workspace is created (practice 74).

**Standard.** Automation authenticates as a service principal, never a human.
**Here.** OAuth M2M service principal per environment, secrets in GitHub Environments with
required reviewers on `prod`. No personal access tokens anywhere.

**Standard.** Cloud infrastructure is code.
**Here.** `infra/` holds Terraform for the resource group, ADLS Gen2, Access Connector, Key
Vault and ADF. `terraform plan` runs on every PR; no portal click-ops. Workspace assets — jobs,
pipelines, clusters, schemas — are Databricks Asset Bundles in `resources/`.

## 2. Source control and CI/CD

**Standard.** Short-lived branches off a protected trunk; every change reviewed; main always
deployable.
**Here.** Branch protection on `main`: no direct pushes, PR + green CI required, linear history.
Conventional Commits, `CHANGELOG.md` generated from them, tagged releases.

**Standard.** CI is the same checks a developer runs locally, and it is fast.
**Here.** `pre-commit` runs the identical hook set CI runs, so failures surface before push:

| Gate | Tool |
|---|---|
| Format + lint | `ruff format --check`, `ruff check` |
| Type check | `mypy --strict` on `ingestion/`, `transformation/` |
| SQL lint | `sqlfluff` (Databricks dialect) on `sql/` and `dbt/` |
| Unit tests + coverage floor | `pytest`, `--cov-fail-under=80` |
| Bundle validity | `databricks bundle validate` per target |
| dbt correctness | `dbt parse`, `dbt compile` |
| Secret scan | `gitleaks` |
| Dependency CVEs | `pip-audit`, Dependabot |
| IaC | `terraform validate`, `tflint`, `checkov` |

**Standard.** Deployment is automated, ordered and reversible.
**Here.** Merge to `main` → auto-deploy `dev` → integration tests against `ceylon_dev` → deploy
`stg` → smoke tests → **manual approval** → deploy `prod`. Rollback is redeploying the previous
git tag, because the bundle is the full definition of the workspace.

**Standard.** Dependencies are pinned and reproducible.
**Here.** `uv` with a committed lockfile; the same lock installed in CI and on clusters.
Transformation logic ships as a versioned wheel, not as loose notebook cells.

## 3. Secrets and security

**Standard.** No credential ever exists in source, notebooks, logs or config files.
**Here.** Databricks secret scopes backed by Azure Key Vault; `dbutils.secrets.get` at runtime
only. `gitleaks` in CI and in pre-commit. Log formatter redacts anything matching known key
patterns.

**Standard.** Least privilege, granted to groups, not individuals.
**Here.** UC grants in `sql/ddl/90_grants.sql`, applied by the bundle:

| Principal | Grant |
|---|---|
| `svc_ingest` | `USE CATALOG`, `MODIFY` on `bronze` only |
| `svc_transform` | `SELECT` on `bronze`/`silver`, `MODIFY` on `silver`/`gold` |
| `analysts` | `SELECT` on `gold` only |
| `owners` | `ALL PRIVILEGES`, one group, small |

No principal has write access to a layer it does not produce.

**Standard.** Sensitive data is classified before it is stored, not after an incident.
**Here.** Every source's data contract declares a PII classification. This project's sources are
public and aggregate, so the honest classification is *none* — but the mechanism is built and
exercised: UC tags (`pii`, `confidential`), plus one column mask and one row filter demonstrated
on a synthetic column so the pattern exists before it is needed.

**Standard.** Access is auditable.
**Here.** UC audit and system tables retained; access reviewed as part of the quarterly docs pass.

## 4. Data architecture and contracts

**Standard.** Every source has a written contract before a line of ingestion code is written.
**Here.** `docs/contracts/<source>.yml` — owner, cadence, expected schema with types and
nullability, primary key, freshness SLA, volume bounds, licence/terms, and the agreed behaviour
when the source breaks. The contract is machine-readable and is what the DQ suite asserts
against, so documentation and enforcement cannot drift apart.

**Standard.** Layer responsibilities are strict and one-directional.
**Here.**

| Layer | Rule |
|---|---|
| **Bronze** | Raw bytes as received, append-only, immutable, never edited. Ingest metadata only (`_ingested_at`, `_source_file`, `_run_id`). No parsing, no filtering, no dedup |
| **Silver** | Typed, validated, deduplicated, conformed. Business keys resolved, SCD2 dimensions. One row = one real-world event or entity version |
| **Gold** | Business-facing star schema and feature tables. No source-system vocabulary leaks through |

Nothing reads upward. Gold never queries bronze.

**Standard.** Schemas are explicit in production. Inference is a development convenience.
**Here.** Auto Loader uses `cloudFiles.schemaHints` plus a checked-in schema location; silver
declares `StructType` explicitly. `mergeSchema` is never enabled on a scheduled job — an
unexpected column is an alert and a quarantined batch, not a silent table mutation.

**Standard.** Jobs are idempotent and re-runnable for any date without side effects.
**Here.** Every job takes `--run-date`; writes are `MERGE` on natural keys or partition
overwrite of exactly that date. Re-running yesterday produces the same table, always. This is
also the backfill mechanism — no separate backfill code path to rot.

**Standard.** Exactly-once, and provably so.
**Here.** Auto Loader checkpoints for file discovery; `MERGE` on business keys downstream. A
test re-runs the same batch twice and asserts row counts are unchanged.

**Standard.** Late and out-of-order data is designed for, not discovered.
**Here.** Silver keys on event date, not ingest date. Watermarks in `run_metadata` track the
high-water mark per source. A record arriving four days late lands in the correct partition and
triggers downstream recompute of the affected dates only.

**Standard.** Dimensional models declare their grain.
**Here.** Every dbt fact model's `schema.yml` states the grain in one sentence and has a
uniqueness test on the grain's key set. Surrogate keys are generated, never natural. SCD2
dimensions carry `valid_from` / `valid_to` / `is_current`.

**Standard.** Retention and physical layout are decisions with reasons.
**Here.** `VACUUM` retention (7 days) and time-travel expectations are documented in the
runbook and aligned with the recovery procedure. Clustering keys are chosen from observed query
predicates and recorded in the model docs — not defaulted to the ingest date out of habit.

## 5. Data quality

**Standard.** Quality checks are gates that fail the pipeline, not reports someone reads later.
**Here.** Lakeflow expectations run in-graph with explicit severities, and the job's DQ task
exits non-zero on any `fail`-severity breach. Downstream tasks never run on bad data.

**Standard.** Checks are layered, because different failures appear at different stages.
**Here.**

| Layer | Checks |
|---|---|
| Bronze | File arrived; parseable; row count within historical bounds; schema matches the contract |
| Silver | Types, nullability, PK uniqueness, referential integrity, domain ranges, freshness |
| Gold | Fact-to-dimension orphan check, grain uniqueness, reconciliation of gold aggregates back to silver totals |

**Standard.** Rejected records are quarantined, never dropped silently.
**Here.** Expectation violations route to `<layer>_quarantine` with the rule that failed and the
`run_id`. Quarantine volume is itself a monitored metric; replay is a documented procedure.

**Standard.** Thresholds are relative to history, not hardcoded.
**Here.** Volume and distribution checks compare against a trailing 30-day median from
`run_metadata`, so a source that quietly halves its output is caught even though it is non-empty.

**Standard.** Quality is measured over time, not per run.
**Here.** Results persist to a `dq_results` Delta table; pass rate per source per day is a panel
on the health dashboard, and a sustained decline is visible before it becomes an outage.

## 6. Observability

**Standard.** One correlation ID spans an entire run, across every system.
**Here.** A `run_id` is generated by the GitHub Actions run and threaded through extraction,
the volume path, every task parameter, `run_metadata`, `dq_results` and every log line. One id
reconstructs the whole run.

**Standard.** Logs are structured and machine-queryable.
**Here.** JSON logging with fixed fields (`run_id`, `source`, `layer`, `event`, `duration_ms`).
No `print`. No f-string narration in place of fields.

**Standard.** Pipelines emit operational metrics, not just data.
**Here.** `run_metadata` — `run_id`, `source`, `layer`, `rows_in`, `rows_out`, `rows_rejected`,
`duration_ms`, `status`, `error_type`, `cluster_id`, `cost_usd`. Populated by every task,
successful or not.

**Standard.** Every dataset has a stated SLO and alerts on breach.
**Here.** Contracts declare freshness and completeness SLOs (e.g. *weather bronze is fresh
within 6 hours, 99% of days*). A monitoring task compares actuals against them and alerts on
breach — including the silent failure mode of a job that succeeds but produces nothing.

**Standard.** Alerts are actionable, routed and rare.
**Here.** Failure alerts name the source, the failing rule, the `run_id` and the runbook
section. Routed to a real channel. Duration alerts fire at 3× the trailing median, so a job
degrading is caught before it starts failing. No alert exists that nobody would act on.

**Standard.** Failure modes have runbooks written before the failure.
**Here.** `docs/runbook.md`, one section per known failure mode — source layout changed,
endpoint down, schema drift, DQ gate breach, quota exhausted, partial run — each with
diagnosis, remedy and replay steps.

**Standard.** Cost is observable per pipeline, per run.
**Here.** System tables (`system.billing.usage`) joined to `run_metadata` on job run id gives
cost per run, trended on the health dashboard. Budget alerts on the Azure resource group.

## 7. Testing

**Standard.** A test pyramid, not a pile of integration tests.
**Here.**

| Level | Scope | Speed |
|---|---|---|
| Unit | Pure functions — parsing, key derivation, SCD2 logic — no Spark | milliseconds |
| Spark local | Transformations on `pyspark` + `delta-spark` with committed fixtures | seconds |
| Data quality | Contract assertions against real ingested data | per run |
| Integration | Bundle deployed to `ceylon_dev`, one source end-to-end | per merge |
| Smoke | Post-deploy on `stg` and `prod`: tables exist, are fresh, are non-empty | per deploy |

**Standard.** Tests use committed fixtures, never live sources or production data.
**Here.** `tests/fixtures/` holds captured real responses, including the malformed ones —
truncated payload, changed column order, empty result, duplicate keys. Every bug fixed gains a
fixture that reproduces it first.

**Standard.** Business logic is testable without a cluster.
**Here.** Transformations are pure functions over DataFrames in importable modules. Notebooks
are thin entrypoints that parse parameters and call the wheel — no logic lives in a notebook,
because a notebook cannot be unit tested or code-reviewed line by line.

## 8. Orchestration

**Standard.** Job definitions live in version control, not in a UI.
**Here.** `resources/jobs/*.yml` in the bundle. A job created by hand in the workspace is drift,
and the next deploy removes it.

**Standard.** Scheduled work runs on ephemeral job clusters.
**Here.** Job clusters on the Azure target, serverless on Free Edition. All-purpose clusters are
for interactive development only and auto-terminate at 10 minutes.

**Standard.** Every task declares timeout, retries and concurrency.
**Here.** Retries with exponential backoff on transient failures only — never on a DQ gate, as
retrying bad data just produces bad data more slowly. `max_concurrent_runs: 1`, so a slow run
cannot overlap the next schedule.

**Standard.** One flaky dependency cannot take down the platform.
**Here.** Sources are extracted independently and fan in. A failing source marks itself degraded
in `run_metadata`, alerts, and lets the run continue with the rest — downstream models that
depend on it are skipped by a conditional task, not fed stale data.

## 9. Performance and cost

**Standard.** Optimise against measurement, never against intuition.
**Here.** `docs/perf-lab.md` records baseline timings, the Spark UI evidence for each change,
and the measured result. Any tuning claim without a before-and-after number is not made.

**Standard.** Physical layout follows query patterns.
**Here.** Clustering keys derived from actual predicate usage in query history; `OPTIMIZE`
scheduled weekly; small-file counts monitored. Partition columns are only chosen where
cardinality justifies them.

**Standard.** Compute is right-sized and never left running.
**Here.** Auto-terminate everywhere, spot workers with on-demand fallback, autoscale bounds set
deliberately, Photon enabled only where it measurably pays for itself.

## 10. Governance and documentation

**Standard.** One governance plane, with ownership assigned.
**Here.** Unity Catalog throughout. Every table has an owner group, a description, and domain
and tier tags. Ownerless tables fail a CI check.

**Standard.** Documentation is generated from the system wherever it can be.
**Here.** `docs/data-dictionary.md` regenerates from `information_schema` in CI, so it cannot go
stale. Column descriptions live in dbt `schema.yml` and propagate into UC comments; a model
merged without descriptions fails `dbt test`.

**Standard.** Significant decisions are recorded with their alternatives.
**Here.** ADRs in `docs/adr/`, numbered, immutable, superseded rather than edited.

**Standard.** A newcomer can run the project from the README alone.
**Here.** README carries the architecture diagram, a quickstart that works from a clean clone,
the live dashboard link, and links to runbook, contracts and dictionary.

## 11. Reliability and recovery

**Standard.** There is a defined recovery point, and it has been tested.
**Here.** Bronze is the recovery point: raw payloads are immutable, so any downstream corruption
is fixed by reprocessing rather than re-fetching from sources that may no longer serve the same
data. Silver and gold are fully reproducible from bronze.

**Standard.** Recovery is rehearsed, not theorised.
**Here.** A quarterly drill: drop a silver table in `dev`, rebuild it from bronze, and record
the wall-clock time in the runbook. Delta `RESTORE` and time travel are the fast path; full
reprocess is the guaranteed one.

**Standard.** The whole platform can be rebuilt from source control.
**Here.** `terraform apply` plus `databricks bundle deploy` reconstructs infrastructure,
catalogs, schemas, grants, jobs and pipelines. The only irreplaceable asset is bronze data.

## 12. Ways of working

**Standard.** Definition of done includes tests, docs, monitoring and a rollback path — not
just working code.
**Here.** PR template with that checklist; reviewers enforce it.

**Standard.** Work is visible and small.
**Here.** Issues in a GitHub Project board, one PR per issue, PRs kept small enough to review
properly.

**Standard.** Incidents produce written learning.
**Here.** Every pipeline failure that reaches `main` gets a short post-incident note appended to
the runbook: what broke, how it was found, what now prevents or detects it.

---

## Build order

Best practice inverts the layer-by-layer plan. Build a **walking skeleton** first: one source
travelling the entire path — extract → bronze → silver → gold → dashboard — with CI/CD, tests,
DQ gates, alerting and a runbook entry, deployed to all three environments. It is thin, and it
is genuinely production-grade.

Then widen: each additional source is a repeat of a proven path, and integration risk was paid
down in week one rather than discovered in week four.

| Stage | Deliverable |
|---|---|
| 1 — Foundations | Repo hygiene, pre-commit, CI gates, Terraform, three catalogs, service principals, secret scopes, bundle skeleton |
| 2 — Walking skeleton | One source (Open-Meteo) end-to-end to a published dashboard, with contract, tests, DQ gates, `run_metadata`, alerting, runbook, promotion to prod |
| 3 — Widen | Remaining sources against the proven path, including one scraped and one PDF source; quarantine and replay exercised for real |
| 4 — Deepen | Full star schema, SCD2 dimensions, CDF-driven incremental gold, Genie space |
| 5 — Harden | Performance lab, cost attribution, backfill drill, recovery drill, SLO alerting, load characterisation |
| 6 — Extend | MLflow baseline forecast, registered in UC, retrained and monitored on schedule |

---

## Checklist

**Environments**
1. Separate dev / staging / prod with identical code and different data
2. No manual changes in production; UI-created objects treated as drift
3. Service principals for all automation; zero personal access tokens
4. Manual approval gate before production deploys
5. Infrastructure as code; no portal click-ops
6. Workspace assets declared as bundles, version controlled

**Source control and CI/CD**
7. Protected trunk, PR review required, linear history
8. Conventional Commits, generated changelog, tagged releases
9. Pre-commit hooks identical to CI gates
10. Lint, format, type check, SQL lint enforced
11. Coverage floor enforced in CI
12. Secret scanning and dependency CVE scanning
13. IaC validation and policy scanning on every PR
14. Pinned dependencies with a committed lockfile
15. Logic packaged as a versioned wheel, not loose notebook cells
16. Ordered promotion dev → stg → prod with smoke tests at each step
17. Rollback by redeploying a previous tag

**Security**
18. No secrets in source, notebooks, config or logs
19. Secret scopes backed by a key vault
20. Least privilege granted to groups, per layer
21. No principal writes to a layer it does not produce
22. Data classified in the contract before ingestion
23. Column masks and row filters available and exercised
24. Audit logging retained and reviewed

**Architecture**
25. Written, machine-readable data contract per source
26. Bronze immutable and append-only; no parsing or filtering
27. Strict one-directional layer responsibilities
28. Explicit schemas in production; no inference, no silent `mergeSchema`
29. Idempotent, re-runnable jobs parameterised by run date
30. Backfill uses the same code path as normal runs
31. Exactly-once semantics, asserted by a re-run test
32. Late and out-of-order data handled by design
33. Watermarks tracked per source
34. Declared grain per fact, with a uniqueness test
35. Surrogate keys; SCD2 with validity ranges and a current flag
36. Retention, `VACUUM` and clustering decisions documented with reasons

**Data quality**
37. DQ gates fail the pipeline; downstream never runs on bad data
38. Checks layered across bronze, silver and gold
39. Reconciliation of gold back to silver totals
40. Rejected records quarantined with rule and run id, never dropped
41. Replay procedure for quarantined data
42. Thresholds relative to trailing history, not hardcoded
43. DQ results persisted and trended over time

**Observability**
44. One correlation id across the entire run
45. Structured JSON logging with fixed fields
46. Operational metrics table covering rows, duration, status, cost
47. Freshness and completeness SLOs declared per dataset
48. Alerting on SLO breach, including success-with-no-data
49. Duration alerting relative to trailing median
50. Actionable alerts naming source, rule, run id and runbook section
51. Runbook written per failure mode before it occurs
52. Cost attributed per pipeline run; budget alerts configured
53. Pipeline health dashboard separate from the business dashboard

**Testing**
54. Test pyramid: unit, Spark-local, DQ, integration, smoke
55. Committed fixtures including malformed and edge-case payloads
56. Every fixed bug gains a regression fixture first
57. Business logic testable without a cluster
58. Notebooks are thin entrypoints only; no logic in notebooks

**Orchestration**
59. Job definitions in version control, not the UI
60. Ephemeral job clusters for scheduled work
61. Explicit timeouts, retries and concurrency limits
62. Retries on transient failures only, never on DQ gates
63. Graceful degradation: one failing source cannot fail the platform
64. Dependent models skipped, not fed stale data

**Performance and cost**
65. Tuning driven by measurement with before-and-after evidence
66. Physical layout derived from observed query predicates
67. Scheduled `OPTIMIZE`; small-file counts monitored
68. Auto-terminate, autoscale bounds, spot with fallback
69. Photon enabled only where it measurably pays

**Governance and docs**
70. Single governance plane with per-table ownership
71. Domain, tier and sensitivity tags applied
72. Ownerless or undescribed tables fail CI
73. Data dictionary generated from the catalog, never hand-maintained
74. ADRs for significant decisions, superseded rather than edited
75. README sufficient to run the project from a clean clone

**Reliability**
76. Defined recovery point; downstream fully reproducible from it
77. Recovery drills rehearsed and timed, not assumed
78. Entire platform rebuildable from source control

**Ways of working**
79. Definition of done covers tests, docs, monitoring and rollback
80. Small, single-issue pull requests
81. Post-incident notes appended to the runbook
82. Walking skeleton before horizontal expansion
