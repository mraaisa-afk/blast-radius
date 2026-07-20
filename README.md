# Blast Radius Agent

**An AI agent for DataHub that finds the root cause of data breakages, maps their full blast radius, flags at-risk ML models, generates a draft fix, and writes the incident back into the DataHub graph — so the next incident (or the next agent) doesn't start from zero.**

## 🚨 The Problem

When a data pipeline breaks in a large organization (a column changes, a data quality test fails, an upstream schema shifts), a data engineer typically spends hours manually answering:

- Which dashboards or reports are affected?
- Which ML models depend on this broken data?
- Who owns the affected assets, and who needs to be notified?
- How do we actually fix it?

This is a real, painful, and time-consuming problem — and most hackathon submissions in this space stop at "a chatbot that searches the catalog." Blast Radius Agent goes much deeper: it doesn't just read DataHub's metadata graph, it acts on an incident end-to-end and writes its findings back into the graph.

## 🧠 How It Works

Blast Radius Agent is made of five modules, run in sequence by a single orchestrator:

1. **Root Cause Detector** — Walks upstream lineage from the broken asset to find what actually changed (schema change, ownership change, or a quality signal going bad).
2. **Blast Radius Mapper** — Walks downstream lineage to list every dashboard, report, and ML model that depends on the broken asset, tagged with its real owner.
3. **ML Risk Flagger** — If a broken column feeds an ML model's features, flags the model's risk level (High / Medium / Low) using DataHub's end-to-end ML lineage — before the model silently degrades in production.
4. **Code-Gen Fix Suggester** — Reads the current live schema via the DataHub MCP Server, generates a realistic patch (e.g. a dbt model or Airflow DAG diff), and opens it as a draft pull request with full context attached.
5. **Write-Back Module** — Creates/updates an "Incident" document in DataHub, links it to every affected asset, and tags them as "at-risk" — so the next incident (or the next agent) doesn't start from zero.
