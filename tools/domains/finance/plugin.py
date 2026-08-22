"""Finance domain plugin — the second domain, first non-sports one.

Registered the same way sports and compute are (B3's ToolRegistry is the
extension point; orchestrator.py untouched). Serves Domain.FINANCIAL plus
keyword routing for finance queries that arrive without a domain tag.

Tools exposed to the session:
    edgar_get_statements(ticker, n_periods)
        ticker → CIK → companyfacts → assembled three statements with
        provenance, gaps, restatement flags, limitations.
    edgar_build_model(ticker, template, analyst_inputs)
        statements → DCF / proforma / comps spec → live-formula workbook
        artifact + sandbox-verified reference computation.

The dispatcher runs fetches in an executor (urllib is blocking); the SEC
rate limiter is per-client, and one client is shared process-wide so a
burst of tool calls cannot approach the 10 req/s ceiling.
"""

import asyncio
import json
import logging
import re
from typing import Optional

from tools.domain_registry import DomainPlugin

logger = logging.getLogger("callisto.finance_plugin")

_client = None
_client_lock = asyncio.Lock()


def _get_client():
    global _client
    if _client is None:
        from agp.provenance import ProvenanceLedger
        from tools.domains.finance.edgar import EdgarClient

        _client = EdgarClient(ledger=ProvenanceLedger())
    return _client


GET_STATEMENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "edgar_get_statements",
        "description": (
            "Fetch a public company's financial statements from SEC EDGAR "
            "XBRL structured data: income statement, balance sheet, cash "
            "flow for N annual periods. Returns tagged values with the "
            "filing fact each number came from, explicit gaps, restatement "
            "flags, and coverage limitations. Tier-1 primary source."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "n_periods": {"type": "integer",
                              "description": "annual periods to assemble (default 4)"},
            },
            "required": ["ticker"],
        },
    },
}

BUILD_MODEL_TOOL = {
    "type": "function",
    "function": {
        "name": "edgar_build_model",
        "description": (
            "Build a live-formula Excel model from SEC XBRL data: "
            "'dcf' (discounted cash flow with WACC×terminal-growth "
            "sensitivity), 'proforma' (three-statement projection), or "
            "'comps' (comparables; requires peers list). The workbook's "
            "Model sheet is LIVE formulas — assumptions propagate. Also "
            "returns a sandbox-sealed reference computation of the "
            "headline numbers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string",
                           "description": "for dcf/proforma"},
                "template": {"type": "string",
                             "enum": ["dcf", "proforma", "comps"]},
                "analyst_inputs": {
                    "type": "object",
                    "description": ("optional judgment overrides, e.g. "
                                    "{wacc: 0.09, terminal_growth: 0.025, "
                                    "diluted_shares: 7400000000}")},
                "peers": {
                    "type": "array",
                    "description": ("for comps: [{name, price, shares, eps, "
                                    "bvps, ebitda, revenue, debt, cash}]")},
                "n_periods": {"type": "integer"},
            },
            "required": ["template"],
        },
    },
}


def _statements_payload(ticker: str, n_periods: int) -> dict:
    from tools.domains.finance.edgar import EdgarError
    from tools.domains.finance.statements import assemble_statements

    client = _get_client()
    cik, facts = client.facts_for_ticker(ticker)
    stmt = assemble_statements(facts, n_periods=max(1, min(int(n_periods), 10)))
    stmt.ticker = ticker.upper()
    return stmt.to_dict()


