"""W5 live smoke — one real query per touched source.

Run BY HAND, never from pytest (the suite is no-socket guarded). This host's
Python has TLS interception issues that curl does not, so the transport
delegates to curl; RestSource still records fetches and the QUERY being
tested is exactly what the planner authored.

Usage: python3 scripts/live_smoke_w5.py
"""

import json
import subprocess
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def curl_transport(url, headers):
    cmd = ["curl", "-s", "-m", "30", url]
    for k, v in headers.items():
        if k.lower() != "accept-encoding":
            cmd[1:1] = ["-H", f"{k}: {v}"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    return 200, out


def main():
    from tools.sources.base import RestSource
    from tools.sources.registry import get_source_registry
    from tools.sources import adapters as all_adapters
    from tools.sources.query_builder import build_plan, execute

    reg = get_source_registry()
    all_adapters.register_all(reg)

    QUESTIONS = {
        "openalex": ("What does recent scholarly research say about "
                     "semiconductor supply chain resilience?"),
        "clinicaltrials": ("Are there recruiting clinical trials of "
                           "semaglutide for obesity?"),
        "federalregister": ("Which proposed rules address vehicle emissions "
                            "standards?"),
        "gdelt": "News coverage of semiconductor export controls",
        "fred": None,          # needs CALLISTO_FRED_API_KEY; skipped if unset
        "wikidata": "What is penicillin?",
        "semantic_scholar": ("What does recent research say about lithium "
                             "battery recycling?"),
        "treasury": "Average interest rates on national debt since 2024-01-01",
    }

    for name, q in QUESTIONS.items():
        if q is None:
            print(f"[{name}] SKIPPED (no API key configured)")
            continue
        plan = build_plan(name, q)
        if not plan.plannable:
            print(f"[{name}] NOT PLANNABLE: {plan.reason}")
            continue
        entry = reg.get(name)
        adapter = entry.make_adapter(
            RestSource(entry.spec, transport=curl_transport))
        try:
            bodies = execute(adapter, plan)
            body = bodies[0]
            if name == "openalex":
                r = body.get("results", [])
                print(f"[{name}] {body.get('meta', {}).get('count')} total; "
                      f"first: {(r[0].get('display_name') if r else 'NONE')[:90]}")
            elif name == "clinicaltrials":
                st = body.get("studies", [])
                first = (st[0]["protocolSection"]["identificationModule"]
                         ["nctId"] if st else "NONE")
                print(f"[{name}] totalCount={body.get('totalCount')}; "
                      f"first: {first}")
            elif name == "federalregister":
                docs = body.get("documents", [])
                print(f"[{name}] count={body.get('count')}; first: "
                      f"{(docs[0].get('title') if docs else 'NONE')[:90]}")
            elif name == "gdelt":
                arts = body.get("articles", [])
                print(f"[{name}] {len(arts)} articles; first: "
                      f"{(arts[0].get('title') if arts else 'NONE')[:90]}")
            elif name == "fred":
                ser = body.get("series", []) or body.get("seriess", [])
                print(f"[{name}] {len(ser)} series found; first: "
                      f"{ser[0].get('id') if ser else 'NONE'}")
            elif name == "wikidata":
                binds = body.get("results", {}).get("bindings", [])
                print(f"[{name}] {len(binds)} entities; first: "
                      f"{binds[0]['itemLabel']['value'] if binds else 'NONE'}")
            elif name == "semantic_scholar":
                data = body.get("data", [])
                print(f"[{name}] {len(data)} papers; first: "
                      f"{data[0].get('title', 'NONE')[:90] if data else 'NONE'}")
            elif name == "treasury":
                rows = body.get("data", [])
                print(f"[{name}] {len(rows)} rows; first: {rows[0] if rows else 'NONE'}")
        except Exception as exc:
            print(f"[{name}] FAILED: {exc}")


if __name__ == "__main__":
    main()
