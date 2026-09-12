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
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

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


def get_downstream_assets(
    urn: str, page_size: int = 100, max_pages: int = 10
) -> list[ImpactedAsset]:
    """Fetch every downstream dataset/dashboard/chart from DataHub lineage with pagination.

    The original implementation used a single query with count=100, silently truncating
    large lineage graphs. This version loops with start=0,100,200... until results are
    exhausted or max_pages is reached (default 1000 assets).
    """
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
    assets: list[ImpactedAsset] = []
    seen_urns: set[str] = set()
    start = 0

    for _ in range(max_pages):
        data = gql(
            query,
            {"input": {"urn": urn, "direction": "DOWNSTREAM", "start": start, "count": page_size}},
        )
        search_results = (data.get("searchAcrossLineage") or {}).get("searchResults") or []
        if not search_results:
            break

        for result in search_results:
            entity = result["entity"]
            entity_urn = entity["urn"]
            if entity_urn in seen_urns:
                continue
            seen_urns.add(entity_urn)
            props = entity.get("properties") or {}
            # URN parsing fallback for name
            if props.get("name"):
                name = props["name"]
            else:
                # Handle URN like urn:li:dataset:(urn:li:dataPlatform:postgres,db.schema.table,PROD)
                if "," in entity_urn:
                    try:
                        name = entity_urn.split(",")[-2]
                    except Exception:
                        name = entity_urn
                else:
                    name = entity_urn
            assets.append(
                ImpactedAsset(
                    urn=entity_urn,
                    name=name,
                    entity_type=entity["type"],
                    degree=result["degree"],
                )
            )

        # If we got fewer than page_size, we're done
        if len(search_results) < page_size:
            break
        start += page_size

    return assets


def _extract_owners_tags(entity_data: dict) -> Tuple[List[str], List[str]]:
    """Helper to extract owners and tags from an entity GraphQL response."""
    owners: List[str] = []
    tags: List[str] = []
    if not entity_data:
        return owners, tags
    for owner in ((entity_data.get("ownership") or {}).get("owners") or []):
        info = owner.get("owner") or {}
        name = info.get("username") or info.get("name")
        if name:
            owners.append(name)
    for tag in ((entity_data.get("tags") or {}).get("tags") or []):
        tag_name = (tag.get("tag") or {}).get("name")
        if tag_name:
            tags.append(tag_name)
    return owners, tags


