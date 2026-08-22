import re, sys
sys.path.insert(0,'.')
from tests.helpers.no_socket import NoSocket
NoSocket().install()
from tests.test_build_gaps import _registry,_spec,_trace,_question
from tools.gaps import classify_gap
reg=_registry(_spec("openalex",["scholarly work search"]))
t=_trace(rejected=0, admitted=0, rounds=[{"round":1,"query":"q","sources":[{"name":"openalex","skipped":"no authored query"}],"admitted":0}])
t.skipped_sources=[{"name":"openalex","reason":"no authored query"}]
g=classify_gap(reg,t,_question())
print(g.kind); print(g.why_not_obtained)
for c in g.candidates: print("cand:",c.name,c.tried,c.obstacle)
