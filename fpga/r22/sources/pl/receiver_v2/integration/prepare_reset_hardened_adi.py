#!/usr/bin/env python3
"""Prepare or validate the independently pinned r6 HDMI-reset ADI overlay."""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, shutil, stat
from pathlib import Path

HERE=Path(__file__).resolve().parent
STATIC=HERE/'reset_hardened_adi_manifest.json'
RECEIPT='RESET_HARDENED_ADI_RECEIPT.json'
OLD_RECEIPT='RXONLY_OVERLAY_RECEIPT.json'
CHANGED=(
 'library/axi_hdmi_tx/axi_hdmi_tx.v','library/axi_hdmi_tx/axi_hdmi_tx_core.v',
 'library/axi_hdmi_tx/axi_hdmi_tx_vdma.v','library/common/up_hdmi_tx.v',
 'library/axi_hdmi_tx/axi_hdmi_tx_reset_sync.v','library/axi_hdmi_tx/axi_hdmi_tx_ip.tcl',
 'library/axi_hdmi_tx/axi_hdmi_tx_axis_epoch_bridge.v','library/axi_hdmi_tx/component.xml')
PARENT_INPUTS=(
 'library/axi_hdmi_tx/axi_hdmi_tx.v','library/axi_hdmi_tx/axi_hdmi_tx_core.v',
 'library/axi_hdmi_tx/axi_hdmi_tx_vdma.v','library/common/up_hdmi_tx.v','library/common/ad_rst.v')

def sha(p:Path)->str:return hashlib.sha256(p.read_bytes()).hexdigest()
def digest(records:list[str])->str:return hashlib.sha256(('\n'.join(records)+'\n').encode()).hexdigest()
def reject_root_link(path:Path,label:str)->Path:
    """Reject a root symlink/junction before resolving it to another tree."""
    candidate=path.absolute()
    try: mode=candidate.lstat().st_mode
    except FileNotFoundError: raise ValueError(f'{label} does not exist: {candidate}')
    attributes=getattr(candidate.lstat(),'st_file_attributes',0)
    if stat.S_ISLNK(mode) or attributes & 0x400:
        raise ValueError(f'{label} root link/reparse point forbidden: {candidate}')
    return candidate.resolve(strict=True)
def admission_controls(parent:Path,include_static:bool)->dict[str,Path]:
    controls={'preparer':Path(__file__).resolve(),'patcher':HERE/'hdmi_reset_patch.py',
      'helper':HERE/'hdmi_reset_patch_sources/axi_hdmi_tx_reset_sync.v',
      'axis_bridge_helper':HERE/'hdmi_reset_patch_sources/axi_hdmi_tx_axis_epoch_bridge.v',
      'parent_receipt':parent/OLD_RECEIPT}
    if include_static: controls['static_manifest']=STATIC
    return controls
def control_hashes(controls:dict[str,Path])->dict[str,str]:
    # Keys deliberately identify roles, not machine-specific absolute paths:
    # frozen admission must remain reproducible after being copied elsewhere.
    return {key:sha(path) for key,path in controls.items()}
def control_path_audit(controls:dict[str,Path])->dict[str,str]:
    return {key:str(path.resolve()) for key,path in controls.items()}
def require_unchanged(before:dict[str,str],controls:dict[str,Path],label:str)->dict[str,str]:
    after=control_hashes(controls)
    if after!=before: raise ValueError(f'{label} changed during preparation')
    return after
def replace_once(data:str,before:str,after:str,label:str)->str:
    count=data.count(before)
    if count!=1: raise ValueError(f'{label}: expected exactly one anchor, found {count}')
    return data.replace(before,after)
def records(root:Path,exclude:set[str]=set()):
    out=[]
    for p in sorted(root.rglob('*')):
        m=p.lstat()
        if stat.S_ISLNK(m.st_mode) or getattr(m,'st_file_attributes',0)&0x400: raise ValueError('link/reparse point forbidden')
        if p.is_file() and p.relative_to(root).as_posix() not in exclude: out.append(p.relative_to(root).as_posix()+'\t'+sha(p))
    return out
