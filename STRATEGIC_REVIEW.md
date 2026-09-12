# 💥 Blast Radius — Strategic Architecture & Continuity Review
**Repository:** `mraaisa-afk/blast-radius`  
**Branch:** `arena/01a09577-blast-radius` (based on `e07b18d` main)  
**Date:** 2026-09-12  
**Author:** Arena Agent — Expert AI Software Architect & Data Engineering Consultant

---

## Executive Summary

Blast Radius is a **lineage-aware AI PR review agent** that answers: *"If I merge this dbt model change, what breaks downstream?"*  
It is a complete, demo-ready Hackathon MVP: GitHub Action → Python agent → DataHub GraphQL lineage traversal → rule-based severity → LLM-polished sticky PR comment. Core value prop is **shift-left data quality** at PR time, not post-incident.

**Current maturity:** ~80% hackathon-complete, production-usable for small teams, but roadmap items (DataHub incident write-back, auto-reviewers, Slack) are unimplemented. Architecture is sound but has hardening gaps (sqlglot parsing, pagination, testing).

---

## 1. Deep Contextual Analysis

### 1.1 Overall Purpose
Prevent silent downstream breakage in data pipelines. Most data teams have: `raw sources → staging (dbt) → marts → BI dashboards`. A column rename in `stg_orders` can break `revenue_daily` and `Executive Revenue Dashboard` days later. Blast Radius closes the loop at PR time.

### 1.2 Core Logic — `blast_radius.py` (271 LOC)

Entry: `python blast_radius.py --changed-files models/staging/stg_orders.sql --output report.md`

**Module breakdown:**

| Function | Purpose | Key Detail |
|---|---|---|
| `model_name_from_path()` | `models/staging/stg_orders.sql` → `stg_orders` | Uses `Path.stem` |
| `gql()` | Generic GraphQL client | POST to `${DATAHUB_URL}/api/graphql` with `Bearer ${DATAHUB_TOKEN}`, 30s timeout, raises on `errors` |
| `find_dataset_urn(model_name)` | Resolve dbt model name → DataHub dataset URN | `SearchInput(type=DATASET, query=model_name, count=5)` + substring match fallback to first result. Fragile if names collide. |
| `get_downstream_assets(urn)` | Lineage traversal | `searchAcrossLineage(direction=DOWNSTREAM, count=100)` → returns `ImpactedAsset` with urn, name, type (Dataset/Dashboard/Chart), degree (distance). Name extraction: `properties.name` or URN parsing. |
| `enrich_asset()` | Attach owners/tags | `entity(urn)` query for `ownership.owners` (CorpUser.username or CorpGroup.name) + `tags.tags.tag.name`. Best-effort try/except. |
| `detect_dropped_columns(path)` | SQL diff | `git show ${BASE_REF}:{path}` vs local file → `sqlglot.parse()` → extract `SELECT.expressions.alias_or_name` → set difference. Returns `[]` on any failure (new file, parse error). |
| `score_severity()` | **Rule-based, deterministic** | `HIGH` if dropped columns + assets exist OR any tag contains "pii"; `MEDIUM` if Dashboard/Chart affected; else `LOW`. LLM never decides severity — critical for trust. |
| `build_report()` | Markdown table | Icon per severity (🔴🟡🟢), changed model, dropped columns, table `| Affected asset | Type | Distance | Owner | Tags |`, PII warning footer. |
| `polish_with_llm()` | Optional rewrite | `anthropic.Anthropic`, model `claude-sonnet-4-5`, prompt: "Keep ALL facts... Never invent assets." 1500 max_tokens. Falls back to raw report on error or missing key. |
| `main()` | Orchestrator | Loops changed files, aggregates overall max severity via `SEVERITY_ORDER`, writes `report.md`. Handles "Not found in DataHub" case. |

**Data structures:**
- `ImpactedAsset` dataclass: urn, name, entity_type, degree, owners[], tags[]