def _model_payload(template: str, ticker: str, analyst_inputs: Optional[dict],
                   peers: Optional[list], n_periods: int) -> dict:
    import tempfile
    from pathlib import Path

    from tools.artifacts import default_store
    from tools.charts import store_workbook
    from tools.domains.finance.models import (
        comps_workbook,
        dcf_workbook,
        proforma_workbook,
    )
    from tools.sandbox import run_python

    template = (template or "").lower()
    if template == "comps":
        if not peers:
            raise ValueError("comps template requires 'peers'")
        spec, notes = comps_workbook(peers)
        sandbox = None
    else:
        from tools.domains.finance.statements import assemble_statements

        client = _get_client()
        cik, facts = client.facts_for_ticker(ticker)
        stmt = assemble_statements(
            facts, n_periods=max(2, min(int(n_periods or 4), 10)))
        stmt.ticker = ticker.upper()
        ai = {k: float(v) for k, v in (analyst_inputs or {}).items()}
        if template == "dcf":
            spec, notes = dcf_workbook(stmt, analyst_inputs=ai)
        elif template == "proforma":
            spec, notes = proforma_workbook(stmt, analyst_inputs=ai)
        else:
            raise ValueError(f"unknown template {template!r}")
        sandbox = run_python(spec["code"]).to_dict() if spec.get("code") else None

    store = default_store()
    wb_result = store_workbook(spec, store=store,
                               name=f"{spec['title'][:48]}")
    out_path = Path(tempfile.gettempdir()) / (
        re.sub(r"[^A-Za-z0-9_-]+", "_", spec["title"])[:60] + ".xlsx")
    out_path.write_bytes(store.get_bytes(wb_result["workbook"].sha256))

    return {
        "template": template,
        "workbook_sha256": wb_result["workbook"].sha256,
        "live_formulas": wb_result["live_formulas"],
        "exported_path": str(out_path),
        "notes": notes,
        "limitations": LIMITS,
        "sandbox_reference": sandbox,
    }


LIMITS = [
    "XBRL gives tagged statement lines only — NO footnotes, segment detail, "
    "lease schedules, commitments, contingencies, or non-GAAP adjustments.",
    "A material footnote can change the meaning of any line. Verify against "
    "the filing before relying on any single figure.",
    "Market-dependent outputs (per-share value vs price, comps multiples) "
    "need quotes EDGAR does not carry; supply them as analyst inputs.",
]


async def _execute(name: str, arguments: dict) -> dict:
    loop = asyncio.get_event_loop()

    def _run(name: str, args: dict) -> dict:
        if name == "edgar_get_statements":
            return _statements_payload(
                args.get("ticker", ""), int(args.get("n_periods", 4)))
        if name == "edgar_build_model":
            return _model_payload(
                args.get("template", ""),
                args.get("ticker", ""),
                args.get("analyst_inputs"),
                args.get("peers"),
                int(args.get("n_periods", 4)),
            )
        raise ValueError(f"finance plugin does not own tool {name!r}")

    try:
        result = await loop.run_in_executor(None, _run, name, dict(arguments))
        result["ok"] = True
        return result
    except Exception as exc:  # surfaced to the model as tool error text
        logger.warning("finance tool %s failed: %s", name, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "tool": name}


_KEYWORDS = re.compile(
    r"\b(edgar|sec filing|10-K|10-Q|xbrl|balance sheet|income statement|"
    r"cash flow statement|financial statements?|revenue|ebitda|free cash ?flow|"
    r"fcf|wacc|dcf|discounted cash flow|proforma|pro forma|comps|"
    r"comparables|valuation|price target|market cap|eps|p/e ratio)\b",
    re.IGNORECASE,
)


def build_finance_plugin() -> DomainPlugin:
    return DomainPlugin(
        name="finance",
        domains={"FINANCIAL"},
        keywords=_KEYWORDS,
        tool_schemas=[GET_STATEMENTS_TOOL, BUILD_MODEL_TOOL],
        freshness=[],  # filings are historical by nature; no window forcing
        execute=_execute,
    )


def register_if_available(registry) -> bool:
    """Register iff the finance modules import cleanly (mirrors compute)."""
    try:
        import tools.domains.finance.edgar  # noqa: F401
        import tools.domains.finance.models  # noqa: F401
        import tools.domains.finance.statements  # noqa: F401
    except ImportError:
        logger.info("finance plugin unavailable (modules not merged yet)")
        return False
    if "finance" not in {p.name for p in registry.plugins()}:
        registry.register(build_finance_plugin())
    return True
