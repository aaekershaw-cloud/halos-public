"""App Store Connect API client for pulling App Store metrics."""

import json
import jwt
import logging
import time
from pathlib import Path
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

APPSTORE_CONNECT_BASE = "https://api.appstoreconnect.apple.com/v1"


class AppStoreConnectClient:
    def __init__(
        self,
        key_id: str,
        issuer_id: str,
        private_key_path: str,
        app_id: Optional[str] = None,
    ):
        self.key_id = key_id
        self.issuer_id = issuer_id
        self.private_key_path = Path(private_key_path).expanduser()
        self.app_id = app_id
        self._token: Optional[str] = None
        self._token_expires: float = 0.0

    def _generate_token(self) -> str:
        if not self.key_id or not self.issuer_id or not self.private_key_path or self.private_key_path.is_dir():
            raise RuntimeError("App Store Connect credentials not configured (key_id, issuer_id, private_key_path)")
        now = time.time()
        headers = {
            "alg": "ES256",
            "kid": self.key_id,
            "typ": "JWT",
        }
        payload = {
            "iss": self.issuer_id,
            "iat": now,
            "exp": now + 1190,  # Apple allows max ~20 min
            "aud": "appstoreconnect-v1",
        }
        private_key = self.private_key_path.read_text()
        token = jwt.encode(payload, private_key, algorithm="ES256", headers=headers)
        self._token = token
        self._token_expires = now + 1100
        return token

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        return self._generate_token()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    async def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        url = f"{APPSTORE_CONNECT_BASE}{endpoint}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self._headers(), params=params, timeout=30.0)
            resp.raise_for_status()
            return resp.json()

    # --- App lookup ---

    async def list_apps(self) -> list[dict]:
        """List all apps accessible with this key."""
        data = await self._get("/apps", {"fields[apps]": "name,bundleId"})
        apps = data.get("data", [])
        logger.info(f"Found {len(apps)} app(s)")
        return apps

    async def find_app_by_bundle_id(self, bundle_id: str) -> Optional[str]:
        """Return app ID for a given bundle ID."""
        apps = await self.list_apps()
        for app in apps:
            attr = app.get("attributes", {})
            if attr.get("bundleId") == bundle_id:
                return app["id"]
        return None

    # --- Sales / Units ---

    async def get_sales_report(
        self,
        frequency: str = "DAILY",  # DAILY | WEEKLY | MONTHLY | YEARLY
        report_date: str = "",
        report_type: str = "SALES",
        report_sub_type: str = "SUMMARY",
        version: str = "1_0",
    ) -> bytes:
        """Download raw sales report CSV."""
        params = {
            "filter[frequency]": frequency,
            "filter[reportType]": report_type,
            "filter[reportSubType]": report_sub_type,
            "filter[vendorNumber]": "",  # must be set if available
            "filter[version]": version,
        }
        if report_date:
            params["filter[reportDate]"] = report_date

        url = f"{APPSTORE_CONNECT_BASE}/salesReports"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self._headers(), params=params, timeout=60.0)
            resp.raise_for_status()
            return resp.content

    # --- App Store Connect Analytics (v1.3+) ---

    async def get_app_units(
        self,
        app_id: str,
        start_date: str,
        end_date: str,
        measures: str = "units",
    ) -> dict:
        """Pull app units (downloads) for a date range."""
        # Note: v1 analytics endpoints use a different base path
        url = f"https://api.appstoreconnect.apple.com/v1/apps/{app_id}/appStoreVersions"
        # For time-series metrics we need the newer analytics API
        # which is technically in beta; fallback is sales report CSV parsing.
        params = {
            "filter[startTime]": start_date,
            "filter[endTime]": end_date,
            "filter[measures]": measures,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.appstoreconnect.apple.com/v1/apps/{app_id}/metrics",
                headers=self._headers(),
                params=params,
                timeout=30.0,
            )
            if resp.status_code == 404:
                logger.warning("App Store Connect Metrics API returned 404 — may not be enabled for this key.")
                return {}
            resp.raise_for_status()
            return resp.json()

    # --- Sales Reports (the reliable way for downloads) ---

    async def get_downloads_csv(self, report_date: str = "") -> bytes:
        """Download raw sales report CSV. Defaults to most recent daily report if no date given."""
        if not report_date:
            from datetime import datetime, timedelta
            report_date = (datetime.now() - timedelta(days=2)).strftime("%Y%m%d")
        return await self.get_sales_report(
            frequency="DAILY",
            report_date=report_date,
            report_type="SALES",
            report_sub_type="SUMMARY",
            version="1_0",
        )

    # --- High-level helpers ---

    async def get_example_app_metrics(self, bundle_id: str = "com.example.app") -> dict:
        """Best-effort ExampleApp metrics pull. Falls back gracefully."""
        app_id = self.app_id or await self.find_app_by_bundle_id(bundle_id)
        if not app_id:
            return {"error": f"App with bundle ID {bundle_id} not found"}

        result = {"app_id": app_id, "bundle_id": bundle_id}
        try:
            units = await self.get_app_units(app_id, "2026-03-01", "2026-04-01")
            result["units"] = units
        except Exception as e:
            logger.warning(f"Could not pull app units: {e}")
            result["units_error"] = str(e)

        try:
            csv_bytes = await self.get_downloads_csv()
            result["sales_report_csv_lines"] = len(csv_bytes.decode("utf-8", errors="ignore").splitlines())
        except Exception as e:
            logger.warning(f"Could not pull sales report: {e}")
            result["sales_report_error"] = str(e)

        return result
