import json, sys

for fname in ['tmp_edges.json', 'tmp_edges2.json']:
    try:
        with open(fname) as f:
            data = json.load(f)
        
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get('edges', data.get('opportunities', [data]))
        else:
            continue
            
        for item in items:
            s = json.dumps(item).lower()
            if 'mariner' in s or 'seattle' in s or 'guardian' in s or 'cleveland' in s:
                print(f"=== {fname} ===")
                print(json.dumps(item, indent=2))
                print()
    except Exception as e:
        print(f"{fname}: {e}")

sys.stdout.flush()