def load_module(name,path):
    s=importlib.util.spec_from_file_location(name,path); m=importlib.util.module_from_spec(s); assert s and s.loader; s.loader.exec_module(m); return m
def load_static():
    m=json.loads(STATIC.read_text(encoding='utf-8'))
    if m.get('schema')!='receiver-v2-reset-hardened-r6/v1' or set(m.get('changed_file_sha256',{}))!=set(CHANGED): raise ValueError('invalid r6 static manifest')
    return m
def patch_ip(path:Path):
    s=path.read_text(encoding='utf-8'); a='  "axi_hdmi_tx_frame_mailbox.v" \\\n+  "axi_hdmi_tx_vdma.v"'
    b='  "axi_hdmi_tx_frame_mailbox.v" \\\n+  "axi_hdmi_tx_reset_sync.v" \\\n+  "axi_hdmi_tx_axis_epoch_bridge.v" \\\n+  "axi_hdmi_tx_vdma.v"'
    a=a.replace("\n+", "\n"); b=b.replace("\n+", "\n")
    if s.count(a)!=1: raise ValueError('ip Tcl helper insertion anchor mismatch')
    path.write_text(s.replace(a,b),encoding='utf-8',newline='\n')
def patch_component(path:Path):
    s=path.read_text(encoding='utf-8'); a='''      <spirit:file>
        <spirit:name>axi_hdmi_tx_frame_mailbox.v</spirit:name>
        <spirit:fileType>verilogSource</spirit:fileType>
      </spirit:file>
      <spirit:file>
        <spirit:name>axi_hdmi_tx_vdma.v</spirit:name>'''
    b='''      <spirit:file>
        <spirit:name>axi_hdmi_tx_frame_mailbox.v</spirit:name>
        <spirit:fileType>verilogSource</spirit:fileType>
      </spirit:file>
      <spirit:file>
        <spirit:name>axi_hdmi_tx_reset_sync.v</spirit:name>
        <spirit:fileType>verilogSource</spirit:fileType>
      </spirit:file>
      <spirit:file>
        <spirit:name>axi_hdmi_tx_axis_epoch_bridge.v</spirit:name>
        <spirit:fileType>verilogSource</spirit:fileType>
      </spirit:file>
      <spirit:file>
        <spirit:name>axi_hdmi_tx_vdma.v</spirit:name>'''
    if s.count(a)!=2: raise ValueError('component synthesis/simulation helper anchors mismatch')
    path.write_text(s.replace(a,b),encoding='utf-8',newline='\n')
def expected_static(candidate:Path,parent:dict):
    r=records(candidate,{RECEIPT}); changed={x:sha(candidate/x) for x in CHANGED}
    return {'schema':'receiver-v2-reset-hardened-r6/v1','parent_static_controls':parent,
      'changed_file_sha256':changed,'expected_output_tree_file_count':len(r),'expected_output_tree_sha256':digest(r)}
def parent_static_controls(original:dict,parent:Path)->dict:
    """Return the portable, content-derived identity of an admitted r5 tree.

    The r5 receipt's own hash changes when audit-only absolute paths are
    rewritten on another machine, so it is deliberately excluded here.  Its
    observed hash remains a runtime freeze/audit field in the r6 receipt.
    """
    return {'parent_overlay':'receiver-v2-rxonly-adi',
      'source_manifest_sha256':original['source_manifest_sha256'],'preparer_sha256':original['preparer_sha256'],
      'input_closure_sha256':original['input_closure_sha256'],'input_closure_count':len(original['input_closure_records']),
      'parent_output_tree_sha256':original['output_tree_sha256_excluding_receipt'],'parent_output_tree_count':len(original['output_tree_records_excluding_receipt']),
      'configuration':original['configuration'],'parent_input_sha256':{x:sha(parent/x) for x in PARENT_INPUTS}}
