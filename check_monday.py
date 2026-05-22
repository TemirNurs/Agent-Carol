import json

with open('data/memory/active_bids.json') as f:
    bids = json.load(f)

monday_bids = [bid for bid in bids if bid['due_date'] == '4/13/2026']
monday_bids.sort(key=lambda x: x.get('distance_miles', 999) or 999)

print(f'BIDS DUE MONDAY 4/13/2026: {len(monday_bids)} total')
print('-' * 60)
for i, bid in enumerate(monday_bids, 1):
    source_tag = '(BC)' if bid['source'] == 'buildingconnected' else '(CC)'
    dist = f"{bid.get('distance_miles', '?')} mi" if bid.get('distance_miles') else '? mi'
    size = f" - {bid.get('size_sf', '')} SF" if bid.get('size_sf') else ""
    print(f"{i:2}. {bid['project_name']} {source_tag} - {bid['gc']} - {dist}{size}")