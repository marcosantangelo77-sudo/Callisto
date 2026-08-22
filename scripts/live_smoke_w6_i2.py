"""I2 live smoke — one real query per adapter touched this wave.

Run BY HAND, never from pytest (the suite is no-socket guarded). This
host's Python has TLS interception issues that curl does not, so the
transport delegates to curl; RestSource still records fetches and the
QUERY tested is exactly what the planner authored.

SEC and ClinicalTrials.gov are deliberately absent: both 403 this machine
after earlier live testing (MORNING_REPORT, environmental blocks).

STATUS 2026-08-22: GDELT throttling is IP-sticky — after a handful of
smoke calls it returns the "limit requests to one every 5 seconds" text
for 1h+ regardless of pacing. Earlier the SAME planner-authored query
returned 18 artlist articles and a real timelinevol series, so the plan
path is proven; expect [gdelt] FAILED until the block lifts.

Usage: python3 scripts/live_smoke_w6_i2.py
"""

import json
import os
import subprocess
import sys
import time

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
    from tools.sources.query_builder import build_plan, execute

    reg = get_source_registry()

    QUESTIONS = [
        ("openalex", "What does recent scholarly research say about "
                     "semiconductor supply chain resilience?"),
        ("semanticscholar", "What does recent research say about lithium "
                            "battery recycling?"),
        ("worldbank", "How has GDP in China changed since 2010?"),
        ("bea", "What happened to the trade balance last year?"),
        ("census", "Are housing starts falling since 2023-06?"),
        ("fdic", "What are the assets of JPMorgan Chase bank?"),
        ("cftc_cot", "Positioning of money managers in crude oil futures"),
        ("wayback", "What did https://example.com say before 2024-01-01?"),
        ("gdelt", "News coverage of semiconductor export controls"),
        ("wikidata", "What is penicillin?"),
        ("treasury", "National debt since 2024-01-01"),
        # keyed — skipped unless configured
        ("eia", "Monthly WTI crude oil prices since 2020"),
        ("uspto_odp", "Patents assigned to TSMC regarding semiconductor "
                      "packaging?"),
        ("courtlistener", "Recent dockets about chip export rules"),
        ("fred", "What is happening to the unemployment rate?"),
    ]

    for name, q in QUESTIONS:
        entry = reg.get(name)
        # respect each source's self-limit between smoke calls — GDELT's
        # 5s floor is real and its throttling is sticky for minutes after
        if entry is not None and entry.spec.min_interval_s:
            time.sleep(entry.spec.min_interval_s)
        if entry is None:
            print(f"[{name}] SKIPPED (not registered)")
            continue
        if entry.spec.key_env_var and \
                not os.environ.get(entry.spec.key_env_var):
            print(f"[{name}] SKIPPED (no {entry.spec.key_env_var})")
            continue
        plan = build_plan(name, q)
        if not plan.plannable:
            print(f"[{name}] NOT PLANNABLE: {plan.reason[:100]}")
            if plan.candidates:
                for slot, cands in plan.candidates.items():
                    print(f"    candidates[{slot}]: "
                          + ", ".join(f"{c.key}({c.confidence})"
                                      for c in cands[:4]))
            continue
        adapter = entry.make_adapter(
            RestSource(entry.spec, transport=curl_transport))
        try:
            bodies = execute(adapter, plan)
            body = bodies[0]
            if name == "openalex":
                r = body.get("results", [])
                print(f"[{name}] {body.get('meta', {}).get('count')} total; "
                      f"first: {(r[0].get('display_name') if r else 'NONE')}")
            elif name in ("semanticscholar",):
                data = body.get("data", [])
                print(f"[{name}] {len(data)} papers; first: "
                      f"{(data[0].get('title', 'NONE') if data else 'NONE')}")
            elif name == "worldbank":
                rows = body.get("rows", [])
                vals = [r["value"] for r in rows if r["value"] is not None]
                print(f"[{name}] total={body.get('total')} rows={len(rows)} "
                      f"non-null={len(vals)}; "
                      f"latest={rows[0]['date'] if rows else 'NONE'} "
                      f"{rows[0]['value'] if rows else ''}")
            elif name == "bea":
                d = body.get("BEAAPIs", {}).get("Results", {})
                data = d.get("Data", [])
                err = d.get("Error", body.get("Error"))
                print(f"[{name}] {len(data)} data rows"
                      + (f"; ERROR: {json.dumps(err)[:150]}" if err else
                         f"; first: {json.dumps(data[0])[:120] if data else 'NONE'}"))
            elif name == "census":
                cols = body.get("columns", [])
                rows = body.get("rows", [])
                print(f"[{name}] cols={cols}; {len(rows)} rows; "
                      f"first: {rows[0] if rows else 'NONE'}")
            elif name == "fdic":
                inst = body.get("data", [])
                print(f"[{name}] {len(inst)} institutions; first: "
                      f"{json.dumps(inst[0].get('data', inst[0]))[:140] if inst else 'NONE'}")
            elif name == "cftc_cot":
                rows = body.get("rows", [])
                print(f"[{name}] {len(rows)} weekly rows; first date: "
                      f"{rows[0].get('report_date_as_yyyy_mm_dd') if rows else 'NONE'}")
            elif name == "wayback":
                snap = (body.get("archived_snapshots", {})
                             .get("closest", {}))
                print(f"[{name}] closest: {snap.get('timestamp')} "
                      f"{snap.get('url', 'NONE')}")
            elif name == "gdelt":
                arts = body.get("articles", [])
                print(f"[{name}] {len(arts)} articles; first: "
                      f"{(arts[0].get('title') if arts else 'NONE')}")
            elif name == "wikidata":
                binds = body.get("results", {}).get("bindings", [])
                print(f"[{name}] {len(binds)} entities; first: "
                      f"{binds[0]['itemLabel']['value'] if binds else 'NONE'}")
            elif name == "treasury":
                rows = body.get("data", [])
                print(f"[{name}] {len(rows)} rows; first: "
                      f"{json.dumps(rows[0])[:120] if rows else 'NONE'}")
            elif name == "eia":
                data = body.get("data", [])
                print(f"[{name}] {len(data)} periods; first: "
                      f"{json.dumps(data[0])[:120] if data else 'NONE'}")
            elif name == "uspto_odp":
                apps = (body.get("patentFileWrapperDataGrid")
                        or body.get("patentFileWrapperDatas")
                        or [])
                print(f"[{name}] {len(apps)} applications; keys: "
                      f"{list(body)[:6]}")
            elif name == "courtlistener":
                res = body.get("results", [])
                print(f"[{name}] count={body.get('count')}; first: "
                      f"{(res[0].get('caseName') if res else 'NONE')}")
            else:
                print(f"[{name}] ok; keys: {list(body)[:6] if isinstance(body, dict) else type(body)}")
        except Exception as exc:
            print(f"[{name}] FAILED: {exc}")


if __name__ == "__main__":
    main()
