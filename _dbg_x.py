
import sys, inspect
sys.path.insert(0, ".")
import tools.pipeline.engine as E
src = inspect.getsource(E.ResearchPipeline._run_inner)
i = src.find("Phase B")
print(src[src.find("# 6."):src.find("# 8. Seal")][:4500])
