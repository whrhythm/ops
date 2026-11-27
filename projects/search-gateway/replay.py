import json
from pathlib import Path
from app import ranked_results
r=json.loads(Path('../../platform-control/release/p1-search-rebuild-v11-matrix.json').read_text())
rows=[]
for case in r['matrix']:
 for run in case['runs']:
  for call in run.get('calls',[]):
   if call['name']!='project_web_search': continue
   out=call.get('output','')
   try: payload=json.loads(out[out.index('{'):])
   except (ValueError,KeyError): continue
   query=json.loads(call['args'])['query']
   candidates=payload.get('candidates',[])
   filtered=ranked_results(query,[dict(x,content=x.get('snippet','')) for x in candidates])
   rows.append(dict(case=case['name'],before=len(candidates),after=len(filtered)))
Path('../../platform-control/release/p1-search-rebuild-quality-replay.json').write_text(json.dumps(rows))
print(json.dumps(rows))
