#!/usr/bin/env python3
"""Blast Radius — lineage-aware impact analysis for dbt pull requests.

Given a list of changed dbt model files, this script:
  1. Resolves each model to its DataHub dataset URN (or mock URN if DataHub not configured).
  2. Fetches downstream lineage, owners, and tags from DataHub (or mock data).
  3. Scores the severity of the change with simple, explainable rules.
  4. Writes a Markdown impact report (optionally polished by an LLM).
  5. Optionally raises DataHub incidents, requests GitHub reviewers, and sends Slack alerts.

Usage:
    python blast_radius.py --changed-files models/staging/stg_orders.sql --output report.md

Mock Mode:
    If DATAHUB_URL is missing, empty, or invalid (e.g. '/api/graphql'), the script
    automatically enters mock mode, bypassing real GraphQL calls and generating
    realistic mock impact reports so CI never crashes.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests

# --------------------------------------------------------------------------
# Environment & Configuration — with graceful fallback for missing vars
# --------------------------------------------------------------------------

def _get_env(name: str, default: str = "") -> str:
    """Get env var, treat empty string as missing (returns default)."""
    val = os.environ.get(name, default)
    # GitHub Actions secrets that are not set expand to empty string
    if val == "":
        return default
    return val

DATAHUB_URL = _get_env("DATAHUB_URL", "").rstrip("/")
DATAHUB_TOKEN = _get_env("DATAHUB_TOKEN", "")
ANTHROPIC_API_KEY = _get_env("ANTHROPIC_API_KEY", "")
BASE_REF = _get_env("BASE_REF", "origin/main")
SLACK_WEBHOOK_URL = _get_env("SLACK_WEBHOOK_URL", "")
GITHUB_TOKEN = _get_env("GITHUB_TOKEN", "") or _get_env("GH_TOKEN", "")
GITHUB_REPOSITORY = _get_env("GITHUB_REPOSITORY", "")
# PR number can come from multiple sources in GH Actions
GITHUB_PR_NUMBER = _get_env("GITHUB_PR_NUMBER", "")

# Re-compute endpoint only if URL is valid, otherwise placeholder
GRAPHQL_ENDPOINT = f"{DATAHUB_URL}/api/graphql" if DATAHUB_URL else "/api/graphql"

SEVERITY_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

# Global mock flag, computed lazily via function
_MOCK_MODE_CACHE: Optional[bool] = None


def is_mock_mode() -> bool:
    """Determine if we should run in mock mode (no real DataHub).

    Mock mode is triggered if:
    - DATAHUB_URL env is missing/empty
    - DATAHUB_URL doesn't start with http:// or https://
    - Explicit BLAST_RADIUS_MOCK=true
    - Running in GitHub Actions with localhost URL (no tunnel)
    """
    global _MOCK_MODE_CACHE
    if _MOCK_MODE_CACHE is not None:
        return _MOCK_MODE_CACHE

    explicit_mock = os.environ.get("BLAST_RADIUS_MOCK", "").lower() in ("1", "true", "yes")
    if explicit_mock:
        _MOCK_MODE_CACHE = True
        return True

    url = os.environ.get("DATAHUB_URL", "").strip()
    if not url:
        _MOCK_MODE_CACHE = True
        return True
    if url in ("/api/graphql",):
        _MOCK_MODE_CACHE = True
        return True
    if not (url.startswith("http://") or url.startswith("https://")):
        _MOCK_MODE_CACHE = True
        return True
    # In GitHub Actions, localhost URLs are not reachable unless ngrok is set up
    if os.environ.get("GITHUB_ACTIONS") == "true":
        if "localhost" in url or "127.0.0.1" in url:
            # If user explicitly set localhost in CI, treat as mock unless they also set a tunnel
            # We check for ngrok or similar in URL, otherwise mock
            if "ngrok" not in url and "trycloudflare" not in url:
                print("[warn] DATAHUB_URL is localhost in CI — entering mock mode (use ngrok for real DataHub)", file=sys.stderr)
                _MOCK_MODE_CACHE = True
                return True

    _MOCK_MODE_CACHE = False
    return False


def reset_mock_cache():
    """For testing: reset cached mock mode decision."""
    global _MOCK_MODE_CACHE
    _MOCK_MODE_CACHE = None


@dataclass
class ImpactedAsset:
    urn: str
    name: str
    entity_type: str
    degree: int
    owners: list = field(default_factory=list)
    tags: list = field(default_factory=list)


# --------------------------------------------------------------------------
# Mock data generation — realistic downstream graph for demo
# --------------------------------------------------------------------------

MOCK_LINEAGE = {
    "stg_orders": [
        {"name": "revenue_daily", "type": "DATASET", "degree": 1, "owners": ["alice"], "tags": []},
        {"name": "Executive Revenue Dashboard", "type": "DASHBOARD", "degree": 2, "owners": ["alice"], "tags": []},
        {"name": "Customer 360", "type": "DASHBOARD", "degree": 2, "owners": ["bob"], "tags": []},
    ],
    "stg_customers": [
        {"name": "Customer 360", "type": "DASHBOARD", "degree": 1, "owners": ["bob"], "tags": ["PII"]},
        {"name": "customer_segments", "type": "DATASET", "degree": 1, "owners": ["bob"], "tags": ["PII"]},
    ],
    "stg_payments": [
        {"name": "revenue_daily", "type": "DATASET", "degree": 1, "owners": ["alice"], "tags": []},
        {"name": "Executive Revenue Dashboard", "type": "DASHBOARD", "degree": 2, "owners": ["alice"], "tags": []},
    ],
    # Default for unknown models
    "__default__": [
        {"name": "revenue_daily", "type": "DATASET", "degree": 1, "owners": ["alice"], "tags": []},
        {"name": "Executive Revenue Dashboard", "type": "DASHBOARD", "degree": 2, "owners": ["alice"], "tags": []},
    ],
}


def get_mock_assets_for_model(model_name: str) -> List[ImpactedAsset]:
    """Generate realistic mock downstream assets for a given model."""
    mock_defs = MOCK_LINEAGE.get(model_name, MOCK_LINEAGE["__default__"])
    assets: List[ImpactedAsset] = []
    for idx, mock in enumerate(mock_defs):
        # Generate realistic URNs
        if mock["type"] == "DATASET":
            urn = f"urn:li:dataset:(urn:li:dataPlatform:postgres,jaffle_shop.{mock['name']},PROD)"
        elif mock["type"] == "DASHBOARD":
            # Dashboard URN: platform + id
            dash_id = mock["name"].lower().replace(" ", "_")
            urn = f"urn:li:dashboard:(looker,{dash_id})"
        else:  # CHART
            chart_id = mock["name"].lower().replace(" ", "_")
            urn = f"urn:li:chart:(looker,{chart_id})"
        assets.append(
            ImpactedAsset(
                urn=urn,
                name=mock["name"],
                entity_type=mock["type"],
                degree=mock["degree"],
                owners=mock.get("owners", []),
                tags=mock.get("tags", []),
            )
        )
    return assets


def get_mock_dataset_urn(model_name: str) -> str:
    """Generate mock dataset URN for a model."""
    return f"urn:li:dataset:(urn:li:dataPlatform:postgres,jaffle_shop.{model_name},PROD)"


# --------------------------------------------------------------------------
# GraphQL client — now with mock mode safety
# --------------------------------------------------------------------------

def gql(query: str, variables: dict) -> dict:
    """Send a GraphQL request to DataHub and return the `data` payload.

    In mock mode, raises RuntimeError to prevent accidental use — callers
    should check is_mock_mode() first and use mock data.
    """
    if is_mock_mode():
        raise RuntimeError("gql() called in mock mode — use mock data instead")

    # Re-compute endpoint from current env (in case env changed after import)
    current_url = os.environ.get("DATAHUB_URL", "").strip()
    if not current_url or not (current_url.startswith("http://") or current_url.startswith("https://")):
        raise RuntimeError(f"DATAHUB_URL is not configured or invalid: '{current_url}' — cannot call DataHub")

    endpoint = f"{current_url.rstrip('/')}/api/graphql"
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("DATAHUB_TOKEN", "") or DATAHUB_TOKEN
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.post(
        endpoint,
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
    """Resolve a dbt model name to a DataHub dataset URN via search.

    In mock mode, returns a deterministic mock URN.
    """
    if is_mock_mode():
        print(f"[mock] find_dataset_urn({model_name}) -> mock URN", file=sys.stderr)
        return get_mock_dataset_urn(model_name)

    query = """
    query search($input: SearchInput!) {
      search(input: $input) {
        searchResults { entity { urn type } }
      }
    }
    """
    try:
        data = gql(query, {"input": {"type": "DATASET", "query": model_name, "start": 0, "count": 5}})
        results = (data.get("search") or {}).get("searchResults") or []
        for result in results:
            urn = result["entity"]["urn"]
            if model_name.lower() in urn.lower():
                return urn
        return results[0]["entity"]["urn"] if results else None
    except Exception as exc:
        print(f"[warn] find_dataset_urn failed for {model_name}: {exc} — falling back to mock URN", file=sys.stderr)
        return get_mock_dataset_urn(model_name)


def get_downstream_assets(
    urn: str, page_size: int = 100, max_pages: int = 10
) -> list[ImpactedAsset]:
    """Fetch every downstream dataset/dashboard/chart from DataHub lineage with pagination.

    In mock mode, parses model name from URN and returns mock assets.
    """
    if is_mock_mode():
        # Try to extract model name from URN: urn:li:dataset:(...,jaffle_shop.stg_orders,PROD) -> stg_orders
        model_name = "unknown"
        try:
            # URN format: urn:li:dataset:(platform,name,env)
            if "," in urn:
                parts = urn.split(",")
                if len(parts) >= 2:
                    # second last part is like "jaffle_shop.stg_orders" or "db.table"
                    candidate = parts[-2]
                    # Take last segment after dot
                    if "." in candidate:
                        model_name = candidate.split(".")[-1]
                    else:
                        model_name = candidate
        except Exception:
            pass
        print(f"[mock] get_downstream_assets for {urn} (model={model_name}) -> mock data", file=sys.stderr)
        return get_mock_assets_for_model(model_name)

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
        try:
            data = gql(
                query,
                {"input": {"urn": urn, "direction": "DOWNSTREAM", "start": start, "count": page_size}},
            )
        except Exception as exc:
            print(f"[warn] get_downstream_assets failed at start={start}: {exc}", file=sys.stderr)
            break

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
            if props.get("name"):
                name = props["name"]
            else:
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

    In mock mode, assets already have owners/tags, so this is a no-op.
    """
    if is_mock_mode():
        return

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

    In mock mode, this is a no-op (mock assets already enriched).
    """
    if not assets:
        return
    if is_mock_mode():
        print(f"[mock] enrich_assets({len(assets)} assets) — already enriched in mock mode", file=sys.stderr)
        return

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

    for batch_start in range(0, len(assets), batch_size):
        batch = assets[batch_start : batch_start + batch_size]
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

        var_defs = ", ".join([f"${k}: String!" for k in variables.keys()])
        query = f"query batchEnrich({var_defs}) {{\n" + "\n".join(query_fields) + "\n}"

        try:
            data = gql(query, variables)
            for alias, asset in alias_to_asset.items():
                entity_data = data.get(alias) or {}
                owners, tags = _extract_owners_tags(entity_data)
                asset.owners.extend(owners)
                asset.tags.extend(tags)
        except Exception as exc:  # noqa: BLE001 - batch enrichment best-effort, fallback to single
            print(f"[warn] batch enrichment failed for batch {batch_start}: {exc}", file=sys.stderr)
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
    """Extract column names for a model from manifest.json."""
    if not manifest_data:
        return None
    nodes = manifest_data.get("nodes") or {}
    for node_id, node in nodes.items():
        if node.get("resource_type") != "model":
            continue
        if node.get("name") != model_name:
            orig_path = node.get("original_file_path") or ""
            if model_name not in Path(orig_path).stem:
                continue
        columns = node.get("columns") or {}
        if columns:
            return {col.lower() for col in columns.keys()}
        else:
            return set()
    return None


def get_model_columns_from_catalog(
    catalog_data: dict, model_name: str
) -> Optional[Set[str]]:
    """Extract column names from catalog.json."""
    if not catalog_data:
        return None
    for collection_key in ("nodes", "sources"):
        collection = catalog_data.get(collection_key) or {}
        for node_id, node in collection.items():
            metadata = node.get("metadata") or {}
            name = metadata.get("name") or node.get("name")
            if name != model_name:
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
    """High-level helper: get columns from manifest.json, fallback to catalog.json."""
    manifest_data = load_manifest(manifest_path)
    if manifest_data is not None:
        cols = get_model_columns_from_manifest(manifest_data, model_name)
        if cols:
            return cols
        if cols is not None and len(cols) == 0:
            catalog_data = load_catalog(catalog_path)
            catalog_cols = get_model_columns_from_catalog(catalog_data, model_name) if catalog_data else None
            if catalog_cols:
                return catalog_cols
            return cols
    catalog_data = load_catalog(catalog_path)
    if catalog_data is not None:
        cols = get_model_columns_from_catalog(catalog_data, model_name)
        if cols is not None:
            return cols
    return None


def get_old_manifest_from_git(base_ref: str, manifest_path_in_repo: str = "target/manifest.json") -> Optional[Path]:
    """Try to retrieve old manifest.json from git history (BASE_REF)."""
    try:
        old_content = subprocess.run(
            ["git", "show", f"{base_ref}:{manifest_path_in_repo}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        if not old_content.strip():
            return None
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
    """Diff columns using two manifest.json files (and optionally catalogs)."""
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

    Primary: manifest.json + catalog.json
    Fallback: sqlglot
    """
    if base_ref is None:
        base_ref = BASE_REF or "origin/main"

    model = model_name_from_path(path)

    try:
        new_cols = get_columns_from_dbt_artifacts(model, manifest_path=manifest_path, catalog_path=catalog_path)
        if new_cols is not None:
            old_manifest_tmp = get_old_manifest_from_git(base_ref, str(manifest_path))
            old_catalog_tmp = None
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
                try:
                    if old_manifest_tmp and old_manifest_tmp.exists():
                        old_manifest_tmp.unlink()
                    if old_catalog_tmp and old_catalog_tmp.exists():
                        old_catalog_tmp.unlink()
                except Exception:
                    pass

                if old_cols is not None:
                    dropped = sorted(old_cols - new_cols)
                    if old_cols or new_cols:
                        return dropped
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] manifest-based column diff failed for {model}: {exc}", file=sys.stderr)

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
    if is_mock_mode():
        lines.append("> _Running in **mock mode** (DATAHUB_URL not configured) — using realistic demo data._")
        lines.append("")
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


