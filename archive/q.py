import json
for l in open('tv24.jsonl', encoding='utf-8'):
    r = json.loads(l)
    if r.get('stage') == 'detail':
        print(r.get('fsn'), '|', r.get('savings'), '|', r.get('model_year'),
              '|', r.get('screen_size'), '|', r.get('estimated_annual_electricity_use'),
              '|', r.get('delivery_availability'))
