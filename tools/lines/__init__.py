"""tools.lines — extracted internals of the line monitor.

Split out of tools/line_monitor.py (which keeps the public LineMonitor
import path stable):
- ingest: WS/incremental snapshot conversion, delta merging, scraper enrichment
- edge_report: devigged consensus, movement → +EV evaluation, model agreement
- movement: significant-movement filtering, persistence, KL divergence
"""