# --------------------------------------------------------------------------
# Priority 2: DataHub Incident Write-back
# --------------------------------------------------------------------------

def raise_datahub_incidents(
    assets: List[ImpactedAsset],
    model_name: str,
    severity: str,
    dropped_columns: List[str],
    pr_url: str = "",
) -> None:
    """Raise DataHub incidents for HIGH severity assets.

    Safely bypasses if DATAHUB_URL missing or mock mode.
    Uses DataHub's IncidentInfo aspect via RestEmitter.
    """
    if severity != "HIGH":
        return
    if not assets:
        return
    if is_mock_mode():
        print(f"[mock] Would raise DataHub incidents for {len(assets)} HIGH severity assets (model={model_name})", file=sys.stderr)
        for asset in assets:
            print(f"[mock] Incident: {asset.name} ({asset.urn}) — dropped {dropped_columns}", file=sys.stderr)
        return

    # Check if DataHub is configured
    current_url = os.environ.get("DATAHUB_URL", "").strip() or DATAHUB_URL
    if not current_url or not (current_url.startswith("http://") or current_url.startswith("https://")):
        print("[warn] DATAHUB_URL not configured — skipping incident write-back", file=sys.stderr)
        return

    try:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.emitter.rest_emitter import DatahubRestEmitter
        import datahub.emitter.mce_builder as builder
        import datahub.metadata.schema_classes as models

        emitter = DatahubRestEmitter(gms_server=current_url, token=DATAHUB_TOKEN or None)

        for asset in assets:
            try:
                # Generate deterministic but unique incident ID
                incident_id = hashlib.md5(
                    f"{asset.urn}:{model_name}:{','.join(dropped_columns)}:{time.time()}".encode()
                ).hexdigest()[:16]
                incident_urn = f"urn:li:incident:{incident_id}"

                now_ms = int(time.time() * 1000)
                created = models.AuditStampClass(time=now_ms, actor=builder.make_user_urn("blast-radius"))
                status = models.IncidentStatusClass(
                    state=models.IncidentStateClass.ACTIVE,
                    lastUpdated=created,
                )
                source = models.IncidentSourceClass(
                    type=models.IncidentSourceTypeClass.MANUAL,
                )

                title = f"Blast Radius HIGH: {model_name} impacts {asset.name}"
                description_parts = [
                    f"Model `{model_name}` change flagged as HIGH severity.",
                    f"Affected asset: `{asset.name}` ({asset.entity_type}, degree {asset.degree}).",
                ]
                if dropped_columns:
                    description_parts.append(f"Dropped columns: {', '.join(f'`{c}`' for c in dropped_columns)}.")
                if asset.tags:
                    description_parts.append(f"Tags: {', '.join(asset.tags)}.")
                if pr_url:
                    description_parts.append(f"PR: {pr_url}")
                description = " ".join(description_parts)

                incident_info = models.IncidentInfoClass(
                    type=models.IncidentTypeClass.CUSTOM,
                    customType="blast-radius/high-severity",
                    title=title,
                    description=description,
                    entities=[asset.urn],
                    status=status,
                    source=source,
                    priority=1,  # 0 CRITICAL, 1 HIGH, 2 MED, 3 LOW
                    created=created,
                )

                mcp = MetadataChangeProposalWrapper(entityUrn=incident_urn, aspect=incident_info)
                emitter.emit(mcp)
                print(f"[info] Raised DataHub incident {incident_urn} for {asset.urn}", file=sys.stderr)

            except Exception as inner_exc:
                print(f"[warn] Failed to raise incident for {asset.urn}: {inner_exc}", file=sys.stderr)
                continue

    except Exception as exc:
        print(f"[warn] DataHub incident write-back failed: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# Priority 2: Auto-Request PR Reviewers
# --------------------------------------------------------------------------

def load_owner_mapping(mapping_path: str | Path = "owner_mapping.json") -> Dict[str, str]:
    """Load DataHub username -> GitHub username mapping.

    Supports:
    - owner_mapping.json file in repo root
    - github_owners.json fallback
    - OWNER_MAPPING env var as JSON string
    - Built-in demo mapping for alice/bob

    Returns dict like {"alice": "alice-github", "bob": "bob-github"}
    """
    # Built-in demo mapping
    mapping: Dict[str, str] = {
        "alice": "alice",
        "bob": "bob",
        "datahub": "datahub",
    }

    # Try env var first
    env_mapping = os.environ.get("OWNER_MAPPING", "").strip()
    if env_mapping:
        try:
            env_dict = json.loads(env_mapping)
            if isinstance(env_dict, dict):
                mapping.update(env_dict)
        except Exception as exc:
            print(f"[warn] Failed to parse OWNER_MAPPING env var: {exc}", file=sys.stderr)

    # Try files
    for candidate in [mapping_path, "github_owners.json", "OWNERS.json", ".github/owner_mapping.json"]:
        try:
            p = Path(candidate)
            if p.exists():
                data = json.loads(p.read_text())
                if isinstance(data, dict):
                    mapping.update(data)
                    print(f"[info] Loaded owner mapping from {candidate}: {len(data)} entries", file=sys.stderr)
                    break
        except Exception as exc:
            print(f"[warn] Failed to load owner mapping from {candidate}: {exc}", file=sys.stderr)
            continue

    return mapping


def map_datahub_owners_to_github(datahub_owners: List[str]) -> List[str]:
    """Map DataHub usernames to GitHub usernames using mapping file."""
    if not datahub_owners:
        return []
    mapping = load_owner_mapping()
    github_users: List[str] = []
    for owner in datahub_owners:
        # Normalize: strip @, lowercase for lookup, but preserve case for output?
        clean_owner = owner.lstrip("@").strip()
        github_user = mapping.get(clean_owner, mapping.get(clean_owner.lower(), clean_owner))
        # GitHub usernames cannot contain spaces, validate
        if github_user and " " not in github_user:
            github_users.append(github_user)
    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for u in github_users:
        if u.lower() not in seen:
            seen.add(u.lower())
            deduped.append(u)
    return deduped


def get_github_pr_info() -> Tuple[Optional[str], Optional[int]]:
    """Extract GitHub repo and PR number from environment.

    Returns (repo, pr_number) like ("mraaisa-afk/blast-radius", 123)
    or (None, None) if not in GH Actions context.
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip() or GITHUB_REPOSITORY
    pr_number: Optional[int] = None

    # Try explicit env var
    pr_num_str = os.environ.get("GITHUB_PR_NUMBER", "").strip() or os.environ.get("PR_NUMBER", "").strip()
    if pr_num_str.isdigit():
        pr_number = int(pr_num_str)

    # Try GITHUB_REF like refs/pull/123/merge
    if pr_number is None:
        ref = os.environ.get("GITHUB_REF", "")
        if "refs/pull/" in ref:
            try:
                # refs/pull/123/merge -> 123
                parts = ref.split("/")
                idx = parts.index("pull")
                pr_number = int(parts[idx + 1])
            except Exception:
                pass

    # Try GITHUB_EVENT_PATH JSON file
    if pr_number is None:
        event_path = os.environ.get("GITHUB_EVENT_PATH", "")
        if event_path and Path(event_path).exists():
            try:
                event_data = json.loads(Path(event_path).read_text())
                pr_number = event_data.get("pull_request", {}).get("number")
                if not pr_number:
                    pr_number = event_data.get("number")
                if pr_number:
                    pr_number = int(pr_number)
                # Also try to get repo from event
                if not repo:
                    repo = event_data.get("repository", {}).get("full_name", "")
            except Exception:
                pass

    if not repo:
        repo = None
    return repo, pr_number


def request_github_reviewers(
    github_users: List[str],
    repo: Optional[str] = None,
    pr_number: Optional[int] = None,
    token: Optional[str] = None,
) -> bool:
    """Request PR reviewers via GitHub API.

    Safely bypasses if token/repo/pr_number missing. Returns True if successful.
    If token is explicitly passed as empty string, it is treated as no token (no fallback).
    If token is None, we fallback to env vars.
    """
    if not github_users:
        print("[info] No GitHub reviewers to request", file=sys.stderr)
        return False

    # Resolve repo and PR number if not provided
    env_repo, env_pr = get_github_pr_info()
    repo = repo or env_repo
    pr_number = pr_number or env_pr

    # Token handling: explicit empty string means no token, None means fallback to env
    if token is None:
        token = GITHUB_TOKEN or os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")
    # If token is still empty after fallback, treat as missing

    if not token:
        print(f"[mock] Would request reviewers {github_users} for PR {pr_number} in {repo} — no GITHUB_TOKEN", file=sys.stderr)
        return False
    if not repo or not pr_number:
        print(f"[mock] Would request reviewers {github_users} — missing repo ({repo}) or PR number ({pr_number})", file=sys.stderr)
        return False

    # GitHub API: POST /repos/{owner}/{repo}/pulls/{pr}/requested_reviewers
    # Need to filter out the PR author? For simplicity, request all
    # Also need to ensure users are valid collaborators — API will error if not, so we try best-effort
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/requested_reviewers"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"reviewers": github_users}

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code in (200, 201):
            print(f"[info] Successfully requested reviewers {github_users} for PR #{pr_number}", file=sys.stderr)
            return True
        else:
            # 422 often means user is not a collaborator or already requested
            print(f"[warn] GitHub reviewer request failed: {resp.status_code} {resp.text}", file=sys.stderr)
            # Try to request team reviewers if users fail? For now just return False
            return False
    except Exception as exc:
        print(f"[warn] Failed to request GitHub reviewers: {exc}", file=sys.stderr)
        return False


def auto_request_reviewers_from_assets(assets: List[ImpactedAsset]) -> None:
    """Collect owners from assets and auto-request PR reviewers."""
    if not assets:
        return
    # Collect all owners
    all_owners: List[str] = []
    for asset in assets:
        all_owners.extend(asset.owners)
    # Deduplicate
    unique_owners = list(dict.fromkeys(all_owners))
    if not unique_owners:
        print("[info] No owners found in impacted assets — skipping reviewer request", file=sys.stderr)
        return

    github_users = map_datahub_owners_to_github(unique_owners)
    if not github_users:
        print(f"[info] No GitHub mapping for DataHub owners {unique_owners}", file=sys.stderr)
        return

    print(f"[info] DataHub owners {unique_owners} -> GitHub users {github_users}", file=sys.stderr)
    request_github_reviewers(github_users)


# --------------------------------------------------------------------------
# Priority 2: Slack Notifications
# --------------------------------------------------------------------------

def send_slack_notification(
    severity: str,
    model: str,
    assets: List[ImpactedAsset],
    dropped_columns: List[str],
    pr_url: str = "",
    overall_severity: str = "",
) -> None:
    """Send Slack webhook notification for HIGH severity changes.

    Safely bypasses if SLACK_WEBHOOK_URL missing. Wrapped in try-except so it never crashes.
    """
    try:
        if severity != "HIGH" and overall_severity != "HIGH":
            # Only notify on HIGH per spec, but allow overall HIGH
            return

        webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip() or SLACK_WEBHOOK_URL
        if not webhook_url:
            print(f"[mock] Would send Slack alert for HIGH severity {model} — no SLACK_WEBHOOK_URL configured", file=sys.stderr)
            return

        if not (webhook_url.startswith("http://") or webhook_url.startswith("https://")):
            print(f"[warn] Invalid SLACK_WEBHOOK_URL: {webhook_url}", file=sys.stderr)
            return

        # Build Slack payload — use simple text + blocks for rich formatting
        affected_summary = ", ".join([f"`{a.name}`" for a in assets[:5]])
        if len(assets) > 5:
            affected_summary += f" +{len(assets)-5} more"

        dropped_str = ", ".join([f"`{c}`" for c in dropped_columns]) if dropped_columns else "none"
        pii_assets = [a.name for a in assets if any("pii" in t.lower() for t in a.tags)]
        pii_warning = f" ⚠️ PII affected: {', '.join(pii_assets)}" if pii_assets else ""

        text = f"🔴 Blast Radius HIGH severity: `{model}` change impacts {len(assets)} assets"

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🔴 Blast Radius: HIGH severity — {model}", "emoji": True},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Changed model:* `{model}`\n*Dropped columns:* {dropped_str}\n*Affected assets ({len(assets)}):* {affected_summary}{pii_warning}\n*Severity:* {severity}",
                },
            },
        ]
        if pr_url:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"<{pr_url}|View PR>"},
                }
            )

        payload = {
            "text": text,
            "blocks": blocks,
        }

        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"[info] Slack notification sent for {model} HIGH severity", file=sys.stderr)
        else:
            print(f"[warn] Slack webhook failed: {resp.status_code} {resp.text}", file=sys.stderr)

    except Exception as exc:
        # Never crash the main flow due to Slack
        print(f"[warn] Slack notification failed (non-critical): {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# Main orchestration
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Lineage-aware PR impact analysis via DataHub.")
    parser.add_argument("--changed-files", nargs="+", required=True, help="Changed dbt model files")
    parser.add_argument("--output", default="report.md", help="Output Markdown file")
    parser.add_argument("--manifest", default="target/manifest.json", help="Path to dbt manifest.json")
    parser.add_argument("--catalog", default="target/catalog.json", help="Path to dbt catalog.json (optional)")
    parser.add_argument("--pr-url", default=os.environ.get("PR_URL", "") or os.environ.get("GITHUB_PR_URL", ""), help="PR URL for incident/Slack context")
    parser.add_argument("--mock", action="store_true", help="Force mock mode (no DataHub calls)")
    args = parser.parse_args()

    if args.mock:
        os.environ["BLAST_RADIUS_MOCK"] = "true"
        reset_mock_cache()

    # Determine mode early
    mock_mode = is_mock_mode()
    if mock_mode:
        print("[info] Running in MOCK MODE — DATAHUB_URL not configured or invalid, using demo data", file=sys.stderr)
        print(f"[info] DATAHUB_URL='{os.environ.get('DATAHUB_URL','')}'", file=sys.stderr)
    else:
        print(f"[info] Running in LIVE MODE — DATAHUB_URL={DATAHUB_URL}", file=sys.stderr)

    sections = []
    overall = "LOW"
    all_assets_for_review: List[ImpactedAsset] = []
    pr_url = args.pr_url or os.environ.get("GITHUB_PR_URL", "") or ""

    # Try to get PR URL from GitHub context if not provided
    if not pr_url and os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_REF"):
        repo = os.environ.get("GITHUB_REPOSITORY")
        ref = os.environ.get("GITHUB_REF", "")
        if "refs/pull/" in ref:
            try:
                pr_num = ref.split("/")[2]
                pr_url = f"https://github.com/{repo}/pull/{pr_num}"
            except Exception:
                pass

    for path in args.changed_files:
        model = model_name_from_path(path)

        # Resolve URN (mock or real)
        try:
            urn = find_dataset_urn(model)
        except Exception as exc:
            print(f"[warn] find_dataset_urn failed for {model}: {exc}, using mock URN", file=sys.stderr)
            urn = get_mock_dataset_urn(model)

        if not urn:
            sections.append(f"### \u2754 `{model}`\n\nNot found in DataHub \u2014 is ingestion up to date?")
            continue

        # Get downstream assets
        try:
            assets = get_downstream_assets(urn)
        except Exception as exc:
            print(f"[warn] get_downstream_assets failed for {model}: {exc}, using mock assets", file=sys.stderr)
            assets = get_mock_assets_for_model(model)

        # Enrich (no-op in mock mode)
        try:
            enrich_assets(assets)
        except Exception as exc:
            print(f"[warn] enrich_assets failed: {exc}", file=sys.stderr)

        # Dropped columns (works without DataHub)
        try:
            dropped = detect_dropped_columns(path, manifest_path=args.manifest, catalog_path=args.catalog)
        except Exception as exc:
            print(f"[warn] detect_dropped_columns failed for {path}: {exc}", file=sys.stderr)
            dropped = []

        severity = score_severity(assets, dropped)
        if SEVERITY_ORDER[severity] > SEVERITY_ORDER[overall]:
            overall = severity

        sections.append(build_report(model, assets, dropped, severity))

        # Priority 2: Incident write-back and Slack for HIGH
        if severity == "HIGH":
            try:
                raise_datahub_incidents(assets, model, severity, dropped, pr_url=pr_url)
            except Exception as exc:
                print(f"[warn] Incident write-back failed: {exc}", file=sys.stderr)
            try:
                send_slack_notification(severity, model, assets, dropped, pr_url=pr_url, overall_severity=overall)
            except Exception as exc:
                print(f"[warn] Slack notification failed: {exc}", file=sys.stderr)

        # Collect assets for reviewer request
        all_assets_for_review.extend(assets)

    # Auto-request reviewers after all models processed (overall)
    if all_assets_for_review:
        try:
            auto_request_reviewers_from_assets(all_assets_for_review)
        except Exception as exc:
            print(f"[warn] Auto-request reviewers failed: {exc}", file=sys.stderr)

    # Overall Slack notification if overall HIGH (single notification for PR)
    if overall == "HIGH" and len(args.changed_files) > 1:
        try:
            # Send summary Slack for overall PR
            send_slack_notification(
                severity=overall,
                model=", ".join([model_name_from_path(p) for p in args.changed_files]),
                assets=all_assets_for_review,
                dropped_columns=[],
                pr_url=pr_url,
                overall_severity=overall,
            )
        except Exception:
            pass

    report = polish_with_llm("\n\n---\n\n".join(sections))
    Path(args.output).write_text(report)
    print(f"Overall severity: {overall} (mock_mode={mock_mode})")
    print(f"Report written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
