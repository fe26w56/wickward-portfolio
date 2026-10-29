import copy
import csv
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import demo

class DemoTests(unittest.TestCase):
    def setUp(self):
        with (demo.ROOT/'data/synthetic-week.csv').open() as f:self.rows=list(csv.DictReader(f))
    def test_complete_week_has_32_median_forecasts(self):
        forecasts=demo.forecast(self.rows)
        self.assertEqual(len(forecasts),32)
        self.assertEqual(forecasts[0]['wait_minutes'],11)
    def test_missing_data_is_rejected(self):
        with self.assertRaises(ValueError):demo.forecast(self.rows[1:])
    def test_duplicate_is_rejected(self):
        with self.assertRaises(ValueError):demo.forecast(self.rows+[self.rows[0]])
    def test_future_observation_is_rejected(self):
        self.rows[0]['observed_at']='2035-04-10T09:00:00+00:00'
        with self.assertRaises(ValueError):demo.forecast(self.rows)
    def test_invalid_wait_is_rejected(self):
        for invalid in ['NaN','inf','-1','241']:
            with self.subTest(invalid=invalid):
                rows=copy.deepcopy(self.rows);rows[0]['wait_minutes']=invalid
                with self.assertRaises(ValueError):demo.forecast(rows)
    def test_original_engine_returns_feasible_route_without_mutating_input(self):
        request=demo.make_request(demo.forecast(self.rows));original=copy.deepcopy(request)
        result=demo.engine.create_plan(request,route_evaluation_budget=500,max_intentional_wait_minutes=30)
        self.assertTrue(result['final_validation']['feasible'])
        self.assertIn('sky',[s['id'] for s in result['final_validation']['steps']])
        self.assertEqual(original,request)
    def test_html_escapes_external_text(self):
        result={'status':'<script>alert(1)</script>','final_validation':{'feasible':False,'steps':[]}}
        rendered=demo.render(result,demo.forecast(self.rows))
        self.assertNotIn('<script>',rendered)
        self.assertIn('&lt;script&gt;',rendered)
        result['final_validation']['steps']=[{'id':'sky','arrival_at':demo.ts(), 'planning_finish_at':demo.ts(480)}]
        rendered=demo.render(result,demo.forecast(self.rows))
        self.assertIn('04/10 18:00 → 04/11 02:00 JST',rendered)
        self.assertIn('00:00 翌日',rendered)
        self.assertIn('原本の時刻はUTC',rendered)

if __name__=='__main__':unittest.main()
