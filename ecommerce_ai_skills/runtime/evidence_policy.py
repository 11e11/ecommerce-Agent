"""Evidence policy: freshness thresholds and skill-input to evidence mapping.

The evidence audit is deliberately data-driven instead of prompt-driven: what
counts as "fresh", which report types satisfy which skill input, and which
inputs are operator parameters rather than evidence all live here so the audit
is reproducible, unit-testable, and reviewable without reading a prompt.

Three input kinds are recognised per skill input:

``evidence``   the input must be backed by an imported report type or a
               metric observation; a required input of this kind that nothing
               matches becomes a data gap.
``parameter``  the input is supplied by the operator (a target, a budget, a
               pasted customer message). It is never evidence-backed and never
               produces a gap -- reporting it as missing data would be noise.
``unknown``    not yet mapped. Required inputs of this kind produce a gap:
               the audit stays conservative and says "we cannot tell" rather
               than guessing that the data is available.
"""

from __future__ import annotations

from typing import Any

# --- freshness -------------------------------------------------------------

# Per-report-type thresholds in days. A report older than its threshold is
# stale for the purpose of this week's decision. Values are deliberately
# explicit rather than derived: a listing export ages more slowly than a sales
# report because the underlying fact changes more slowly.
DEFAULT_FRESHNESS_DAYS = 14
FRESHNESS_DAYS_BY_SOURCE_TYPE: dict[str, int] = {
    "amazon_business_report": 3,
    "amazon_ads_search_term": 3,
    "amazon_fba_inventory": 3,
    "amazon_returns": 14,
    "amazon_listing": 14,
    "shopify_products": 3,
    "metric_observation": 3,
    "platform_generic": 14,
}

# Used only when a platform has no assigned skill to derive requirements from.
PLATFORM_BASELINE_REPORT_TYPES: dict[str, tuple[str, ...]] = {
    "amazon": ("amazon_business_report",),
    "shopify": ("shopify_products",),
}


# --- skill input mapping ---------------------------------------------------

PARAMETER = "parameter"
UNKNOWN = "unknown"
EVIDENCE = "evidence"


def _evidence(source_types: tuple[str, ...], metric_keys: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"kind": EVIDENCE, "source_types": source_types, "metric_keys": metric_keys}


SKILL_INPUT_EVIDENCE: dict[str, dict[str, dict[str, Any]]] = {
    "ecom-advertising": {
        "campaign_data": _evidence(
            ("amazon_ads_search_term",), ("ad_spend", "ad_sales", "acos")
        ),
        "search_term_report": _evidence(("amazon_ads_search_term",)),
        "target_acos": {"kind": PARAMETER},
        "budget": {"kind": PARAMETER},
    },
    "ecom-applicability": {
        "task": {"kind": PARAMETER},
        # The audit itself answers data availability, so this input is always
        # satisfied when an audit exists.
        "data_availability": {"kind": EVIDENCE, "source_types": (), "metric_keys": (),
                              "self_satisfied": True},
        "risk_tolerance": {"kind": PARAMETER},
    },
    "ecom-compliance": {
        "product_info": _evidence(("amazon_listing", "shopify_products")),
        "target_markets": {"kind": PARAMETER},
        "brand_info": {"kind": PARAMETER},
    },
    "ecom-customer-service": {
        "customer_message": {"kind": PARAMETER},
        "review_data": {"kind": UNKNOWN},
        "product_info": _evidence(("amazon_listing", "shopify_products")),
        "platform": {"kind": PARAMETER},
        "market": {"kind": PARAMETER},
        "order_info": {"kind": UNKNOWN},
        "complaint_notice": {"kind": PARAMETER},
        "return_report": _evidence(("amazon_returns",)),
    },
    "ecom-inventory": {
        "sales_history": _evidence(
            ("amazon_business_report", "shopify_products"), ("units_ordered", "sales")
        ),
        "lead_time_days": {"kind": PARAMETER},
        "current_stock": _evidence(
            ("amazon_fba_inventory",), ("fulfillable_quantity", "stock_level")
        ),
        "service_level": {"kind": PARAMETER},
    },
    "ecom-listing": {
        "product_info": _evidence(("amazon_listing", "shopify_products")),
        "keywords": {"kind": UNKNOWN},
        "platform": {"kind": PARAMETER},
        "market": {"kind": PARAMETER},
        "competitor_listings": {"kind": UNKNOWN},
    },
    "ecom-pricing": {
        "cost_data": {"kind": PARAMETER},
        "competitor_prices": {"kind": UNKNOWN},
        "target_margin": {"kind": PARAMETER},
    },
    "ecom-research": {
        "category": {"kind": PARAMETER},
        "market": {"kind": PARAMETER},
        "criteria": {"kind": PARAMETER},
    },
    "ecom-social": {
        "content_assets": {"kind": PARAMETER},
        "platform": {"kind": PARAMETER},
        "goal": {"kind": PARAMETER},
    },
}
