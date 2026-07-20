#!/usr/bin/env python3
"""Emit demo dashboards, owners, and PII tags into DataHub.

Run AFTER `datahub ingest -c dbt_recipe.yml` so the dbt models already exist.
This script decorates the ingested graph so the Blast Radius demo has a real
downstream story:

  * Tags `stg_customers` with a `PII` tag.
  * Creates demo users `alice` and `bob`.
  * Creates a downstream table `revenue_daily` fed by `stg_orders`.
  * Creates two dashboards fed by those tables:
      - "Executive Revenue Dashboard"  (owner: alice)
      - "Customer 360"                 (owner: bob)

Environment variables (same as blast_radius.py):
  DATAHUB_URL    DataHub GMS URL   (default: http://localhost:8080)
  DATAHUB_TOKEN  Personal access token (optional for unauthenticated local)

Usage:
    python scripts/emit_dashboards.py
"""

import os
import sys
import time

import requests

import datahub.emitter.mce_builder as builder
import datahub.metadata.schema_classes as models
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter

DATAHUB_URL = os.environ.get("DATAHUB_URL", "http://localhost:8080").rstrip("/")
DATAHUB_TOKEN = os.environ.get("DATAHUB_TOKEN", "")

GRAPHQL_ENDPOINT = f"{DATAHUB_URL}/api/graphql"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
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


def find_dataset_urn(model_name: str) -> str | None:
    """Resolve a dbt model name to a DataHub dataset URN via search."""
    query = """
    query search($input: SearchInput!) {
      search(input: $input) {
        searchResults { entity { urn type } }
      }
    }
    """
    data = gql(
        query,
        {"input": {"type": "DATASET", "query": model_name, "start": 0, "count": 10}},
    )
    results = (data.get("search") or {}).get("searchResults") or []
    for result in results:
        urn = result["entity"]["urn"]
        if model_name.lower() in urn.lower():
            return urn
    return results[0]["entity"]["urn"] if results else None


def audit_stamp() -> models.AuditStampClass:
    return models.AuditStampClass(
        time=int(time.time() * 1000), actor=builder.make_user_urn("datahub")
    )


def ownership(*usernames: str) -> models.OwnershipClass:
    return models.OwnershipClass(
        owners=[
            models.OwnerClass(
                owner=builder.make_user_urn(u),
                type=models.OwnershipTypeClass.TECHNICAL_OWNER,
            )
            for u in usernames
        ]
    )


def emit(emitter: DatahubRestEmitter, urn: str, aspect) -> None:
    emitter.emit(MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    emitter = DatahubRestEmitter(gms_server=DATAHUB_URL, token=DATAHUB_TOKEN or None)

    # 1. Resolve the ingested dbt models --------------------------------------
    print("Resolving dbt models in DataHub...")
    stg_orders = find_dataset_urn("stg_orders")
    stg_customers = find_dataset_urn("stg_customers")
    if not stg_orders or not stg_customers:
        print(
            "ERROR: could not find stg_orders / stg_customers in DataHub.\n"
            "Run the ingestion first:  datahub ingest -c dbt_recipe.yml",
            file=sys.stderr,
        )
        return 1
    print(f"  stg_orders    -> {stg_orders}")
    print(f"  stg_customers -> {stg_customers}")

    # 2. PII tag on stg_customers ---------------------------------------------
    pii_tag_urn = builder.make_tag_urn("PII")
    emit(
        emitter,
        pii_tag_urn,
        models.TagPropertiesClass(
            name="PII", description="Contains personally identifiable information."
        ),
    )
    emit(
        emitter,
        stg_customers,
        models.GlobalTagsClass(
            tags=[models.TagAssociationClass(tag=pii_tag_urn)]
        ),
    )
    print("Tagged stg_customers with PII.")

    # 3. Demo users ------------------------------------------------------------
    for username, full_name in (("alice", "Alice Analyst"), ("bob", "Bob Builder")):
        emit(
            emitter,
            builder.make_user_urn(username),
            models.CorpUserInfoClass(
                active=True,
                displayName=full_name,
                email=f"{username}@example.com",
            ),
        )
    print("Created demo users alice and bob.")

    # 4. Downstream table: revenue_daily <- stg_orders --------------------------
    revenue_daily = builder.make_dataset_urn(
        platform="postgres", name="jaffle_shop.revenue_daily", env="PROD"
    )
    emit(
        emitter,
        revenue_daily,
        models.DatasetPropertiesClass(
            name="revenue_daily",
            description="Daily revenue rollup built from stg_orders (demo asset).",
        ),
    )
    emit(
        emitter,
        revenue_daily,
        models.UpstreamLineageClass(
            upstreams=[
                models.UpstreamClass(
                    dataset=stg_orders,
                    type=models.DatasetLineageTypeClass.TRANSFORMED,
                )
            ]
        ),
    )
    emit(emitter, revenue_daily, ownership("alice"))
    print(f"Created revenue_daily downstream of stg_orders -> {revenue_daily}")

    # 5. Dashboards --------------------------------------------------------------
    dashboards = [
        ("exec_revenue_overview", "Executive Revenue Dashboard",
         "Company-wide revenue KPIs reviewed every Monday.",
         [revenue_daily, stg_orders], "alice"),
        ("customer_360", "Customer 360",
         "Customer profile and lifetime-value explorer.",
         [stg_customers], "bob"),
    ]
    for dash_id, title, description, dataset_urns, owner in dashboards:
        dash_urn = builder.make_dashboard_urn(platform="looker", name=dash_id)
        emit(
            emitter,
            dash_urn,
            models.DashboardInfoClass(
                title=title,
                description=description,
                lastModified=models.ChangeAuditStampsClass(
                    created=audit_stamp(), lastModified=audit_stamp()
                ),
                datasets=list(dataset_urns),
                datasetEdges=[
                    models.EdgeClass(destinationUrn=u) for u in dataset_urns
                ],
            ),
        )
        emit(emitter, dash_urn, ownership(owner))
        print(f"Created dashboard '{title}' (owner: {owner}) -> {dash_urn}")

    print(
        "\nDone. Open a downstream lineage view on stg_orders in DataHub "
        "to see the demo graph, then try:\n"
        "  python blast_radius.py --changed-files models/staging/stg_orders.sql"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
