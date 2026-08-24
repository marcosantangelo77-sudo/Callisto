import os
from tools.sources.base import RestSource
from tools.sources import federalreserve as frmod
from tools.sources import pubmed as pmmod

src = RestSource(frmod.SPEC)
ad = frmod.FederalReserveAdapter(src)
sp = ad.recent_speeches()
print("speeches:", len(sp), sp[0]["title"][:60] if sp else "-")
mp = ad.monetary_policy_items()
print("monetary policy items:", len(mp))

src2 = RestSource(pmmod.SPEC)
ad2 = pmmod.PubMedAdapter(src2)
r = ad2.search("semaglutide cardiovascular outcomes", limit=3)
print("pubmed count:", r["count"], "pmids:", r["pmids"])
s = ad2.summarize(r["pmids"])
for k, v in list(s.items()):
    if k == "_fetch":
        continue
    print(" ", k, v.get("title", "")[:60], "|", v.get("journal"))