def prepare(parent:Path,out:Path,propose=False):
    if out.exists(): raise ValueError('output must be fresh')
    board=load_module('rbb',HERE/'run_board_build.py')
    parent=reject_root_link(parent,'parent r5'); out=out.absolute()
    controls=admission_controls(parent,not propose)
    controls_before=control_hashes(controls)
    parent_receipt_sha=board.validate_rxonly_overlay(parent)
    if parent_receipt_sha!=controls_before['parent_receipt']:
        raise ValueError('strict r5 admission returned a different parent receipt hash')
    original=json.loads((parent/OLD_RECEIPT).read_text(encoding='utf-8'))
    parent_controls=parent_static_controls(original,parent)
    shutil.copytree(parent,out)
    # The copied payload is accepted only if the original strict r5 admission
    # remains byte-identical after the copy and retains the initial receipt.
    if board.validate_rxonly_overlay(parent)!=parent_receipt_sha:
        raise ValueError('parent r5 receipt changed during copy')
    require_unchanged(controls_before,controls,'admission control source')
    (out/OLD_RECEIPT).unlink()
    hp=load_module('hrp',HERE/'hdmi_reset_patch.py')
    for rel,fn in {'library/axi_hdmi_tx/axi_hdmi_tx.v':hp.patch_top,'library/axi_hdmi_tx/axi_hdmi_tx_core.v':hp.patch_core,'library/axi_hdmi_tx/axi_hdmi_tx_vdma.v':hp.patch_vdma,'library/common/up_hdmi_tx.v':hp.patch_up}.items():
        p=out/rel; p.write_text(fn(hp.read_verified(parent,rel)),encoding='utf-8',newline='\n')
    shutil.copyfile(HERE/'hdmi_reset_patch_sources/axi_hdmi_tx_reset_sync.v',out/'library/axi_hdmi_tx/axi_hdmi_tx_reset_sync.v')
    shutil.copyfile(HERE/'hdmi_reset_patch_sources/axi_hdmi_tx_axis_epoch_bridge.v',out/'library/axi_hdmi_tx/axi_hdmi_tx_axis_epoch_bridge.v')
    patch_ip(out/'library/axi_hdmi_tx/axi_hdmi_tx_ip.tcl'); patch_component(out/'library/axi_hdmi_tx/component.xml')
    candidate=expected_static(out,parent_controls)
    if propose:
        require_unchanged(controls_before,controls,'admission control source')
        if board.validate_rxonly_overlay(parent)!=parent_receipt_sha:
            raise ValueError('parent r5 receipt changed before candidate publication')
        (out/'r6_manifest_candidate.json').write_text(json.dumps(candidate,indent=2,sort_keys=True)+'\n',encoding='utf-8'); return candidate
    static=load_static()
    if candidate!=static: raise ValueError('r6 output differs from reviewed static manifest')
    controls_after=require_unchanged(controls_before,controls,'admission control source')
    if board.validate_rxonly_overlay(parent)!=parent_receipt_sha:
        raise ValueError('parent r5 receipt changed before receipt publication')
    receipt={'schema':'receiver-v2-reset-hardened-r6-receipt/v1','status':'PREPARED_NOT_BUILT','parent_provenance_path':str(parent),'parent_static_controls':parent_controls,
      'actual_parent_receipt_sha256':parent_receipt_sha,
      'preparer_sha256':sha(Path(__file__).resolve()),'patcher_sha256':sha(HERE/'hdmi_reset_patch.py'),'helper_sha256':sha(HERE/'hdmi_reset_patch_sources/axi_hdmi_tx_reset_sync.v'),'axis_bridge_helper_sha256':sha(HERE/'hdmi_reset_patch_sources/axi_hdmi_tx_axis_epoch_bridge.v'),
      'admission_control_sha256_before':controls_before,'admission_control_sha256_after':controls_after,
      'admission_control_path_audit':control_path_audit(controls),
      'changed_file_sha256':candidate['changed_file_sha256'],'output_tree_records_excluding_receipt':records(out,{RECEIPT}),'output_tree_sha256_excluding_receipt':candidate['expected_output_tree_sha256']}
    (out/RECEIPT).write_text(json.dumps(receipt,indent=2,sort_keys=True)+'\n',encoding='utf-8'); return receipt
