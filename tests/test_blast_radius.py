"""Pytest suite for blast_radius.py core functions.

Covers:
- model_name_from_path()
- score_severity()
- detect_dropped_columns() with sqlglot mocking
- manifest.json parsing (new architecture)
- pagination for get_downstream_assets()
- batch enrichment for enrich_assets()
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

import blast_radius
from blast_radius import (
    ImpactedAsset,
    detect_dropped_columns,
    detect_dropped_columns_from_manifests,
    enrich_assets,
    get_columns_from_dbt_artifacts,
    get_downstream_assets,
    get_model_columns_from_catalog,
    get_model_columns_from_manifest,
    load_catalog,
    load_manifest,
    model_name_from_path,
    score_severity,
)


# --------------------------------------------------------------------------
# model_name_from_path
# --------------------------------------------------------------------------

class TestModelNameFromPath:
    def test_staging_model(self):
        assert model_name_from_path("models/staging/stg_orders.sql") == "stg_orders"

    def test_nested_path(self):
        assert model_name_from_path("models/marts/core/revenue_daily.sql") == "revenue_daily"

    def test_simple_filename(self):
        assert model_name_from_path("stg_customers.sql") == "stg_customers"

    def test_no_extension(self):
        # Path.stem handles no extension as whole name
        assert model_name_from_path("models/staging/stg_payments") == "stg_payments"

    def test_absolute_path(self):
        assert model_name_from_path("/home/user/project/models/staging/stg_orders.sql") == "stg_orders"


# --------------------------------------------------------------------------
# score_severity
# --------------------------------------------------------------------------

class TestScoreSeverity:
    def _make_asset(self, entity_type="TABLE", tags=None, owners=None):
        return ImpactedAsset(
            urn=f"urn:li:dataset:(urn:li:dataPlatform:postgres,test.{entity_type},PROD)",
            name=f"test_{entity_type.lower()}",
            entity_type=entity_type,
            degree=1,
            owners=owners or [],
            tags=tags or [],
        )

    def test_dropped_columns_with_assets_high(self):
        assets = [self._make_asset()]
        assert score_severity(assets, ["order_total"]) == "HIGH"

    def test_dropped_columns_no_assets_low(self):
        # Dropped columns but no downstream assets -> LOW (no impact)
        assert score_severity([], ["order_total"]) == "LOW"

    def test_pii_tag_high(self):
        assets = [self._make_asset(tags=["PII"])]
        assert score_severity(assets, []) == "HIGH"

    def test_pii_case_insensitive(self):
        assets = [self._make_asset(tags=["pii"])]
        assert score_severity(assets, []) == "HIGH"
        assets = [self._make_asset(tags=["Pii_Sensitive"])]
        assert score_severity(assets, []) == "HIGH"

    def test_pii_mixed_assets(self):
        assets = [
            self._make_asset(tags=[]),
            self._make_asset(tags=["PII"]),
        ]
        assert score_severity(assets, []) == "HIGH"

    def test_dashboard_medium(self):
        assets = [self._make_asset(entity_type="DASHBOARD")]
        assert score_severity(assets, []) == "MEDIUM"

    def test_chart_medium(self):
        assets = [self._make_asset(entity_type="CHART")]
        assert score_severity(assets, []) == "MEDIUM"

    def test_table_low(self):
        assets = [self._make_asset(entity_type="TABLE")]
        assert score_severity(assets, []) == "LOW"

    def test_empty_assets_low(self):
        assert score_severity([], []) == "LOW"

    def test_dashboard_with_pii_high_overrides_medium(self):
        assets = [self._make_asset(entity_type="DASHBOARD", tags=["PII"])]
        assert score_severity(assets, []) == "HIGH"

    def test_dropped_overrides_medium(self):
        assets = [self._make_asset(entity_type="DASHBOARD")]
        assert score_severity(assets, ["col"]) == "HIGH"


# --------------------------------------------------------------------------
# detect_dropped_columns - sqlglot mocking
# --------------------------------------------------------------------------

class TestDetectDroppedColumnsSqlglot:
    """Tests for legacy sqlglot path, mocked to avoid real git/sqlglot dependency."""

    @patch("blast_radius.subprocess.run")
    @patch("blast_radius.Path.read_text")
    def test_dropped_column_detected(self, mock_read_text, mock_subprocess):
        # Mock old SQL from git show
        mock_subprocess.return_value = MagicMock(stdout="SELECT id, customer_id, order_total FROM raw_orders")
        mock_read_text.return_value = "SELECT id, customer_id FROM raw_orders"

        # Mock sqlglot
        mock_sqlglot = MagicMock()
        # Create mock expressions for old SQL: 3 columns
        def make_mock_select(cols):
            mock_select = MagicMock()
            mock_projections = []
            for col in cols:
                proj = MagicMock()
                proj.alias_or_name = col
                mock_projections.append(proj)
            mock_select.expressions = mock_projections
            mock_expr = MagicMock()
            mock_expr.find.return_value = mock_select
            return mock_expr

        # parse returns list of expressions, each with find returning Select
        # We'll need to differentiate old vs new call via side_effect
        mock_sqlglot.parse.side_effect = [
            [make_mock_select(["id", "customer_id", "order_total"])],  # old_sql
            [make_mock_select(["id", "customer_id"])],  # new_sql
        ]
        mock_sqlglot.exp.Select = MagicMock()

        with patch.dict("sys.modules", {"sqlglot": mock_sqlglot}):
            # Need to also mock Path.exists to return True
            with patch.object(Path, "exists", return_value=True):
                result = blast_radius._detect_dropped_columns_sqlglot(
                    "models/staging/stg_orders.sql", "origin/main"
                )
        assert result == ["order_total"]

    @patch("blast_radius.subprocess.run")
    def test_git_failure_returns_empty(self, mock_subprocess):
        mock_subprocess.side_effect = Exception("git error")
        result = blast_radius._detect_dropped_columns_sqlglot("models/staging/stg_orders.sql", "origin/main")
        assert result == []

    @patch("blast_radius.subprocess.run")
    @patch("blast_radius.Path.read_text")
    def test_no_dropped_columns(self, mock_read_text, mock_subprocess):
        mock_subprocess.return_value = MagicMock(stdout="SELECT a, b FROM t")
        mock_read_text.return_value = "SELECT a, b, c FROM t"

        mock_sqlglot = MagicMock()

        def make_mock_select(cols):
            mock_select = MagicMock()
            mock_projections = []
            for col in cols:
                proj = MagicMock()
                proj.alias_or_name = col
                mock_projections.append(proj)
            mock_select.expressions = mock_projections
            mock_expr = MagicMock()
            mock_expr.find.return_value = mock_select
            return mock_expr

        mock_sqlglot.parse.side_effect = [
            [make_mock_select(["a", "b"])],
            [make_mock_select(["a", "b", "c"])],
        ]
        mock_sqlglot.exp.Select = MagicMock()

        with patch.dict("sys.modules", {"sqlglot": mock_sqlglot}):
            with patch.object(Path, "exists", return_value=True):
                result = blast_radius._detect_dropped_columns_sqlglot(
                    "models/staging/stg_orders.sql", "origin/main"
                )
        assert result == []


# --------------------------------------------------------------------------
# Manifest parsing - new architecture
# --------------------------------------------------------------------------

SAMPLE_MANIFEST = {
    "nodes": {
        "model.jaffle_shop.stg_orders": {
            "resource_type": "model",
            "name": "stg_orders",
            "original_file_path": "models/staging/stg_orders.sql",
            "columns": {
                "order_id": {"name": "order_id"},
                "customer_id": {"name": "customer_id"},
                "order_date": {"name": "order_date"},
                "status": {"name": "status"},
                "order_total": {"name": "order_total"},
            },
        },
        "model.jaffle_shop.stg_customers": {
            "resource_type": "model",
            "name": "stg_customers",
            "original_file_path": "models/staging/stg_customers.sql",
            "columns": {
                "customer_id": {"name": "customer_id"},
                "first_name": {"name": "first_name"},
                "last_name": {"name": "last_name"},
                "email": {"name": "email"},
            },
        },
        "model.jaffle_shop.empty_model": {
            "resource_type": "model",
            "name": "empty_model",
            "original_file_path": "models/staging/empty_model.sql",
            "columns": {},
        },
    }
}

SAMPLE_CATALOG = {
    "nodes": {
        "model.jaffle_shop.stg_orders": {
            "metadata": {"name": "stg_orders"},
            "columns": {
                "order_id": {"name": "order_id"},
                "customer_id": {"name": "customer_id"},
                "order_total": {"name": "order_total"},
            },
        }
    }
}


class TestManifestParsing:
    def test_load_manifest_valid(self, tmp_path):
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(SAMPLE_MANIFEST))
        data = load_manifest(manifest_file)
        assert data is not None
        assert "nodes" in data

    def test_load_manifest_missing(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        assert load_manifest(missing) is None

    def test_load_manifest_invalid_json(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json")
        assert load_manifest(bad_file) is None

    def test_get_columns_from_manifest_found(self):
        cols = get_model_columns_from_manifest(SAMPLE_MANIFEST, "stg_orders")
        assert cols == {"order_id", "customer_id", "order_date", "status", "order_total"}

    def test_get_columns_from_manifest_case_lower(self):
        # Ensure lowercasing
        manifest = {
            "nodes": {
                "model.test.UPPER": {
                    "resource_type": "model",
                    "name": "upper",
                    "columns": {"ID": {}, "Name": {}},
                }
            }
        }
        cols = get_model_columns_from_manifest(manifest, "upper")
        assert cols == {"id", "name"}

    def test_get_columns_from_manifest_empty(self):
        cols = get_model_columns_from_manifest(SAMPLE_MANIFEST, "empty_model")
        assert cols == set()

    def test_get_columns_from_manifest_not_found(self):
        cols = get_model_columns_from_manifest(SAMPLE_MANIFEST, "nonexistent")
        assert cols is None

    def test_get_columns_from_catalog_found(self):
        cols = get_model_columns_from_catalog(SAMPLE_CATALOG, "stg_orders")
        assert cols == {"order_id", "customer_id", "order_total"}

    def test_get_columns_from_catalog_not_found(self):
        cols = get_model_columns_from_catalog(SAMPLE_CATALOG, "missing")
        assert cols is None

    def test_get_columns_from_dbt_artifacts_manifest_primary(self, tmp_path):
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(SAMPLE_MANIFEST))
        catalog_file = tmp_path / "catalog.json"
        catalog_file.write_text(json.dumps(SAMPLE_CATALOG))

        cols = get_columns_from_dbt_artifacts("stg_orders", manifest_file, catalog_file)
        assert cols == {"order_id", "customer_id", "order_date", "status", "order_total"}

    def test_get_columns_from_dbt_artifacts_fallback_to_catalog(self, tmp_path):
        # Manifest has empty columns, should fallback to catalog
        manifest_empty = {
            "nodes": {
                "model.jaffle_shop.stg_orders": {
                    "resource_type": "model",
                    "name": "stg_orders",
                    "original_file_path": "models/staging/stg_orders.sql",
                    "columns": {},
                }
            }
        }
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(manifest_empty))
        catalog_file = tmp_path / "catalog.json"
        catalog_file.write_text(json.dumps(SAMPLE_CATALOG))

        cols = get_columns_from_dbt_artifacts("stg_orders", manifest_file, catalog_file)
        assert cols == {"order_id", "customer_id", "order_total"}

    def test_get_columns_from_dbt_artifacts_no_files(self, tmp_path):
        cols = get_columns_from_dbt_artifacts(
            "stg_orders", tmp_path / "no_manifest.json", tmp_path / "no_catalog.json"
        )
        assert cols is None


class TestDetectDroppedFromManifests:
    def test_dropped_columns_manifest(self, tmp_path):
        old_manifest = {
            "nodes": {
                "model.jaffle_shop.stg_orders": {
                    "resource_type": "model",
                    "name": "stg_orders",
                    "columns": {
                        "order_id": {},
                        "customer_id": {},
                        "order_total": {},
                    },
                }
            }
        }
        new_manifest = {
            "nodes": {
                "model.jaffle_shop.stg_orders": {
                    "resource_type": "model",
                    "name": "stg_orders",
                    "columns": {
                        "order_id": {},
                        "customer_id": {},
                    },
                }
            }
        }
        old_path = tmp_path / "old_manifest.json"
        new_path = tmp_path / "new_manifest.json"
        old_path.write_text(json.dumps(old_manifest))
        new_path.write_text(json.dumps(new_manifest))

        dropped = detect_dropped_columns_from_manifests("stg_orders", old_path, new_path)
        assert dropped == ["order_total"]

    def test_no_dropped_manifest(self, tmp_path):
        manifest = {
            "nodes": {
                "model.jaffle_shop.stg_orders": {
                    "resource_type": "model",
                    "name": "stg_orders",
                    "columns": {"a": {}, "b": {}},
                }
            }
        }
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest))
        dropped = detect_dropped_columns_from_manifests("stg_orders", path, path)
        assert dropped == []

    def test_model_not_in_manifest_returns_none(self, tmp_path):
        manifest = {"nodes": {}}
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest))
        result = detect_dropped_columns_from_manifests("missing", path, path)
        assert result is None


# --------------------------------------------------------------------------
# Pagination for get_downstream_assets
# --------------------------------------------------------------------------

class TestGetDownstreamAssetsPagination:
    @patch("blast_radius.is_mock_mode", return_value=False)
    @patch("blast_radius.gql")
    def test_pagination_two_pages(self, mock_gql, mock_mock_mode):
        # First page returns 2 results, second page returns 1, third empty
        # page_size=2 for test
        def side_effect(query, variables):
            start = variables["input"]["start"]
            if start == 0:
                return {
                    "searchAcrossLineage": {
                        "searchResults": [
                            {
                                "degree": 1,
                                "entity": {
                                    "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,db.table1,PROD)",
                                    "type": "DATASET",
                                    "properties": {"name": "table1"},
                                },
                            },
                            {
                                "degree": 1,
                                "entity": {
                                    "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,db.table2,PROD)",
                                    "type": "DATASET",
                                    "properties": {"name": "table2"},
                                },
                            },
                        ]
                    }
                }
            elif start == 2:
                return {
                    "searchAcrossLineage": {
                        "searchResults": [
                            {
                                "degree": 2,
                                "entity": {
                                    "urn": "urn:li:dashboard:(looker,dash1)",
                                    "type": "DASHBOARD",
                                    "properties": {"name": "dash1"},
                                },
                            }
                        ]
                    }
                }
            else:
                return {"searchAcrossLineage": {"searchResults": []}}

        mock_gql.side_effect = side_effect

        assets = get_downstream_assets("urn:li:dataset:(urn:li:dataPlatform:postgres,db.stg_orders,PROD)", page_size=2, max_pages=5)
        assert len(assets) == 3
        assert assets[0].name == "table1"
        assert assets[2].name == "dash1"
        # Verify gql called with increasing start
        assert mock_gql.call_count == 2 or mock_gql.call_count == 3  # depending on break logic

    @patch("blast_radius.is_mock_mode", return_value=False)
    @patch("blast_radius.gql")
    def test_deduplication(self, mock_gql, mock_mock_mode):
        # Same URN returned twice across pages should be deduped
        mock_gql.return_value = {
            "searchAcrossLineage": {
                "searchResults": [
                    {
                        "degree": 1,
                        "entity": {
                            "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,db.table1,PROD)",
                            "type": "DATASET",
                            "properties": {"name": "table1"},
                        },
                    },
                    {
                        "degree": 1,
                        "entity": {
                            "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,db.table1,PROD)",
                            "type": "DATASET",
                            "properties": {"name": "table1"},
                        },
                    },
                ]
            }
        }
        assets = get_downstream_assets("urn:test", page_size=100, max_pages=1)
        assert len(assets) == 1

    @patch("blast_radius.is_mock_mode", return_value=False)
    @patch("blast_radius.gql")
    def test_empty_results(self, mock_gql, mock_mock_mode):
        mock_gql.return_value = {"searchAcrossLineage": {"searchResults": []}}
        assets = get_downstream_assets("urn:test")
        assert assets == []


# --------------------------------------------------------------------------
# Batch enrichment
# --------------------------------------------------------------------------

class TestBatchEnrichment:
    @patch("blast_radius.is_mock_mode", return_value=False)
    @patch("blast_radius.gql")
    def test_enrich_assets_batch(self, mock_gql, mock_mock_mode):
        # Mock batch response with aliases a0, a1
        mock_gql.return_value = {
            "a0": {
                "ownership": {
                    "owners": [{"owner": {"username": "alice"}}]
                },
                "tags": {"tags": [{"tag": {"name": "PII"}}]},
            },
            "a1": {
                "ownership": {
                    "owners": [{"owner": {"username": "bob"}}]
                },
                "tags": {"tags": [{"tag": {"name": "Finance"}}]},
            },
        }

        assets = [
            ImpactedAsset(urn="urn:li:dataset:(postgres,db.table1,PROD)", name="table1", entity_type="DATASET", degree=1),
            ImpactedAsset(urn="urn:li:dataset:(postgres,db.table2,PROD)", name="table2", entity_type="DATASET", degree=1),
        ]

        enrich_assets(assets, batch_size=2)

        assert assets[0].owners == ["alice"]
        assert assets[0].tags == ["PII"]
        assert assets[1].owners == ["bob"]
        assert assets[1].tags == ["Finance"]
        # Should be single gql call for batch_size=2
        assert mock_gql.call_count == 1

    @patch("blast_radius.is_mock_mode", return_value=False)
    @patch("blast_radius.gql")
    def test_enrich_assets_batching_multiple_batches(self, mock_gql, mock_mock_mode):
        # 3 assets, batch_size 2 => 2 calls
        mock_gql.return_value = {
            "a0": {"ownership": {"owners": []}, "tags": {"tags": []}},
            "a1": {"ownership": {"owners": []}, "tags": {"tags": []}},
        }

        assets = [
            ImpactedAsset(urn=f"urn:li:dataset:(postgres,db.table{i},PROD)", name=f"table{i}", entity_type="DATASET", degree=1)
            for i in range(3)
        ]

        enrich_assets(assets, batch_size=2)
        assert mock_gql.call_count == 2

    @patch("blast_radius.gql")
    def test_enrich_assets_empty(self, mock_gql):
        enrich_assets([])
        mock_gql.assert_not_called()

    @patch("blast_radius.is_mock_mode", return_value=False)
    @patch("blast_radius.gql")
    def test_enrich_assets_fallback_on_failure(self, mock_gql, mock_mock_mode):
        # First batch fails, fallback to single enrich_asset calls
        # We need to mock gql to fail on batch then succeed on single
        def side_effect(query, variables):
            if "a0" in query and "a1" in query:
                raise RuntimeError("batch failed")
            # Single enrich returns
            return {
                "entity": {
                    "ownership": {"owners": [{"owner": {"username": "alice"}}]},
                    "tags": {"tags": []},
                }
            }

        mock_gql.side_effect = side_effect

        assets = [
            ImpactedAsset(urn="urn:li:dataset:(postgres,db.table1,PROD)", name="table1", entity_type="DATASET", degree=1),
            ImpactedAsset(urn="urn:li:dataset:(postgres,db.table2,PROD)", name="table2", entity_type="DATASET", degree=1),
        ]

        enrich_assets(assets, batch_size=2)
        # After fallback, assets should still be enriched via single calls
        assert len(assets[0].owners) == 1
        assert assets[0].owners[0] == "alice"
