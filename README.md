<div align="center"><br><br># 💥 Blast Radius<br><br>**A lineage-aware AI review agent that tells you what your data change will break — before you merge.**<br><br>Built for [Build with DataHub: The Agent Hackathon](https://datahub.devpost.com/) · Powered by [DataHub](https://datahub.com/), the open-source Context Platform<br><br>[![DataHub](https://img.shields.io/badge/DataHub-Context%20Platform-blue)](https://datahub.com/)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![GitHub Actions](https://img.shields.io/badge/CI-GitHub%20Actions-2088FF?logo=githubactions&logoColor=white)](https://docs.github.com/en/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)<br><br></div><br><br>---<br><br>## 📌 The Problem<br><br>In most companies, data flows through a long chain: raw sources → staging models → business tables → executive dashboards. When an engineer changes one model — renames a column, drops a field — they usually have **no idea what sits downstream**. The breakage surfaces days later, when a dashboard silently shows wrong numbers.<br><br>**Blast Radius closes that gap.** On every pull request, an AI agent queries DataHub's lineage graph, computes the downstream impact of the change, and posts a clear, actionable report directly on the PR — affected dashboards, severity, PII warnings, and the owners who need to know.<br><br>## ✨ What It Does<br><br>When a PR touches any dbt model, Blast Radius automatically:<br><br>- 🔍 **Maps changed files to data assets** in DataHub (dbt model → dataset URN)<br>- 🕸️ **Traverses downstream lineage** to find every affected table, view, and dashboard<br>- 🚨 **Detects breaking changes** by diffing SQL for dropped/renamed columns and checking them against downstream schemas<br>- 🔐 **Flags PII exposure** when affected assets carry a `PII` tag<br>- 👥 **Identifies owners** of impacted assets so the right people get looped in<br>- 🤖 **Writes a concise impact report** with an LLM — severity score, affected assets table, and a Mermaid lineage diagram — posted as a sticky PR comment<br><br>### Example PR comment<br><br>
> ### 🔴 Blast Radius: HIGH severity
> **Changed:** `stg_orders` — column `order_total` dropped
>
> | Affected asset | Type | Owner |
> |---|---|---|
> | `revenue_daily` | Table | @alice |
> | Executive Revenue Dashboard | Dashboard | @alice |
> | `customer_lifetime_value` | Table | @bob |
>
> ⚠️ `customers` is tagged **PII** — review before merging.<br><br>## 🏗️ Architecture<br><br>```mermaid
flowchart LR
    A[Pull Request] --> B[GitHub Action]
    B --> C[Blast Radius Agent]
    C -->|lineage, owners, tags, schemas| D[(DataHub\nMCP / GraphQL)]
    C -->|structured facts| E[LLM]
    E --> F[Impact Report]
    F -->|sticky comment| A
```<br><br>1. A GitHub Action triggers on PRs touching `models/**`.
2. The agent resolves changed dbt models to DataHub URNs.
3. It queries DataHub for downstream lineage, ownership, tags, and schemas (via the [DataHub MCP server](https://docs.datahub.com/docs/features/feature-guides/mcp) or GraphQL API).
4. A rule engine scores severity (dropped column used downstream = HIGH; PII asset affected = HIGH; additive change = LOW).
5. An LLM turns the structured facts into a readable Markdown report — it is **never allowed to invent assets**.
6. The report is posted as a single, continuously updated PR comment.<br><br>## 🚀 Getting Started<br><br>### Prerequisites<br><br>- Docker Desktop (for local DataHub)
- Python 3.9+
- A DataHub instance ([quickstart guide](https://docs.datahub.com/docs/features)) with your dbt project ingested
- An Anthropic or OpenAI API key<br><br>### 1. Run DataHub locally<br><br>```bash
python3 -m pip install --upgrade acryl-datahub
datahub docker quickstart
```<br><br>Then create an access token in the DataHub UI (**Settings → Access Tokens**).<br><br>### 2. Ingest your dbt project<br><br>```bash
dbt docs generate
datahub ingest -c dbt_recipe.yml
```<br><br>### 3. Configure environment<br><br>```bash
export DATAHUB_URL="http://localhost:8080"
export DATAHUB_TOKEN="<your-datahub-token>"
export ANTHROPIC_API_KEY="<your-llm-key>"
```<br><br>### 4. Run locally<br><br>```bash
pip install -r requirements.txt
python blast_radius.py --changed-files models/staging/stg_orders.sql --output report.md
```<br><br>### 5. Enable the GitHub Action<br><br>Add these repository secrets (**Settings → Secrets and variables → Actions**):<br><br>| Secret | Description |
|---|---|
| `DATAHUB_URL` | Public URL of your DataHub instance (use an [ngrok](https://ngrok.com) tunnel for local demos) |
| `DATAHUB_TOKEN` | DataHub personal access token |
| `ANTHROPIC_API_KEY` | LLM API key |

The workflow at [`.github/workflows/blast-radius.yml`](.github/workflows/blast-radius.yml) runs on every PR that touches `models/**` and posts the impact report automatically.<br><br>## 📁 Project Structure<br><br>```
.
├── blast_radius.py            # Agent entrypoint: diff → lineage → severity → report
├── requirements.txt           # Python dependencies
├── dbt_recipe.yml             # DataHub ingestion recipe for the dbt project
├── models/                    # dbt models (raw → staging → marts)
├── .github/
│   └── workflows/
│       └── blast-radius.yml   # CI workflow that posts the PR comment
└── docs/
    └── architecture.png       # Architecture diagram
```

## 🧠 How Severity Is Scored

Severity is **rule-based, not vibes-based**:

| Condition | Severity |
|---|---|
| Dropped/renamed column referenced by a downstream asset | 🔴 HIGH |
| Any affected asset tagged `PII` | 🔴 HIGH |
| Downstream dashboards affected, no schema break detected | 🟡 MEDIUM |
| Additive-only change (new columns, new models) | 🟢 LOW |

The LLM only narrates facts collected from DataHub and the SQL diff.

## 🗺️ Roadmap

- [ ] Auto-request PR reviews from downstream asset owners
- [ ] Slack notifications for HIGH-severity changes
- [ ] Auto-drafted migration notices for affected teams
- [ ] Write-back to DataHub (documentation & incident annotations) once MCP write support ships

## 🙏 Acknowledgements

- [DataHub](https://github.com/datahub-project/datahub) — the open-source metadata platform powering lineage, ownership, and tags
- [DataHub MCP Server](https://github.com/acryldata/mcp-server-datahub) — agent-native access to DataHub context
- [dbt](https://www.getdbt.com/) & the Jaffle Shop example project

## 📄 License

Released under the [MIT License](LICENSE).
