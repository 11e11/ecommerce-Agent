"""Local-only populated UI fixture for browser design QA.

This module is never packaged or used by production runtime paths.  It binds to
loopback, creates an isolated temporary database, and accepts the literal
non-secret browser key ``test`` so visual QA can exercise the real UI without
transmitting a real credential.
"""

from __future__ import annotations

import tempfile
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ecommerce_ai_skills.runtime.api import RuntimeApplication, _Handler
from ecommerce_ai_skills.demo_seed import DemoSeedProvider
from datetime import datetime, timedelta, timezone
from ecommerce_ai_skills.runtime.storage import Database


class PreviewProvider(DemoSeedProvider):
    def configuration(self):
        return "ui_preview_fixture", "fixture-model"


def seed(app: RuntimeApplication):
    tenant_id, owner_id = app.db.create_tenant(
        "UI Preview Demo", "director@example.test", mode="demo"
    )
    principal = app.auth.authenticate(app.auth.issue_key(tenant_id, owner_id))

    def imported(platform, report_type, filename, observed_at, raw):
        return app.evidence_imports.import_csv(
            principal,
            raw=raw,
            platform=platform,
            report_type=report_type,
            filename=filename,
            observed_at=observed_at,
            idempotency_key=f"preview:{filename}",
            request_id=f"preview:{filename}",
        )

    import_ids = []
    for day, revenue, units, sessions in (
        ("16", 185000, 920, 8400),
        ("17", 205000, 980, 8600),
        ("18", 196000, 950, 8500),
        ("19", 232000, 1100, 9000),
        ("20", 218000, 1040, 8900),
        ("21", 226000, 1070, 9050),
        ("22", 244000, 1150, 9200),
    ):
        item = imported(
            "amazon",
            "amazon_business_report",
            f"business-2026-08-{day}.csv",
            (datetime.now(timezone.utc) - timedelta(days=22-int(day), minutes=35)).isoformat(),
            (
                "ASIN,Sessions,Units Ordered,Ordered Product Sales\n"
                f"B08-A,{sessions},{units},{revenue}\n"
            ).encode(),
        )
        import_ids.append(item["id"])
    ads = imported(
        "amazon",
        "amazon_ads_search_term",
        "ads-2026-08-22.csv",
        (datetime.now(timezone.utc) - timedelta(days=0, minutes=20)).isoformat(),
        b"Campaign Name,Search Term,Spend\nSP-1,kitchen shelf,8400\nSP-1,storage rack,6200\n",
    )
    inventory = imported(
        "amazon",
        "amazon_fba_inventory",
        "inventory-2026-08-22.csv",
        (datetime.now(timezone.utc) - timedelta(days=0, minutes=5)).isoformat(),
        b"Seller SKU,Fulfillable Quantity\nSKU-1,0\nSKU-2,0\nSKU-3,0\nSKU-4,18\n",
    )
    shopify = imported(
        "shopify",
        "platform_generic",
        "shopify-products-2026-08-22.csv",
        (datetime.now(timezone.utc) - timedelta(days=0, minutes=0)).isoformat(),
        b"SKU,Price\nSKU-1,29.00\nSKU-2,41.00\n",
    )
    import_ids.extend([ads["id"], inventory["id"], shopify["id"]])
    run = app.agent_runs.request(
        principal,
        "weekly_ops",
        "AI 能做吗？判断这批经营数据是否适合 AI 分析，保留人工复核。",
        [],
        "preview-run",
        "preview-run-request",
        evidence_import_ids=import_ids,
    )
    app.agent_runs.execute(principal, run["id"], "preview-run-execute")
    for index, report_id in enumerate(("report-1", "report-2"), start=1):
        app.actions.request(
            principal,
            "amazon_spapi.import_report",
            {
                "external_account_id": "seller-us",
                "report_id": report_id,
                "evidence_report_type": "amazon_business_report",
            },
            f"preview-action-{index}",
            f"preview-action-request-{index}",
        )
    return principal


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="eai-ui-preview-"))
    app = RuntimeApplication(
        Database(root / "runtime.sqlite"), agent_provider=PreviewProvider()
    )
    principal = seed(app)

    class PreviewHandler(_Handler):
        def _principal(self):
            return principal

    server = ThreadingHTTPServer(("127.0.0.1", 8794), PreviewHandler)
    server.app = app
    print("UI preview fixture listening on http://127.0.0.1:8794/app", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
