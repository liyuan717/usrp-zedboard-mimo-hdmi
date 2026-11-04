import copy,importlib.util,json,tempfile,unittest
from pathlib import Path
S=Path(__file__).resolve().parents[1]
def module(n):
 spec=importlib.util.spec_from_file_location(n,S/'scripts'/n);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
class SkillTests(unittest.TestCase):
 def setUp(self):
  self.m=module('check_evidence.py')
  self.result=dict(technical_pass=True,new_RF=True,new_ADC=True,historical_redisplay=False,original_file_byte_identical=True,LO_closed=True,ADC_closed=True,run_id='new',boot_id='fresh',kernel_errors=[],RF_frames=837,CRC_correct_packets=1674,HDMI_frames=25,ADC_blocks=5120,original_file_sha256='a'*64,continuous_realtime_verified=False,presentation_cadence={'nominal_5fps_grid_followed':False})
 def test_success_never_implies_visual_or_uniform_fps(self):
  r=self.m.check(self.result);self.assertEqual(r['visual'],'not_observed');self.assertFalse(r['uniform_5fps']);self.assertFalse(r['continuous_realtime_verified'])
 def test_old_iq_failure_missing_health_rejected(self):
  for key,value in [('new_ADC',False),('new_RF',None),('historical_redisplay',True),('kernel_errors',['Oops']),('CRC_correct_packets',1672),('technical_pass',False),('LO_closed',False)]:
   x=copy.deepcopy(self.result);x[key]=value
   with self.assertRaises(ValueError):self.m.check(x)
 def test_identity_bound_visual(self):
  self.assertEqual(self.m.check(self.result,dict(run_id='new',choice='stable'))['visual'],'stable')
  with self.assertRaises(ValueError):self.m.check(self.result,dict(run_id='old',choice='stable'))
 def test_wrong_expected_file_hash_rejected(self):
  with self.assertRaises(ValueError):self.m.check(self.result,expected_hash='b'*64)
 def test_real_saved_results(self):
  self.assertTrue(self.m.check(json.loads((S/'tests/fixtures/new_rf_result.json').read_text()))['technical_pass'])
  with self.assertRaises(ValueError):self.m.check(json.loads((S/'tests/fixtures/failed_rf_result.json').read_text()))
 def test_byte_compare_detects_corruption(self):
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   x=Path(tmp)/'a';y=Path(tmp)/'b';x.write_bytes(b'original bytes');y.write_bytes(x.read_bytes())
   cmd=[sys.executable,str(S/'scripts/compare_bytes.py'),str(x),str(y)]
   self.assertEqual(subprocess.run(cmd,capture_output=True).returncode,0)
   y.write_bytes(b'Original bytes');self.assertNotEqual(subprocess.run(cmd,capture_output=True).returncode,0)
 def test_reports_accept_recorded_summary_reject_negative_slack(self):
  import subprocess,sys
  fixtures=S/'tests/fixtures';cmd=[sys.executable,str(S/'scripts/check_reports.py'),'--utilization',str(fixtures/'utilization.rpt'),'--timing']
  self.assertEqual(subprocess.run(cmd+[str(fixtures/'timing_summary_excerpt.rpt')],capture_output=True).returncode,0)
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'bad.rpt';text=(fixtures/'timing_summary_excerpt.rpt').read_text();p.write_text(text.replace('0.160','-0.160',1))
   self.assertNotEqual(subprocess.run(cmd+[str(p)],capture_output=True).returncode,0)
if __name__=='__main__':unittest.main()
