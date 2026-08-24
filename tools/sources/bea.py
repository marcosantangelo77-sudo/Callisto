"""BEA API — GDP, trade, industry accounts, regional data. Tier 1.

api.bea.gov/api/beahist?... no — the current surface is
https://apps.bea.gov/api/data?&UserID=...&method=GetData&DataSetName=...
Key free (apps.bea.gov/API/signup); set CALLISTO_BEA_API_KEY. Without a
key, 100 requests/min per IP is allowed with UserID='sampleUser' only on
some methods — we require a key and self-limit to ~1 req/s.

Standard call pattern: GetParameterList → GetParameterValues → GetData.
We wrap GetData directly for the common datasets and expose parameter
listing for discovery.

Answers: NIPA tables (GDP, components, price indexes), international
trade in goods & services, input-output / GDP-by-industry accounts,
state & metro personal income.
Cannot answer: monthly sub-NIPA firm data, forecasts (BEA publishes
estimates + revisions, not projections), real-time intraday anything;
revisions mean the latest vintage is NOT what was known historically.
"""

from __future__ import annotations

from tools.sources.base import RestSource, SourceError, SourceSpec

SPEC = SourceSpec(
    name="bea",
    base_url="https://apps.bea.gov/api/data",
    description="BEA: GDP, trade balances, industry accounts",
    answers=(
        "GDP and NIPA table series",
        "international trade in goods and services",
        "GDP by industry and input-output accounts",
        "state/metro personal income",
    ),
    cannot_answer=(
        "forecasts or projections — published estimates only",
        "firm-level data",
        "pre-revision vintages via this API (latest estimates only)",
    ),
    tier=1,
    min_interval_s=1.0,
    terms_url="https://www.bea.gov/information-use",
    key_env_var="CALLISTO_BEA_API_KEY",
)


class BeaAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def _url(self, params: dict) -> str:
        key = self.source.api_key()
        if not key:
            raise SourceError("BEA requires an API key; set CALLISTO_BEA_API_KEY")
        params = {"UserID": key, "ResultFormat": "JSON", **params}
        return self.source.build_url("", params)

    def get_data(self, dataset: str, tablename: str = "",
                 linecode: str = "", frequency: str = "",
                 years: str = "", **extra) -> dict:
        """GetData for NIPA ('NIPA', 'T10101', linecode), regional
        ('Regional', TableName like 'SAINC1'), etc. Returns BEA's payload
        with '_fetch' attached."""
        params: dict[str, str] = {"method": "GetData", "DataSetName": dataset}
        if tablename:
            params["TableName"] = tablename
        if linecode:
            params["LineCode"] = str(linecode)
        if frequency:
            params["Frequency"] = frequency
        if years:
            params["Year"] = str(years)
        params.update({k: str(v) for k, v in extra.items()})
        url = self._url(params)
        data, rec = self.source.get_json(url)
        # BEA signals failure with HTTP 200 + Results.Error, and has moved
        # its envelope key before ('BEAAPIs' -> 'BEAAPI'); an unrecognized
        # envelope made an error payload parse as ZERO rows downstream —
        # a rejected request masquerading as 'no data exists'.
        env = data.get("BEAAPIs") or data.get("BEAAPI") or {}
        err = (env.get("Results") or {}).get("Error")
        if err:
            code = err.get("APIErrorCode", "?")
            desc = err.get("APIErrorDescription", "unknown BEA error")
            raise SourceError(f"BEA API error {code}: {desc}")
        data["_fetch"] = {"url": rec.url, "sha256": rec.content_sha256,
                          "fetched_at": rec.fetched_at}
        return data

    def list_parameters(self, dataset: str) -> dict:
        return self.source.get_json(
            self._url({"method": "GetParameterList",
                       "DataSetName": dataset}))[0]
