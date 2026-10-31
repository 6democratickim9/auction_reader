"""Offline regressions: python -m unittest test_search.py"""
import unittest
from unittest.mock import Mock, patch
import requests
import courtauction_insights as app

class SearchTests(unittest.TestCase):
    def test_partial_result_is_preserved_without_retry(self):
        session = Mock()
        response = Mock()
        response.json.return_value = {'rows': []}
        session.post.side_effect = [response, requests.ConnectionError('RemoteDisconnected')]
        row = {'사건번호': '2026타경1', '물건번호': '1', '소재지': '검증용 주소', '최저매각가_원': 123000000}
        with patch.object(app.requests, 'Session', return_value=session), patch.object(app, 'extract_rows', return_value=[row]), patch.object(app.time, 'sleep'):
            data, status = app.search_live('2026-10-31', '2026-11-14', 'B000210', 4, 10)
        self.assertEqual(data['rows'], [row])
        self.assertTrue(data['partial'])
        self.assertEqual(data['failed_stage'], '2페이지 조회')
        self.assertIn('4페이지 중 1페이지', status)
        self.assertNotIn('RemoteDisconnected', status)
        self.assertEqual(session.post.call_count, 2)
        session.close.assert_called_once()
        self.assertIn('전체 검색 결과가 아닙니다', app.result_dashboard(data['rows'], data))

    def test_first_connection_failure(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError('closed')
        with patch.object(app.requests, 'Session', return_value=session):
            data, status = app.search_live('2026-10-31', '2026-11-14', 'B000210', 1, 10)
        self.assertEqual(data['rows'], [])
        self.assertIn('조회하지 못했습니다', status)
        session.post.assert_not_called()

    def test_money_units_preserve_exact_amount(self):
        cases = [(None, "미확인"), (0, "0원"), (16000000, "1,600만 원"),
                 (156000000, "1억 5,600만 원"), (56438000, "5,643만 8,000 원"),
                 (1000000000000, "1조 원"), (9999, "9,999 원")]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(app.fmt_money(value), expected)

    def test_cards_escape_source_content(self):
        row = {'소재지': '<script>alert(1)</script>', '최저매각가_원': 100000000, '할인율_%': 0, '면적_㎡': 0}
        markup = app.result_dashboard([row], {'rows':[row]})
        self.assertNotIn('<script>', markup)
        self.assertIn('&lt;script&gt;', markup)
        self.assertIn('1억 원', markup)
        self.assertIn('0% 할인', markup)


class ComparisonCacheTests(unittest.TestCase):
    def test_revisit_reuses_result_and_refresh_reloads(self):
        row = {'사건번호': 'test', '소재지': '검증 주소'}
        result = ('조회 결과', None, app.pd.DataFrame(), None)
        with patch.object(app, 'insights', return_value=result) as lookup:
            first = app.cached_insights({}, [row], '', '', '', '', 1, False)
            second = app.cached_insights(first[0], [row], '', '', '', '', 1, False)
            self.assertEqual(lookup.call_count, 1)
            self.assertEqual(second[1], '조회 결과')
            app.refresh_insights(second[0], [row], '', '', '', '', 1, False)
            self.assertEqual(lookup.call_count, 2)
            app.cached_insights(second[0], [row], '', '', '', '', 2, False)
            self.assertEqual(lookup.call_count, 3)

    def test_card_opens_correct_property(self):
        rows = [{'사건번호':'first'}, {'사건번호':'second'}]
        active, tabs, detail = app.open_card(rows, app.gr.SelectData(None, {'index':1,'value':'1'}))
        self.assertEqual(active, [rows[1]])
        self.assertEqual(tabs['selected'], 'compare')
        self.assertIn('second', detail)



class FullSearchTests(unittest.TestCase):
    def run_pages(self, total, repeat=False):
        session = Mock()
        calls = []
        def post(url, **kwargs):
            page = kwargs['json']['dma_pageInfo']['pageNo']
            calls.append(page)
            response = Mock()
            response.json.return_value = {'data': {'dma_pageInfo': {'totalCnt':total,'pageSize':10}},
                                          'test_rows':[{'사건번호':str(1 if repeat else page),'소재지':str(1 if repeat else page)}]}
            return response
        session.post.side_effect = post
        with patch.object(app.requests,'Session',return_value=session), patch.object(app,'extract_rows',side_effect=lambda p:p['test_rows']), patch.object(app.time,'sleep'):
            result = app.search_live('2026-10-31','2026-11-14','B000210',0,10)
        return result, calls

    def test_all_pages_beyond_previous_limit(self):
        (data, status), calls = self.run_pages(65)
        self.assertEqual(calls, list(range(1,8)))
        self.assertFalse(data['partial'])
        self.assertEqual(len(data['rows']),7)

    def test_repeated_page_stops_and_preserves_data(self):
        (data,status),calls = self.run_pages(65,True)
        self.assertEqual(calls,[1,2])
        self.assertTrue(data['partial'])
        self.assertEqual(len(data['rows']),1)

    def test_type_filter_and_discovered_choices(self):
        rows = [{'物':'a','물건용도':'단독주택'}, {'物':'b','물건용도':'아파트'}, {'物':'c','물건용도':'새 용도'}]
        args = [{'rows':rows,'payloads':[]},'전체','',0,0,0,0,0,100,0,0,'㎡','기본 순서']
        self.assertEqual(len(app.render(*args, [])[0]),3)
        result = app.render_dashboard(*args,['단독주택','아파트'])
        self.assertEqual(len(result[0]),2)
        self.assertIn('새 용도',result[-1]['choices'])
        config = app.build_app().get_config_file()
        controls = [c for c in config['components'] if c['props'].get('label')=='물건 용도']
        self.assertEqual(len(controls),1)
        self.assertTrue(controls[0]['props']['multiselect'])

if __name__ == '__main__':
    unittest.main()
