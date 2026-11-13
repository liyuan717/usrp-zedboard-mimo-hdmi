#!/usr/bin/env python3
"""Post-route compact ingress BRAM input cones must start at registers/constants."""
import argparse,json,subprocess,re
from pathlib import Path

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);dcp=a.checkpoint.resolve()
 tcl='open_checkpoint {'+str(dcp)+'}\n'+r'''
set memories [get_cells -hier -filter {NAME =~ *ingress/elastic/* && (REF_NAME == RAMB36E1 || REF_NAME == RAMB18E1)}]
if {[llength $memories] == 0} {error "compact ingress BRAM not found"}
set fd [open boundary.tsv w]
set count 0
foreach ram $memories {
 set pins [get_pins -of_objects $ram -filter {DIRECTION == IN && (REF_PIN_NAME =~ EN* || REF_PIN_NAME =~ WE* || REF_PIN_NAME =~ ADDR* || REF_PIN_NAME =~ RST* || REF_PIN_NAME =~ REGCE* || REF_PIN_NAME =~ DI*)}]
 foreach pin $pins {
  set starts [all_fanin -flat -startpoints_only -to $pin]
  foreach start $starts {
   if {[get_property CLASS $start] eq "port"} {error "primary input reaches BRAM boundary: $start -> $pin"}
   set cell [get_cells -of_objects $start]
   set ref [get_property REF_NAME $cell]
   if {$ref ni {FDRE FDSE VCC GND}} {error "unexpected boundary driver $ref: $start -> $pin"}
   if {$ref in {FDRE FDSE}} {
    set clock [get_clocks -of_objects [get_pins -of_objects $cell -filter {REF_PIN_NAME == C}]]
    if {[llength $clock]!=1 || [get_property NAME $clock] ne "processing" || abs([get_property PERIOD $clock]-20.0)>0.000001} {error "unexpected BRAM boundary clock: $cell"}
   }
   puts $fd "$pin\t$start\t$ref"
  }
  incr count
 }
}
close $fd
puts "G0_COMPACT_MEMORY_BOUNDARY_PASS memories=[llength $memories] checked_inputs=$count"
'''
 (out/'audit.tcl').write_text(tcl)
 with (out/'audit.log').open('w') as log:subprocess.run([str(a.vivado_bin.resolve()/'vivado'),'-mode','batch','-source',str(out/'audit.tcl'),'-nolog','-nojournal'],cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
 log=(out/'audit.log').read_text();match=re.search(r'^G0_COMPACT_MEMORY_BOUNDARY_PASS memories=(\d+) checked_inputs=(\d+)$',log,re.M);assert match and int(match[1])>0 and int(match[2])>0
 r={'scope':__doc__,'checkpoint':str(dcp),'result':match[0],'memories':int(match[1]),'checked_inputs':int(match[2]),'register_clock':'processing','register_clock_period_ns':20.0,'detailed_startpoints':'boundary.tsv','full_board_CDC_signoff':False};(out/'receipt.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--checkpoint',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
