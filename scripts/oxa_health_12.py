"""One-off: probe the 12 sources named as working in the JOB brief."""
import sys
sys.path.insert(0, ".")
from tools.sources.health import run_all, render_table

names = ["fred", "treasury", "fdic", "courtlistener", "gdelt", "kalshi",
         "openalex", "wayback", "wikidata", "worldbank", "census",
         "clinicaltrials"]
print(render_table(run_all(names)))
