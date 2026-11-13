#!/usr/bin/env python3
"""Compile real CRC/FEC ingress mutations; require specific functional witnesses."""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 if a.audit_existing:assert out.is_dir()
 else:out.mkdir(parents=True,exist_ok=False)
 suffix='audit-' if a.audit_existing else ''
 before=disk_check('before-crc-fec-controls-'+suffix);(out/('disk-'+suffix+'before.json')).write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
 try:
  receipt=json.loads((ref/'receipt.json').read_text());assert receipt['simulation'].startswith('G0_CRC_FEC_PIPELINE_PASS');hashes=receipt['source_sha256']
  for n,h in hashes.items():assert hashlib.sha256((project/'sim/g0'/n).read_bytes()).hexdigest()==h
  changes=[
   ('CRC_bypass','link_packet_cost_probe.sv','if({in_data,received_crc[31:8]}!=~crc)result_error<=1;',"if(1'b0)result_error<=1;",'Fatal: ingress CRC-bad descriptor admitted'),
   ('ignore_partial_packet_abort','link_fec_ingress_cost_probe.sv','wire core_packet_abort=packet_abort || bad_data;','wire core_packet_abort=bad_data;','Fatal: ingress current abort fence'),
   ('reopen_completed_block','link_fec_ingress_cost_probe.sv','wire declare_new=!bank_known[declare_bank] ||','wire declare_new=!bank_live[declare_bank] ||','Fatal: ingress completed block reopened by repeat'),
   ('accept_stale_range','link_fec_ingress_cost_probe.sv','descriptor_shape && serial_ok && !retired','descriptor_shape && !retired','Fatal: ingress stale range replay accepted'),
   ('keep_old_session_engine','link_fec_ingress_cost_probe.sv','wire core_rst=rst || session_switch;','wire core_rst=rst;','Fatal: ingress held source changed'),
  ];rows=[]
  for label,target,old,new,witness in changes:
   d=out/label;s=(ref/target).read_text();assert s.count(old)==1,(label,s.count(old));mutant=s.replace(old,new)
   if a.audit_existing:
    for n in hashes:assert (d/n).read_bytes()==(mutant.encode() if n==target else (ref/n).read_bytes()),(label,n)
    for p in [*ref.glob('*.hex'),ref/'tb.sv']:assert (d/p.name).read_bytes()==p.read_bytes(),(label,p.name)
   else:
    d.mkdir()
    for p in ref.glob('*.hex'):os.link(p,d/p.name)
    for n in hashes:shutil.copyfile(ref/n,d/n)
    shutil.copyfile(ref/'tb.sv',d/'tb.sv');(d/target).write_text(mutant)
   def run(step,args):
    with (d/(step+'.log')).open('w') as f:subprocess.run(args,cwd=d,stdout=f,stderr=subprocess.STDOUT,check=True)
   if not a.audit_existing:
    run('compile',[str(bp/'xvlog'),'--sv',*hashes,'tb.sv']);run('elaborate',[str(bp/'xelab'),'tb','-s','crc_fec_control_tb','-mt','off'])
    try:run('simulate',[str(bp/'xsim'),'crc_fec_control_tb','-runall'])
    except subprocess.CalledProcessError:pass
   else:
    assert 'ERROR:' not in (d/'compile.log').read_text() and 'ERROR:' not in (d/'elaborate.log').read_text()
   log=(d/'simulate.log').read_text();line=next((x for x in log.splitlines() if x.startswith(witness)),None)
   assert line and 'timeout' not in line.lower() and 'G0_CRC_FEC_PIPELINE_PASS' not in log and '$finish called at time' in log and 'Exiting xsim' in log,(label,log[-3500:])
   row={'name':label,'detected_by':line};rows.append(row);print(json.dumps(row),flush=True)
  (out/'receipt.json').write_text(json.dumps({'scope':__doc__,'source_sha256':hashes,'negative_controls':rows,'existing_terminal_logs_audited_without_rerun':a.audit_existing,'all_mutated_sources_TB_and_vectors_checked_against_positive':True,'g0_passed':False},indent=2)+'\n')
 finally:(out/('disk-'+suffix+'after.json')).write_text(json.dumps(disk_check('after-crc-fec-controls-'+suffix),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ('output','reference','vivado-bin'):p.add_argument('--'+n,type=Path,required=True)
 p.add_argument('--audit-existing',action='store_true');main(p.parse_args())