def enrich_asset(asset: ImpactedAsset) -> None:
    """Attach owners and tags to an impacted asset (best-effort, single).

    Kept for backward compatibility. For batch efficiency, use enrich_assets().
    """
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
        ... on Chart {
          ownership { owners { owner { ... on CorpUser { username } ... on CorpGroup { name } } } }
          tags { tags { tag { name } } }
        }
      }
    }
    """
    try:
        data = gql(query, {"urn": asset.urn})
        entity = data.get("entity") or {}
        owners, tags = _extract_owners_tags(entity)
        asset.owners.extend(owners)
        asset.tags.extend(tags)
    except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
        print(f"[warn] could not enrich {asset.urn}: {exc}", file=sys.stderr)


def enrich_assets(assets: List[ImpactedAsset], batch_size: int = 20) -> None:
    """Batch enrichment: fetch owners/tags for multiple URNs in single GraphQL queries.

    Instead of N+1 queries (one per asset), this batches up to batch_size URNs per
    request using GraphQL aliases (a0, a1, ...). This reduces round-trips from O(N)
    to O(N/batch_size).

    Example query generated for 2 URNs:
        query batchEnrich($urn0: String!, $urn1: String!) {
          a0: entity(urn: $urn0) { ... }
          a1: entity(urn: $urn1) { ... }
        }
    """
    if not assets:
        return

    # Shared fragment for ownership/tags across entity types
    entity_fragment = """
      ... on Dataset {
        ownership { owners { owner { ... on CorpUser { username } ... on CorpGroup { name } } } }
        tags { tags { tag { name } } }
      }
      ... on Dashboard {
        ownership { owners { owner { ... on CorpUser { username } ... on CorpGroup { name } } } }
        tags { tags { tag { name } } }
      }
      ... on Chart {
        ownership { owners { owner { ... on CorpUser { username } ... on CorpGroup { name } } } }
        tags { tags { tag { name } } }
      }
    """

    # Process in batches
    for batch_start in range(0, len(assets), batch_size):
        batch = assets[batch_start : batch_start + batch_size]
        # Build variables and query parts
        variables: Dict[str, str] = {}
        query_fields: List[str] = []
        alias_to_asset: Dict[str, ImpactedAsset] = {}

        for idx, asset in enumerate(batch):
            var_name = f"urn{idx}"
            alias = f"a{idx}"
            variables[var_name] = asset.urn
            alias_to_asset[alias] = asset
            query_fields.append(
                f"  {alias}: entity(urn: ${var_name}) {{ {entity_fragment} }}"
            )

        # Build variable definitions: ($urn0: String!, $urn1: String!, ...)
        var_defs = ", ".join([f"${k}: String!" for k in variables.keys()])
        query = f"query batchEnrich({var_defs}) {{\n" + "\n".join(query_fields) + "\n}"

        try:
            data = gql(query, variables)
            # data contains keys a0, a1, etc.
            for alias, asset in alias_to_asset.items():
                entity_data = data.get(alias) or {}
                owners, tags = _extract_owners_tags(entity_data)
                asset.owners.extend(owners)
                asset.tags.extend(tags)
        except Exception as exc:  # noqa: BLE001 - batch enrichment best-effort, fallback to single
            print(f"[warn] batch enrichment failed for batch {batch_start}: {exc}", file=sys.stderr)
            # Fallback: try single enrichment for each asset in batch
            for asset in batch:
                enrich_asset(asset)


# --------------------------------------------------------------------------
# dbt manifest.json parsing — replaces sqlglot for Jinja-safe column detection
# --------------------------------------------------------------------------

def load_manifest(manifest_path: str | Path) -> Optional[dict]:
    """Load a dbt manifest.json file, return dict or None on failure."""
    try:
        p = Path(manifest_path)
        if not p.exists():
            return None
        return json.loads(p.read_text())
    except Exception:
        return None


def load_catalog(catalog_path: str | Path) -> Optional[dict]:
    """Load a dbt catalog.json file, return dict or None."""
    try:
        p = Path(catalog_path)
        if not p.exists():
            return None
        return json.loads(p.read_text())
    except Exception:
        return None


def get_model_columns_from_manifest(
    manifest_data: dict, model_name: str
) -> Optional[Set[str]]:
    """Extract column names for a model from manifest.json.

    Looks in manifest['nodes'] for resource_type='model' and name=model_name.
    Returns set of lowercased column names, or None if not found.
    If columns dict is empty (no schema.yml), returns empty set (not None) to
    signal model was found but has no defined columns — caller may fallback to catalog.
    """
    if not manifest_data:
        return None
    nodes = manifest_data.get("nodes") or {}
    for node_id, node in nodes.items():
        # node_id like "model.jaffle_shop.stg_orders"
        if node.get("resource_type") != "model":
            continue
        if node.get("name") != model_name:
            # Also check if model file path matches? Fallback to original_file_path check
            # original_file_path: "models/staging/stg_orders.sql"
            orig_path = node.get("original_file_path") or ""
            if model_name not in Path(orig_path).stem:
                continue
        # Found matching model node
        columns = node.get("columns") or {}
        # columns is dict: {col_name: {name, description, ...}}
        if columns:
            return {col.lower() for col in columns.keys()}
        else:
            # Model found but no columns defined in manifest — return empty set
            # to allow catalog fallback
            return set()
    return None


def get_model_columns_from_catalog(
    catalog_data: dict, model_name: str
) -> Optional[Set[str]]:
    """Extract column names from catalog.json (generated via dbt docs generate).

    Catalog contains actual database columns, which is more reliable than manifest
    when schema.yml is not defined. Returns set of lowercased names or None if not found.
    """
    if not catalog_data:
        return None
    # catalog has nodes and sources
    for collection_key in ("nodes", "sources"):
        collection = catalog_data.get(collection_key) or {}
        for node_id, node in collection.items():
            # node_id like "model.jaffle_shop.stg_orders" or similar
            # Check metadata name
            metadata = node.get("metadata") or {}
            name = metadata.get("name") or node.get("name")
            if name != model_name:
                # Also try to match node_id suffix
                if not node_id.endswith(f".{model_name}"):
                    continue
            columns = node.get("columns") or {}
            if columns:
                return {col.lower() for col in columns.keys()}
    return None


def get_columns_from_dbt_artifacts(
    model_name: str,
    manifest_path: str | Path = "target/manifest.json",
    catalog_path: str | Path = "target/catalog.json",
) -> Optional[Set[str]]:
    """High-level helper: get columns from manifest.json, fallback to catalog.json.

    Architectural approach:
    - Primary source: manifest.json (contains declared columns + depends_on, fast, generated by `dbt parse`)
    - Secondary source: catalog.json (contains actual DB columns after `dbt docs generate`, more complete when schema.yml missing)
    - Returns None if neither artifact exists or model not found.
    - This avoids sqlglot parsing Jinja `{{ ref(...) }}` which previously failed.
    """
    # Try manifest first
    manifest_data = load_manifest(manifest_path)
    if manifest_data is not None:
        cols = get_model_columns_from_manifest(manifest_data, model_name)
        if cols:
            return cols
        # If cols is empty set, try catalog before returning empty
        if cols is not None and len(cols) == 0:
            catalog_data = load_catalog(catalog_path)
            catalog_cols = get_model_columns_from_catalog(catalog_data, model_name) if catalog_data else None
            if catalog_cols:
                return catalog_cols
            # If catalog also empty, return empty set (model found but no columns)
            return cols
    # Manifest not found or model not in manifest, try catalog directly
    catalog_data = load_catalog(catalog_path)
    if catalog_data is not None:
        cols = get_model_columns_from_catalog(catalog_data, model_name)
        if cols is not None:
            return cols
    return None


def get_old_manifest_from_git(base_ref: str, manifest_path_in_repo: str = "target/manifest.json") -> Optional[Path]:
    """Try to retrieve old manifest.json from git history (BASE_REF).

    Returns Path to temp file containing old manifest, or None if not found.
    This enables diffing columns across PRs when manifest.json is not gitignored
    or when we generate old manifest on the fly in CI.
    """
    try:
        old_content = subprocess.run(
            ["git", "show", f"{base_ref}:{manifest_path_in_repo}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        if not old_content.strip():
            return None
        # Validate JSON
        json.loads(old_content)
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        tmp.write(old_content)
        tmp.close()
        return Path(tmp.name)
    except Exception:
        return None


def detect_dropped_columns_from_manifests(
    model_name: str,
    old_manifest_path: str | Path,
    new_manifest_path: str | Path,
    old_catalog_path: Optional[str | Path] = None,
    new_catalog_path: Optional[str | Path] = None,
) -> Optional[List[str]]:
    """Diff columns using two manifest.json files (and optionally catalogs).

    Returns list of dropped columns (old - new) sorted, or None if either manifest
    doesn't contain the model. Returns [] if no columns dropped.
    """
    old_cols = get_columns_from_dbt_artifacts(
        model_name, manifest_path=old_manifest_path, catalog_path=old_catalog_path or "target/catalog.json"
    )
    new_cols = get_columns_from_dbt_artifacts(
        model_name, manifest_path=new_manifest_path, catalog_path=new_catalog_path or "target/catalog.json"
    )
    if old_cols is None or new_cols is None:
        return None
    dropped = sorted(old_cols - new_cols)
    return dropped


def _detect_dropped_columns_sqlglot(path: str, base_ref: str) -> List[str]:
    """Legacy sqlglot-based detection (kept as fallback)."""
    try:
        import sqlglot

        old_sql = subprocess.run(
            ["git", "show", f"{base_ref}:{path}"],
            capture_output=True,
            text=True,
            check=True,
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


def detect_dropped_columns(
    path: str,
    manifest_path: str | Path = "target/manifest.json",
    catalog_path: str | Path = "target/catalog.json",
    base_ref: str | None = None,
) -> List[str]:
    """Detect dropped columns for a model file.

    Primary strategy (new): Use dbt artifacts (manifest.json + catalog.json).
    - Loads new columns from target/manifest.json or target/catalog.json
    - Attempts to load old columns from BASE_REF:target/manifest.json via git show
    - If both available, diffs them.

    Fallback strategy (legacy): Use sqlglot to parse old vs new SQL.
    This handles cases where manifest is not yet generated (local dev without dbt run).

    Args:
        path: Path to changed dbt model file (e.g., models/staging/stg_orders.sql)
        manifest_path: Path to new manifest.json (default target/manifest.json)
        catalog_path: Path to new catalog.json (default target/catalog.json)
        base_ref: Git ref to diff against (defaults to env BASE_REF)

    Returns:
        List of dropped column names (lowercased, sorted). Empty list if none or on failure.
    """
    if base_ref is None:
        base_ref = BASE_REF

    model = model_name_from_path(path)

    # Attempt manifest-based detection first
    try:
        new_cols = get_columns_from_dbt_artifacts(model, manifest_path=manifest_path, catalog_path=catalog_path)
        if new_cols is not None:
            # Try to get old manifest from git
            old_manifest_tmp = get_old_manifest_from_git(base_ref, str(manifest_path))
            old_catalog_tmp = None
            # Also try old catalog from git if exists
            try:
                old_catalog_content = subprocess.run(
                    ["git", "show", f"{base_ref}:{catalog_path}"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                if old_catalog_content.strip():
                    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
                    tmp.write(old_catalog_content)
                    tmp.close()
                    old_catalog_tmp = Path(tmp.name)
            except Exception:
                old_catalog_tmp = None

            if old_manifest_tmp is not None or old_catalog_tmp is not None:
                old_manifest_path = old_manifest_tmp or manifest_path
                old_catalog_path_eff = old_catalog_tmp or catalog_path
                old_cols = get_columns_from_dbt_artifacts(
                    model,
                    manifest_path=old_manifest_path,
                    catalog_path=old_catalog_path_eff,
                )
                # Cleanup temp files
                try:
                    if old_manifest_tmp and old_manifest_tmp.exists():
                        old_manifest_tmp.unlink()
                    if old_catalog_tmp and old_catalog_tmp.exists():
                        old_catalog_tmp.unlink()
                except Exception:
                    pass

                if old_cols is not None:
                    dropped = sorted(old_cols - new_cols)
                    # If we found a diff via manifest, return it (even if empty)
                    # But only if old_cols was non-empty or we are confident
                    # To avoid false negatives when manifest had no columns, fallback to sqlglot if both empty?
                    if old_cols or new_cols:
                        return dropped
    except Exception as exc:  # noqa: BLE001 - manifest parsing best-effort
        print(f"[warn] manifest-based column diff failed for {model}: {exc}", file=sys.stderr)

    # Fallback to sqlglot
    return _detect_dropped_columns_sqlglot(path, base_ref)


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
    parser.add_argument("--manifest", default="target/manifest.json", help="Path to dbt manifest.json")
    parser.add_argument("--catalog", default="target/catalog.json", help="Path to dbt catalog.json (optional)")
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
        # Use batched enrichment instead of N+1
        enrich_assets(assets)
        dropped = detect_dropped_columns(path, manifest_path=args.manifest, catalog_path=args.catalog)
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
