"""B6 — EDGAR / financial-modeling domain plugin.

Modules:
    edgar       SEC structured-data fetcher (tier-1 source per NEXT.md §4)
    statements  XBRL facts → assembled income statement / balance sheet / cash flow
    models      DCF / three-statement proforma / comparables as live-formula workbooks
    plugin      ToolRegistry registration (the extension point; orchestrator untouched)
"""
