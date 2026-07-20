#!/usr/bin/env python3
"""Blast Radius — lineage-aware impact analysis for dbt pull requests.

Given a list of changed dbt model files, this script:
  1. Resolves each model to its DataHub dataset URN.
  2. Fetches downstream lineage, owners, and tags from DataHub.
  3. Scores the severity of the change with simple, explainable rules.
  4. Writes a Markdown impact report (optionally polished by an LLM).

Usage:
    python blast_radius.py --changed-files models/staging/stg_orders.sql --output report.md
"""

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import requests

DATAHUB_URL = os.environ.get("DATAHUB_URL", "http://localhost:8080").rstrip("/")
DATAHUB_TOKEN = os.environ.get("DATAHUB_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BASE_REF = os.environ.get("BASE_REF", "origin/main")

GRAPHQL_ENDPOINT = f"{DATAHUB_URL}/api/graphql"

SEVERITY_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


@dataclass
class ImpactedAsset:
    urn: str
    name: str
    entity_type: str
    degree: int
    owners: list = field(default_factory=list)
    tags: list = field(default_factory=list)


def gql(query: str, variables: dict) -> dict:
    """Send a GraphQL request to DataHub and return the `data` payload."""
    headers = {"Content-Type": "application/json"}
    if DATAHUB_TOKEN:
        headers["Authorization"] = f"Bearer {DATAHUB_TOKEN}"
    resp = requests.post(
        GRAPHQL_ENDPOINT,
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(f"DataHub GraphQL error: {body['errors']}")
    return body["data"]


def model_name_from_path(path: str) -> str:
    """models/staging/stg_orders.sql -> stg_orders"""
    return Path(path).stem


def find_dataset_urn(model_name: str) -> str | None:
    """Resolve a dbt model name to a DataHub dataset URN via search."""
    query = """
    query search($input: SearchInput!) {
      search(input: $input) {
        searchResults { entity { urn type } }
      }
    }
    """
    data = gql(query, {"input": {"type": "DATASET", "query": model_name, "start": 0, "count": 5}})
    results = (data.get("search") or {}).get("searchResults") or []
    for result in results:
        urn = result["entity"]["urn"]
        if model_name.lower() in urn.lower():
            return urn
    return results[0]["entity"]["urn"] if results else None


def get_downstream_assets(urn: str) -> list[ImpactedAsset]:
    """Fetch every downstream dataset/dashboard/chart from DataHub lineage."""
    query = """
    query lineage($input: SearchAcrossLineageInput!) {
      searchAcrossLineage(input: $input) {
        searchResults {
          degree
          entity {
            urn
            type
            ... on Dataset { properties { name } }
            ... on Dashboard { properties { name } }
            ... on Chart { properties { name } }
          }
        }
      }
    }
    """
    data = gql(query, {"input": {"urn": urn, "direction": "DOWNSTREAM", "start": 0, "count": 100}})
    assets = []
    for result in (data.get("searchAcrossLineage") or {}).get("searchResults") or []:
        entity = result["entity"]
        props = entity.get("properties") or {}
        assets.append(
            ImpactedAsset(
                urn=entity["urn"],
                name=props.get("name") or entity["urn"].split(",")[-2] if "," in entity["urn"] else entity["urn"],
                entity_type=entity["type"],
                degree=result["degree"],
            )
        )
    return assets


def enrich_asset(asset: ImpactedAsset) -> None:
    """Attach owners and tags to an impacted asset (best-effort)."""
    query = """
    query enrich($urn: String!) {
      entity(urn: $urn) {
        ... on Dataset {
          ownership { owners { owner { ... on CorpUser { username } ... on CorpGroup { name } } } }
          tags { tags { tag { name } } }
        }
        ... on Dashboard {
          ownership { owners { owner { ... on CorpUser { username } ... on CorpGroup { name } } } }
          tags { tags { tag { name } } }
        }
      }
    }
    """
    try:
        data = gql(query, {"urn": asset.urn})
        entity = data.get("entity") or {}
        for owner in ((entity.get("ownership") or {}).get("owners") or []):
            info = owner.get("owner") or {}
            name = info.get("username") or info.get("name")
            if name:
                asset.owners.append(name)
        for tag in ((entity.get("tags") or {}).get("tags") or []):
            asset.tags.append(tag["tag"]["name"])
    except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
        print(f"[warn] could not enrich {asset.urn}: {exc}", file=sys.stderr)


def detect_dropped_columns(path: str) -> list[str]:
    """Compare the old and new version of a model file and report dropped columns.

    Uses sqlglot to extract the output column names of the final SELECT.
    Best-effort: returns [] when parsing fails or the file is new.
    """
    try:
        import sqlglot

        old_sql = subprocess.run(
            ["git", "show", f"{BASE_REF}:{path}"],
            capture_output=True, text=True, check=True,
        ).stdout
        new_sql = Path(path).read_text() if Path(path).exists() else ""

        def output_columns(sql: str) -> set[str]:
            columns = set()
            for expr in sqlglot.parse(sql):
                if expr is None:
                    continue
                select = expr.find(sqlglot.exp.Select)
                if select:
                    for projection in select.expressions:
                        columns.add(projection.alias_or_name.lower())
            return columns

        return sorted(output_columns(old_sql) - output_columns(new_sql))
    except Exception:  # noqa: BLE001 - diffing is best-effort
        return []


def score_severity(assets: list[ImpactedAsset], dropped_columns: list[str]) -> str:
    """Rule-based severity. The LLM never decides this."""
    if dropped_columns and assets:
        return "HIGH"
    if any("pii" in tag.lower() for asset in assets for tag in asset.tags):
        return "HIGH"
    if any(asset.entity_type in ("DASHBOARD", "CHART") for asset in assets):
        return "MEDIUM"
    return "LOW"


def build_report(model: str, assets: list[ImpactedAsset], dropped: list[str], severity: str) -> str:
    icon = {"HIGH": "\U0001f534", "MEDIUM": "\U0001f7e1", "LOW": "\U0001f7e2"}[severity]
    lines = [f"### {icon} Blast Radius: {severity} severity", ""]
    lines.append(f"**Changed model:** `{model}`")
    if dropped:
        lines.append(f"**Dropped/renamed columns:** {', '.join(f'`{c}`' for c in dropped)}")
    lines.append("")
    if assets:
        lines.append("| Affected asset | Type | Distance | Owner | Tags |")
        lines.append("|---|---|---|---|---|")
        for asset in sorted(assets, key=lambda a: a.degree):
            owners = ", ".join(asset.owners) or "\u2014"
            tags = ", ".join(asset.tags) or "\u2014"
            lines.append(f"| `{asset.name}` | {asset.entity_type.title()} | {asset.degree} | {owners} | {tags} |")
        pii = [a.name for a in assets if any("pii" in t.lower() for t in a.tags)]
        if pii:
            lines.append("")
            lines.append(f"\u26a0\ufe0f PII-tagged assets affected: {', '.join(f'`{n}`' for n in pii)} \u2014 review before merging.")
    else:
        lines.append("No downstream assets found in DataHub. \u2705")
    return "\n".join(lines)


def polish_with_llm(report: str) -> str:
    """Optionally rewrite the report for clarity. Facts must not change."""
    if not ANTHROPIC_API_KEY:
        return report
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": (
                    "Rewrite this data-impact report to be clearer for a reviewer. "
                    "Keep ALL facts, tables, asset names, and severity exactly as given. "
                    "Never invent assets.\n\n" + report
                ),
            }],
        )
        return message.content[0].text
    except Exception as exc:  # noqa: BLE001 - polishing is optional
        print(f"[warn] LLM polish skipped: {exc}", file=sys.stderr)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Lineage-aware PR impact analysis via DataHub.")
    parser.add_argument("--changed-files", nargs="+", required=True, help="Changed dbt model files")
    parser.add_argument("--output", default="report.md", help="Output Markdown file")
    args = parser.parse_args()

    sections = []
    overall = "LOW"
    for path in args.changed_files:
        model = model_name_from_path(path)
        urn = find_dataset_urn(model)
        if not urn:
            sections.append(f"### \u2754 `{model}`\n\nNot found in DataHub \u2014 is ingestion up to date?")
            continue
        assets = get_downstream_assets(urn)
        for asset in assets:
            enrich_asset(asset)
        dropped = detect_dropped_columns(path)
        severity = score_severity(assets, dropped)
        if SEVERITY_ORDER[severity] > SEVERITY_ORDER[overall]:
            overall = severity
        sections.append(build_report(model, assets, dropped, severity))

    report = polish_with_llm("\n\n---\n\n".join(sections))
    Path(args.output).write_text(report)
    print(f"Overall severity: {overall}")
    print(f"Report written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
