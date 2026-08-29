# ceylon-lakehouse

Medallion-architecture lakehouse ingesting Sri Lankan tourism, retail, energy, commodity
and capital-market data.

**Stack:** Databricks (Lakeflow Jobs & Pipelines, Unity Catalog, Auto Loader, SQL warehouse, MLflow) · PySpark · Delta Lake · dbt · Azure (Databricks, ADLS Gen2, Data Factory) · GitHub Actions

> **Status: planning.** The staged build is in [docs/roadmap.md](docs/roadmap.md) and the
> practices it is built to are in [docs/engineering-standards.md](docs/engineering-standards.md).
> No pipeline code yet.

## Why this project

Sri Lanka's economy is concentrated in a handful of sectors — leisure, retail/FMCG, ports
and marine fuel, tea, and the Colombo capital market. This platform ingests public data
across all of them into a single lakehouse, so cross-sector questions ("does rainfall in
the hill country move tea auction averages?") become one SQL query instead of five
spreadsheets.

## Planned architecture

```
                +-- Open-Meteo (daily, keyless)
                +-- FX / LKR rates (daily)
   sources -----+-- Brent crude / bunker proxy (daily)
                +-- CSE trade summary (trading days, unofficial endpoint)
                +-- Colombo Tea Auction (weekly, scraped)
                +-- SLTDA arrivals / World Bank macro (monthly)
                        |
                        v
            EXTRACT on a GitHub Actions runner -- the one place with open egress
                        |  raw bytes, cached verbatim, uploaded via the Databricks CLI
                        v
   BRONZE   raw, immutable, partitioned by ingest date        (UC volume -> Delta)
                        |  schema enforcement, dedup, SCD2 dims
                        v
   SILVER   typed, conformed, DQ-gated                        (PySpark on serverless)
                        |  dbt models
                        v
   GOLD     Kimball star schema + ML feature tables           (dbt -> AI/BI dashboard)

   Platform:      Databricks -- one codebase, two bundle targets
                    free  : Free Edition serverless, UC managed storage (permanent)
                    azure : Azure Databricks job clusters, ADLS Gen2 external location
   Orchestration: Lakeflow Jobs daily; ADF -> Databricks notebook activity on the azure target
   Observability: run_metadata Delta table + system tables -> pipeline health & cost
```

## Repo layout (planned)

```
config/         source definitions, logging config
ingestion/      connector factory + one module per source
transformation/ PySpark bronze -> silver jobs (run on serverless)
dbt/            silver -> gold star schema models (dbt-databricks)
resources/      Lakeflow job, pipeline + cluster definitions (Databricks Asset Bundle)
infra/          Bicep/Terraform for the Azure sprint (ADLS Gen2, ADF, Key Vault)
tests/          pytest unit + integration
docs/           plan, architecture, data contracts, runbook, ADRs
.github/        CI (lint, test, dbt build) + daily-ingest workflow
```

## Docs

- [Roadmap](docs/roadmap.md) — the staged build, simple to advanced
- [Engineering standards](docs/engineering-standards.md) — the practices this is built to, and the checklist