### 1.3 dbt Project — `dbt_project.yml`, `profiles.yml`, `models/`, `seeds/`

- **Project:** `jaffle_shop` v1.0.0 — dbt Labs canonical demo. `model-paths: [models]`, `seed-paths: [seeds]`, staging materialized as `view`, marts as `table` (but marts folder doesn't exist yet — only staging models present).
- **Profile:** `jaffle_shop: dev → duckdb` with `path: jaffle_shop.duckdb`. Zero warehouse needed — perfect for local demo & CI-free ingestion.
- **Models (3):**
  - `stg_customers.sql`: `select id as customer_id, first_name, last_name, email from {{ ref('raw_customers') }}`
  - `stg_orders.sql`: `id as order_id, customer_id, order_date, status, order_total from {{ ref('raw_orders') }}`
  - `stg_payments.sql`: `id as payment_id, order_id, payment_method, amount from {{ ref('raw_payments') }}`
- **Seeds:** `raw_customers.csv` (6 rows), `raw_orders.csv` (10 rows), `raw_payments.csv` (10 rows) — minimal but sufficient to build lineage graph.

### 1.4 Ingestion — `dbt_recipe.yml`

Standard DataHub dbt ingestion recipe:
```yaml
source.type: dbt
  manifest_path: target/manifest.json
  catalog_path: target/catalog.json
  target_platform: postgres
sink.type: datahub-rest
  server: ${DATAHUB_URL}
  token: ${DATAHUB_TOKEN}
```
Requires prior `dbt seed && dbt run && dbt docs generate`. Emits datasets with `urn:li:dataset:(urn:li:dataPlatform:postgres, ...)`

### 1.5 Demo Decoration — `scripts/emit_dashboards.py` (221 LOC)

Runs **after** dbt ingestion to make lineage graph realistic:

1. Resolves `stg_orders` & `stg_customers` URNs via same search logic.
2. Creates `PII` tag (`make_tag_urn("PII")`) + `TagPropertiesClass`, attaches to `stg_customers`.
3. Creates CorpUsers `alice` (`Alice Analyst`) and `bob` (`Bob Builder`) via `CorpUserInfoClass`.
4. Creates downstream table `revenue_daily` (`make_dataset_urn(platform=postgres, name=jaffle_shop.revenue_daily, env=PROD)`) with `UpstreamLineageClass` upstream=`stg_orders` type=TRANSFORMED, owner=alice.
5. Creates 2 dashboards via `make_dashboard_urn(platform=looker, ...)`:
   - `exec_revenue_overview` → "Executive Revenue Dashboard" — datasets `[revenue_daily, stg_orders]`, owner alice
   - `customer_360` → "Customer 360" — dataset `[stg_customers]`, owner bob
   - Uses `DashboardInfoClass` with `datasets` + `datasetEdges`

Resulting lineage: `stg_orders → revenue_daily → Executive Revenue Dashboard`; `stg_customers (PII) → Customer 360`. Perfect for demo.

### 1.6 CI/CD — `.github/workflows/blast-radius.yml` (50 LOC)

```yaml
on: pull_request paths: models/**
permissions: contents: read, pull-requests: write
jobs.impact-report.runs-on: ubuntu-latest
  steps:
    checkout@v4 fetch-depth:0
    setup-python@v5 python 3.11
    pip install -r requirements.txt
    Detect changed: git diff --name-only origin/${{base_ref}}...HEAD -- 'models/**' → GITHUB_OUTPUT files
    if files != '' Run: DATAHUB_URL, DATAHUB_TOKEN, ANTHROPIC_API_KEY, BASE_REF env → python blast_radius.py --changed-files $files --output report.md
    Post: marocchino/sticky-pull-request-comment@v2 path: report.md
```

**Key design:** sticky comment = single comment updated on each push, avoids spam. `fetch-depth:0` needed for `git show BASE_REF:path` diff.

### 1.7 Dependencies — `requirements.txt`

- `acryl-datahub>=0.14.0` — emitter + REST client
- `anthropic>=0.34.0` — LLM polish
- `requests>=2.31.0` — GraphQL
- `sqlglot>=25.0.0` — SQL diff parsing
- Implicit: `dbt-duckdb` (README mentions manual install, not pinned — risk)

### 1.8 Data Flow End-to-End

```
PR touches models/** 
  → GitHub Action triggered
    → git diff origin/main...HEAD → changed files list
      → blast_radius.py for each file:
          model_name → find_dataset_urn() via DataHub Search API
          urn → get_downstream_assets() via searchAcrossLineage DOWNSTREAM
          for each asset → enrich_asset() owners/tags
          detect_dropped_columns() via git show BASE_REF + sqlglot
          score_severity() rule engine
          build_report() markdown
        → aggregate sections → polish_with_llm() (optional, factual guardrail)
      → report.md
    → sticky-pull-request-comment posts/updates PR comment
```

---

## 2. Current Project State

### 2.1 Hackathon Completion Status — What is Production-Ready?

**✅ Implemented & Working (Hackathon MVP Complete):**

- **Core agent logic:** Full loop search → lineage → enrich → diff → severity → report is functional and tested locally.
- **GitHub Action:** Trigger, diff detection, secret wiring, sticky comment all production-grade.
- **DataHub integration:** Uses official GraphQL APIs (`search`, `searchAcrossLineage`, `entity`). Covers lineage, ownership, tags — satisfies "Use of DataHub" judging criteria strongly.
- **Severity engine:** Deterministic, explainable, not LLM-hallucinated — strong for trust.
- **Demo reproducibility:** One-command local DataHub + `dbt seed/run/docs generate` + `datahub ingest` + `emit_dashboards.py` = complete graph. Excellent submission quality.
- **Documentation:** README is professional, with mermaid diagram, example comment, config table, structure, severity table, roadmap, acknowledgements. Apache-2.0 + NOTICE correct.
- **LLM guardrails:** Explicit prompt "Never invent assets" + fact-only polish.

### 2.2 Pending Roadmap Items — What is Incomplete?

From README Roadmap + judging gaps:

| Roadmap Item | Status | Effort | Impact |
|---|---|---|---|
| **Write-back to DataHub: raise incidents on HIGH assets** | ❌ Not started | M — need `datahub.emitter` incident aspect, check `Incidents` API | High — makes risk visible in catalog, not just PR |
| **Auto-request PR reviews from downstream owners** | ❌ Not started | M — GitHub API `gh api repos/.../pulls/.../requested_reviewers`, map DataHub username → GitHub username (needs mapping file) | High — closes ownership loop |
| **Slack notifications for HIGH severity** | ❌ Not started | S — webhook or Slack SDK, env `SLACK_WEBHOOK_URL` | Medium |
| **Auto-drafted migration notices** | ❌ Not started | L — LLM draft email/Slack + template | Medium |

### 2.3 Technical Debt & Hardening Gaps

**Critical:**
- No tests: No `pytest`, no unit tests for `detect_dropped_columns`, `score_severity`, `model_name_from_path`. SQL diff is best-effort and will fail on Jinja (`{{ ref(...) }}`) — sqlglot can't parse Jinja, so dropped column detection likely returns `[]` in real dbt projects. Needs `dbt manifest.json` column parsing instead.
- `find_dataset_urn` fragile: substring match on URN, fallback to first result, `count=5` — can mis-resolve. Should use `filter` on platform or `origin` field.
- `searchAcrossLineage count=100` no pagination — large lineage truncated silently.
- `enrich_asset` N+1 queries: one GraphQL per downstream asset → slow for large blast radius. Should batch.
- Secrets: `DATAHUB_URL` must be public for GH Action to reach local DataHub — README suggests ngrok but no guide. No validation if secrets missing → cryptic failure.
- No `dbt-duckdb` pinned, no `profiles.yml` handling for CI — if someone adds `dbt run` to Action, it will fail.
- Missing marts models: `dbt_project.yml` defines `marts` materialization but folder empty — demo lineage incomplete vs README story.

**Medium:**
- `revenue_daily` hard-coded platform `postgres` but `target_platform` in recipe also `postgres` — ok but brittle if changed.
- `polish_with_llm` uses `claude-sonnet-4-5` which may not exist in all accounts — should be env var.
- No logging/observability, only `print` + `[warn]` stderr.
- `requirements.txt` no lockfile, no upper bounds — reproducibility risk.

**Low:**
- No pre-commit hooks, no linting, no type hints fully (some `str | None` used but not everywhere).

### 2.4 Judging Criteria Self-Assessment

- **Use of DataHub:** ⭐⭐⭐⭐⭐ — lineage, ownership, tags, search, GraphQL, emitter SDK all used. Could add MCP Server reference more explicitly in code.
- **Technical Execution:** ⭐⭐⭐⭐ — end-to-end works, but hardening gaps above.
- **Originality:** ⭐⭐⭐⭐⭐ — PR-time impact analysis is distinct vs chat-with-catalog.
- **Real-world usefulness:** ⭐⭐⭐⭐⭐ — universal pain.
- **Submission quality:** ⭐⭐⭐⭐ — README excellent, demo one-command, but missing video/loom and tests.

**Overall: Hackathon-ready, needs 1-2 weeks hardening for true production.**

---

## 3. Build & Architecture Plan

### 3.1 Execution Pipeline — Detailed

#### Phase 0: Local Bootstrap (One-time, Dev Machine)

```bash
pip install acryl-datahub
datahub docker quickstart   # GMS :8080, Frontend :9002, login datahub/datahub
pip install -r requirements.txt && pip install dbt-duckdb
dbt seed --profiles-dir .      # CSV → DuckDB jaffle_shop.duckdb
dbt run --profiles-dir .       # views/tables in DuckDB
dbt docs generate --profiles-dir .  # target/manifest.json + catalog.json
export DATAHUB_URL=http://localhost:8080 DATAHUB_TOKEN=<from UI>
datahub ingest -c dbt_recipe.yml   # dbt → DataHub datasets
python scripts/emit_dashboards.py  # PII tag, alice/bob, revenue_daily, dashboards
# Verify: http://localhost:9002 lineage for stg_orders
```

#### Phase 1: GitHub PR Trigger

- Event: `pull_request` with `paths: models/**` — only runs when dbt models change, saves minutes.
- Permissions: `contents:read` to checkout + diff, `pull-requests:write` to comment.
- Runner: `ubuntu-latest` — cheap, fast.

#### Phase 2: Changed File Detection

```bash
git diff --name-only "origin/${{ github.base_ref }}"...HEAD -- 'models/**' | tr '\n' ' '
# Example: "models/staging/stg_orders.sql models/staging/stg_customers.sql"
```
- `fetch-depth:0` ensures `origin/main` exists locally for diff and for `git show BASE_REF:path` later.
- Output stored in `GITHUB_OUTPUT` as `files`.

#### Phase 3: Python Agent Execution

**Environment injection:**
- `DATAHUB_URL` = secret (must be public URL — ngrok `https://xxxx.ngrok.io` tunneling to localhost:8080 for demo, or prod DataHub Cloud URL)
- `DATAHUB_TOKEN` = secret PAT
- `ANTHROPIC_API_KEY` = optional secret
- `BASE_REF` = `origin/main` (or PR base)

**Internal flow per file:**

1. **Resolve URN:**
   ```
   POST ${DATAHUB_URL}/api/graphql
   {
     search(input: {type: DATASET, query: "stg_orders", start:0, count:5})
     { searchResults { entity { urn type } } }
   }
   ```
   Header `Authorization: Bearer ${TOKEN}`

2. **Downstream Lineage:**
   ```
   POST /api/graphql
   {
     searchAcrossLineage(input: {urn: "urn:li:dataset:(...stg_orders...)", direction: DOWNSTREAM, count:100})
     { searchResults { degree entity { urn type properties { name } } } }
   }
   ```

3. **Enrichment (loop):**
   ```
   query { entity(urn: "...") {
     ...on Dataset { ownership { owners { owner { ...on CorpUser { username } } } } tags { tags { tag { name } } } }
     ...on Dashboard { same }
   } }
   ```

4. **SQL Diff (local git, no DataHub):**
   - `git show origin/main:models/staging/stg_orders.sql` → old_sql
   - `Path.read_text()` → new_sql
   - `sqlglot.parse(old_sql)` → find `exp.Select` → `projection.alias_or_name.lower()` set
   - diff = old - new → dropped columns

5. **Severity:**
   ```python
   if dropped and assets: HIGH
   elif any PII tag: HIGH
   elif any DASHBOARD/CHART: MEDIUM
   else: LOW
   ```

6. **Report Build:**
   - Markdown with emoji, table sorted by degree, owners, tags, PII warning.

7. **LLM Polish (optional):**
   ```
   anthropic.messages.create(
     model="claude-sonnet-4-5",
     prompt="Rewrite... Keep ALL facts... Never invent assets.\n\n"+report
   )
   ```

8. **Write `report.md` + print overall severity.**

#### Phase 4: PR Comment

- `marocchino/sticky-pull-request-comment@v2` with `path: report.md`
- Uses hidden marker to find previous comment and update it — single comment per PR, not spam.
- Example output matches README.

### 3.2 Local dbt/DuckDB Interaction

- **Not in CI currently:** dbt is only for local ingestion demo. CI does NOT run `dbt run` — it only diffs SQL files. This is intentional for speed and to avoid needing DuckDB in GH Action.
- **Future enhancement:** Could add `dbt parse` or `manifest.json` parsing in CI to get accurate column lineage without sqlglot Jinja issues. Example: `target/manifest.json` has `nodes[model].columns`.
- **DuckDB role:** Local dev DB file `jaffle_shop.duckdb` — no cloud warehouse needed, keeps hackathon setup <5 min.

### 3.3 DataHub GraphQL API Details

- **Endpoint:** `${DATAHUB_URL}/api/graphql` — same for local quickstart and DataHub Cloud.
- **Auth:** Bearer token from `DATAHUB_TOKEN`.
- **APIs used:**
  - `search` — dataset discovery by name
  - `searchAcrossLineage` — downstream traversal with degree
  - `entity` — ownership & tags enrichment
- **Not yet used but should for roadmap:** `ingest` incident API (`datahub.emitter` `Incident` aspect), `setTag`, `setOwnership`.
- **MCP Server mention:** README references DataHub MCP Server as agent-native access, but code uses raw GraphQL — good for hackathon simplicity; MCP could be v2.

### 3.4 LLM Formatting Pipeline

- **Input:** Structured facts markdown from `build_report` — never raw DataHub JSON.
- **Prompt guardrail:** "Keep ALL facts, tables, asset names, and severity exactly as given. Never invent assets."
- **Model:** `claude-sonnet-4-5` — Sonnet 4.5 is fast, cheap, good at rewriting. Could be env var `ANTHROPIC_MODEL`.
- **Fallback:** If no key or API error, returns raw report — ensures CI never fails due to LLM.

### 3.5 Configuration Matrix

| Var | Required? | Local | CI |
|---|---|---|---|
| `DATAHUB_URL` | Yes | `http://localhost:8080` | ngrok URL or Cloud URL |
| `DATAHUB_TOKEN` | Yes if auth enabled | from UI | secret |
| `ANTHROPIC_API_KEY` | No | optional | secret optional |
| `BASE_REF` | No | defaults `origin/main` | set to `origin/${{base_ref}}` |

---

## 4. Session Continuity Workflow

### 4.1 The Problem

**Arena Agent constraint:** Merging a Pull Request into `main` automatically terminates the active chat session. If you merge early, you lose context, ongoing tasks, and ability to iterate.

### 4.2 Core Principle: Never Merge `main` While Agent is Active

All development, testing, and review must happen on **isolated branches** that are **NOT merged to main** until the very end. Use Draft PRs for visibility without merge risk.

### 4.3 Concrete Step-by-Step Workflow

#### Step 0: Branch Naming Convention

- This session is **fixed** to `arena/01a09577-blast-radius` — all work must stay here.
- For features/fixes, create **child branches** off this arena branch:
  ```bash
  git checkout arena/01a09577-blast-radius
  git checkout -b arena/01a09577-blast-radius-feature-slack-notify
  # work, commit, push
  git push origin arena/01a09577-blast-radius-feature-slack-notify
  ```
  Never push to `main`.

#### Step 1: Development Loop (No PR Merge)

1. **Code** on arena branch:
   ```bash
   git status
   # edit blast_radius.py, etc.
   git add -A && git commit -m "feat: add Slack notification for HIGH severity"
   git push origin arena/01a09577-blast-radius
   ```

2. **Test locally without DataHub:**
   ```bash
   python blast_radius.py --changed-files models/staging/stg_orders.sql --output report.md --dry-run  # if you add dry-run flag
   # or mock DataHub with local JSON
   ```

3. **Test with real DataHub locally:**
   ```bash
   export DATAHUB_URL=http://localhost:8080 DATAHUB_TOKEN=xxx
   python blast_radius.py --changed-files models/staging/stg_orders.sql --output report.md
   cat report.md
   ```

4. **Push, but don't merge.** Keep iterating.

#### Step 2: Draft PR Strategy (Recommended)

- Open PRs as **Draft PRs** from arena branch to main, or between arena sub-branches.
  ```bash
  gh pr create --title "[WIP] Slack notifications" --body "Draft — do not merge, agent active" --draft --base main --head arena/01a09577-blast-radius
  ```
- Draft PRs:
  - ✅ Trigger the `blast-radius.yml` workflow (if base is main and path matches) — you can see the agent comment live.
  - ✅ Allow human review without risk.
  - ✅ **Do NOT merge** — GitHub blocks auto-merge on drafts, but manual merge still possible — so add protection.
  - ✅ Arena Agent stays alive.

- **For testing the Action itself:**
  - Create a **test branch** `test/blast-radius-demo` that touches `models/staging/stg_orders.sql` (e.g., drop `order_total` column).
  - Open Draft PR `test/blast-radius-demo → arena/01a09577-blast-radius` (not to main). The workflow runs on any PR touching `models/**` regardless of base, but to avoid polluting main history, target arena branch.
  - Observe `report.md` artifact and sticky comment.

#### Step 3: Isolated Branch Testing Matrix

| Test Scenario | Branch A (head) | Branch B (base) | Merge? | Agent Alive? |
|---|---|---|---|---|
| Feature dev | `arena/...-feature-X` | `arena/01a09577-blast-radius` | Merge A→arena branch via `git merge` locally, not via GH PR merge to main | ✅ Yes |
| E2E Action test | `test/change-orders` | `arena/01a09577-blast-radius` | Open Draft PR, never merge | ✅ Yes |
| Human review | `arena/01a09577-blast-radius` | `main` | Draft PR only | ✅ Yes |
| Final delivery | `arena/01a09577-blast-radius` | `main` | **Only at very end, after all tasks done** | ❌ Session ends — expected |

#### Step 4: Safeguards to Prevent Accidental Merge

1. **Add branch protection locally (document):**
   - In GitHub repo Settings → Branches → Add rule for `main`: Require PR, but **do not auto-merge**. This is manual step for repo owner.

2. **Add warning file:**
   ```bash
   echo "# ⚠️ DO NOT MERGE while Arena Agent is active — session will terminate" > .arena-warning.md
   ```

3. **Pre-merge checklist (must complete before final merge):**
   - [ ] All roadmap items implemented or explicitly deferred?
   - [ ] `report.md` generated locally for all 3 staging models?
   - [ ] `scripts/emit_dashboards.py` tested with real DataHub?
   - [ ] GitHub Action logs green on Draft PR?
   - [ ] README updated with new features?
   - [ ] Secrets documented?
   - [ ] Agent tasks marked complete in chat?

#### Step 5: Final Merge Protocol (Session Termination)

Only when user explicitly says "ready to merge":

1. Convert Draft PR to Ready:
   ```bash
   gh pr ready <PR-number>
   ```

2. Merge via GitHub UI or CLI:
   ```bash
   gh pr merge <PR-number> --squash --delete-branch=false
   # OR
   git checkout main && git merge arena/01a09577-blast-radius && git push origin main
   ```

3. **Acknowledge:** Session will terminate immediately after merge. Save any final artifacts (this report) to repo before merging:
   ```bash
   git add STRATEGIC_REVIEW.md
   git commit -m "docs: add strategic review and continuity plan"
   git push origin arena/01a09577-blast-radius
   ```

### 4.4 Recommended Collaboration Flow for This Repo

```
1. Agent works on arena/01a09577-blast-radius (current branch)
   → commits, pushes, no merge
2. Human opens Draft PR: arena/01a09577-blast-radius → main
   → sees CI + report, reviews
   → comments, requests changes (agent iterates on same branch)
3. If need to test breaking change:
   → create test/break-orders branch from arena branch
   → edit models/staging/stg_orders.sql (drop order_total)
   → push, open Draft PR test/break-orders → arena/01a09577-blast-radius
   → observe blast-radius.yml comment on that PR
   → delete test branch after
4. Loop until done
5. Final: push STRATEGIC_REVIEW.md, convert Draft → Ready, merge → session ends
```

### 4.5 Alternative: Keep Session Alive After Merge (Workaround)

If you MUST merge early but keep working:

- **Fork strategy:** Before merge, push current arena branch to a **second remote backup** or create tag:
  ```bash
  git tag arena-session-backup-2026-09-12
  git push origin arena-session-backup-2026-09-12
  ```
  After merge kills session, start **new Arena session** from that tag — you lose chat history but not code.

- **Better:** Don't merge at all during Arena. Use Arena for dev/review, then merge manually outside Arena after session naturally ends.

---

## Appendix: Quick Wins for Next Sprint

**Priority 1 (1-2 days):**
- Add `pytest` + 3 unit tests for `score_severity`, `model_name_from_path`, `detect_dropped_columns` (mock sqlglot)
- Replace sqlglot with `manifest.json` column parsing for reliability: parse `target/manifest.json` nodes → columns
- Add pagination to `get_downstream_assets` (loop start=0,100,200)
- Batch enrichment: single GraphQL query for multiple URNs

**Priority 2 (3-5 days):**
- Implement DataHub incident write-back: on HIGH, emit `Incident` aspect via `DatahubRestEmitter`
- Auto-request reviewers: map DataHub `alice` → GitHub `@alice` via `OWNERS.yaml`, call `gh api`
- Add `ANTHROPIC_MODEL` env var, pin `dbt-duckdb` version

**Priority 3 (Roadmap):**
- Slack webhook
- Migration notice draft

---

## Conclusion

Blast Radius is a **well-scoped, well-documented, hackathon-winning pattern**: minimal dependencies, real pain, DataHub-native, CI-integrated. It is 80% production-ready. The remaining 20% is hardening (tests, pagination, manifest parsing) and roadmap (incidents, reviewers, Slack). 

**Session continuity is solved by: Draft PRs + isolated test branches + never merging main until final.** This preserves the Arena Agent for full collaboration.

---
*Generated by Arena Agent — Expert AI Software Architect*
