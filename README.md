💥 Blast Radius
A lineage-aware AI review agent that tells you what your data change will break — before you merge.

Built for Build with DataHub: The Agent Hackathon · Powered by DataHub, the open-source Context Platform

DataHub
Python
GitHub Actions
License: MIT

📌 The Problem
In most companies, data flows through a long chain: raw sources → staging models → business tables → executive dashboards. When an engineer changes one model — renames a column, drops a field — they usually have no idea what sits downstream. The breakage surfaces days later, when a dashboard silently shows wrong numbers.

Blast Radius closes that gap. On every pull request, an AI agent queries DataHub's lineage graph, computes the downstream impact of the change, and posts a clear, actionable report directly on the PR — affected dashboards, severity, PII warnings, and the owners who need to know.

✨ What It Does
When a PR touches any dbt model, Blast Radius automatically:

🔍 Maps changed files to data assets in DataHub (dbt model → dataset URN)
🕸️ Traverses downstream lineage to find every affected table, view, and dashboard
🚨 Detects breaking changes by diffing SQL for dropped/renamed columns and checking them against downstream schemas
🔐 Flags PII exposure when affected assets carry a PII tag
👥 Identifies owners of impacted assets so the right people get looped in
🤖 Writes a concise impact report with an LLM — severity score, affected assets table, and a Mermaid lineage diagram — posted as a sticky PR comment
Example PR comment
🔴 Blast Radius: HIGH severity
Changed: stg_orders — column order_total dropped

Affected asset	Type	Owner
revenue_daily	Table	@alice
Executive Revenue Dashboard	Dashboard	@alice
customer_lifetime_value	Table	@bob
⚠️ stg_customers is tagged PII — review before merging.

🏗️ Architecture
flowchart LR
    A[Pull Request] --> B[GitHub Action]
    B --> C[Blast Radius Agent]
    C -->|lineage, owners, tags, schemas| D[(DataHub)]
    C -->|structured facts| E[LLM]
    E --> F[Impact Report]
    F -->|sticky comment| A
A GitHub Action triggers on PRs touching models/**.
The agent resolves changed dbt models to DataHub URNs.
It queries DataHub for downstream lineage, ownership, tags, and schemas via the GraphQL API (see also the DataHub MCP server).
A rule engine scores severity (dropped column used downstream = HIGH; PII asset affected = HIGH; additive change = LOW).
An LLM turns the structured facts into a readable Markdown report — it is never allowed to invent assets.
The report is posted as a single, continuously updated PR comment.
🚀 Getting Started
Prerequisites
Docker Desktop (for local DataHub)
Python 3.9+
An Anthropic API key (optional — used only to polish the report)
1. Run DataHub locally
python -m pip install --upgrade acryl-datahub
datahub docker quickstart
Then open http://localhost:9002 and generate an access token (Settings → Access Tokens).

2. Build and ingest the demo dbt project
pip install -r requirements.txt
dbt seed --profiles-dir .
dbt run --profiles-dir .
export DATAHUB_URL="http://localhost:8080"
export DATAHUB_TOKEN="<your-datahub-token>"
dbt docs generate --profiles-dir .
datahub ingest -c dbt_recipe.yml
python scripts/emit_dashboards.py   # demo dashboards, owners, and PII tags
3. Run the agent manually
export ANTHROPIC_API_KEY="<your-llm-key>"   # optional
python blast_radius.py --changed-files models/staging/stg_orders.sql --output report.md
4. Enable the GitHub Action
Add these repository secrets (Settings → Secrets and variables → Actions):

Secret	Description
DATAHUB_URL	Public URL of your DataHub instance (use an ngrok tunnel for local demos)
DATAHUB_TOKEN	DataHub personal access token
ANTHROPIC_API_KEY	LLM API key (optional)
The workflow at .github/workflows/blast-radius.yml runs on every PR that touches models/** and posts the impact report automatically.

⚙️ Configuration
Variable	What it is	Where to get it
DATAHUB_URL	DataHub backend URL	http://localhost:8080 for local quickstart
DATAHUB_TOKEN	DataHub access token	DataHub UI → Settings → Access Tokens
ANTHROPIC_API_KEY	LLM key for report polishing (optional)	console.anthropic.com
BASE_REF	Git ref to diff against	Set automatically by CI (defaults to origin/main)
📁 Project Structure
.
├── blast_radius.py            # Agent entrypoint: diff → lineage → severity → report
├── requirements.txt           # Python dependencies
├── dbt_project.yml            # Demo dbt project (jaffle_shop)
├── profiles.yml               # Local DuckDB profile
├── dbt_recipe.yml             # DataHub ingestion recipe
├── seeds/                     # Demo source data (CSV)
├── models/
│   ├── staging/               # Cleaned source models
│   └── marts/                 # Business tables feeding dashboards
├── scripts/
│   └── emit_dashboards.py     # Emits demo dashboards, owners & PII tags
└── .github/workflows/
    └── blast-radius.yml       # CI workflow that posts the PR comment
🧠 How Severity Is Scored
Severity is rule-based, not vibes-based:

Condition	Severity
Dropped/renamed column referenced by a downstream asset	🔴 HIGH
Any affected asset tagged PII	🔴 HIGH
Downstream dashboards affected, no schema break detected	🟡 MEDIUM
Additive-only change (new columns, new models)	🟢 LOW
The LLM only narrates facts collected from DataHub and the SQL diff.

🏆 Hackathon Judging Criteria
Use of DataHub — lineage traversal, ownership, tags, and schema metadata drive every decision the agent makes.
Technical execution — end to end: PR opened → CI → DataHub queries → severity engine → report comment.
Originality — impact analysis at PR time, where the merge decision actually happens; not another "chat with your catalog."
Real-world usefulness — every data team with dbt + GitHub feels this pain weekly.
Submission quality — reproducible demo (seeded dbt project + one-command DataHub), clear README, CI included.
🗺️ Roadmap
[ ] Auto-request PR reviews from downstream asset owners
[ ] Slack notifications for HIGH-severity changes
[ ] Auto-drafted migration notices for affected teams
[ ] Write-back to DataHub (documentation & incident annotations)
🙏 Acknowledgements
DataHub — the open-source metadata platform powering lineage, ownership, and tags
DataHub MCP Server — agent-native access to DataHub context
dbt & the Jaffle Shop example project
📄 License
Released under the MIT License.
