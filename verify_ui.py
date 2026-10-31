"""Offline UI smoke checks. Run: python verify_ui.py"""
from courtauction_insights import build_app, render

app = build_app()
config = app.get_config_file()
ids = {c['props'].get('elem_id') for c in config['components']}
assert {'hero', 'filter-panel', 'summary', 'results-table', 'detail'} <= ids
assert len(config['dependencies']) >= 7
args = [{"rows": [], "payloads": []}, '전체', '', 0, 0, 0, 0, 0, 100, 0, 0, '㎡', '기본 순서']
result = render(*args)
assert len(result) == 7 and result[0] == [] and result[2].empty
args[5], args[6] = 10, 5
assert '조건 오류' in render(*args)[1]
print('PASS: UI construction, event bindings, empty results, invalid filter range')