def validate_reset_hardened_adi(out:Path,integration=None):
    """Validate an already prepared r6 tree without reading its parent path.

    ``parent_provenance_path`` is retained solely for audit traceability.  A
    copied artifact remains verifiable on another machine because the checked
    parent identity is the reviewed static record, not a live path lookup.
    """
    if integration is not None and reject_root_link(Path(integration),'integration')!=HERE.resolve():
        raise ValueError('integration must be this frozen validator directory')
    static=load_static(); out=reject_root_link(out,'r6 output'); receipt=json.loads((out/RECEIPT).read_text(encoding='utf-8'))
    if receipt.get('schema')!='receiver-v2-reset-hardened-r6-receipt/v1': raise ValueError('r6 receipt schema mismatch')
    if 'parent_receipt_sha256' in static['parent_static_controls']:
        raise ValueError('r6 static manifest must not pin an r5 receipt byte hash')
    if receipt.get('status')!='PREPARED_NOT_BUILT' or receipt.get('parent_static_controls')!=static['parent_static_controls']: raise ValueError('r6 receipt provenance mismatch')
    if receipt.get('preparer_sha256')!=sha(Path(__file__).resolve()): raise ValueError('r6 preparer source hash mismatch')
    if receipt.get('patcher_sha256')!=sha(HERE/'hdmi_reset_patch.py'): raise ValueError('r6 HDMI patcher source hash mismatch')
    if receipt.get('helper_sha256')!=sha(HERE/'hdmi_reset_patch_sources/axi_hdmi_tx_reset_sync.v'): raise ValueError('r6 HDMI helper source hash mismatch')
    if receipt.get('axis_bridge_helper_sha256')!=sha(HERE/'hdmi_reset_patch_sources/axi_hdmi_tx_axis_epoch_bridge.v'): raise ValueError('r6 HDMI AXIS bridge helper source hash mismatch')
    frozen_before=receipt.get('admission_control_sha256_before')
    frozen_after=receipt.get('admission_control_sha256_after')
    expected_roles={'preparer','patcher','helper','axis_bridge_helper','static_manifest','parent_receipt'}
    if not isinstance(frozen_before,dict) or set(frozen_before)!=expected_roles or frozen_before!=frozen_after:
        raise ValueError('r6 admission-control freeze mismatch')
    if receipt.get('actual_parent_receipt_sha256')!=frozen_before['parent_receipt']:
        raise ValueError('r6 runtime parent receipt hash does not match frozen parent receipt')
    current_controls={'preparer':Path(__file__).resolve(),'patcher':HERE/'hdmi_reset_patch.py',
      'helper':HERE/'hdmi_reset_patch_sources/axi_hdmi_tx_reset_sync.v','static_manifest':STATIC}
    current_controls['axis_bridge_helper']=HERE/'hdmi_reset_patch_sources/axi_hdmi_tx_axis_epoch_bridge.v'
    for role,path in current_controls.items():
        if frozen_before.get(role)!=sha(path):
            raise ValueError('r6 current admission-control source hash mismatch')
    if receipt.get('changed_file_sha256')!=static['changed_file_sha256']: raise ValueError('r6 receipt changed-file pin mismatch')
    r=records(out,{RECEIPT})
    if len(r)!=static['expected_output_tree_file_count'] or digest(r)!=static['expected_output_tree_sha256']: raise ValueError('r6 complete tree differs from static manifest')
    if receipt.get('output_tree_records_excluding_receipt')!=r or receipt.get('output_tree_sha256_excluding_receipt')!=digest(r): raise ValueError('r6 receipt cannot self-sign changed tree')
    if {x:sha(out/x) for x in CHANGED}!=static['changed_file_sha256']: raise ValueError('r6 changed-file pin mismatch')
    return sha(out/RECEIPT)
validate=validate_reset_hardened_adi
def main():
 p=argparse.ArgumentParser(); p.add_argument('--parent-r5',type=Path);p.add_argument('--out',type=Path,required=True);p.add_argument('--propose-manifest',action='store_true');p.add_argument('--validate',action='store_true');a=p.parse_args()
 if a.validate: print(validate_reset_hardened_adi(a.out)); return
 if not a.parent_r5: p.error('--parent-r5 required for preparation')
 print(json.dumps(prepare(a.parent_r5,a.out,a.propose_manifest),indent=2,sort_keys=True))
if __name__=='__main__':main()
