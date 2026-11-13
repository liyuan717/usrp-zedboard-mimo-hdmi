# Fail-closed typed CDC release gate for an already-open, routed design.
# Usage (inside the final implementation Tcl, after route):
#   source receiver_v2_typed_cdc_gate.tcl
#   receiver_v2_typed_cdc_gate::validate <fresh-out> <frozen-integration> \
#       <reviewed-info.json> <python> <vivado>
#
# The existing exact-pair/sync-stage observers intentionally open a checkpoint.
# This gate therefore writes a hash-bound snapshot of *this* in-memory routed
# design and launches the unmodified frozen observers against that snapshot.
# No exception, waiver, or constraint is created here.
namespace eval ::receiver_v2_typed_cdc_gate {

  proc fail {message} { error "RXV2_TYPED_CDC_GATE_FAIL: $message" }
  proc load_contracts {integration} {
    namespace eval ::receiver_v2_external_io_contract {variable library_only 1}
    foreach name {capture_reset_guard_contract.tcl release_assertion_contract.tcl release_assertion_effect.tcl hdmi_epoch_cdc_contract.tcl hdmi_gray_pointer_contract.tcl hdmi_mailbox_exact_contract.tcl receiver_v2_external_io_contract.tcl} {
      # source inherits the caller namespace. These libraries own global
      # namespaces, including when invoked from the production signoff proc.
      uplevel #0 [list source [file join $integration $name]]
    }
  }
  proc assert_route_complete {report} {
    if {[string first "Design Route Status" $report] < 0} {fail "unrecognized route status report"}
    set counts [dict create]
    foreach line [split $report \n] {
      if {[regexp {^[ \t]*# of ([a-z ]+)\.+[ \t]*:[ \t]*([0-9]+)[ \t]*:} $line unused label count]} {
        if {[dict exists $counts $label]} {fail "duplicate route status count $label"}
        dict set counts $label $count
      }
    }
    set expected {{logical nets} {nets not needing routing} {internally routed nets} {nets with no loads} {routable nets} {fully routed nets} {nets with routing errors}}
    if {[lsort [dict keys $counts]] ne [lsort $expected]} {fail "missing/extra route status counts"}
    foreach label $expected {set value($label) [dict get $counts $label]}
    if {$value(nets with routing errors) != 0 || $value(routable nets) <= 0 ||
        $value(fully routed nets) != $value(routable nets) ||
        $value(logical nets) != $value(nets not needing routing) + $value(routable nets) ||
        $value(nets not needing routing) != $value(internally routed nets) + $value(nets with no loads)} {
      fail "incomplete/error routing or inconsistent route accounting: $counts"
    }
    return $counts
  }
  proc one {objects label} { if {[llength $objects] != 1} { fail "$label expected one object, got '$objects'" }; return [lindex $objects 0] }
  proc sha256 {path} {
    if {![file isfile $path]} { fail "cannot hash missing file $path" }
    if {[catch {exec -- certutil -hashfile [file normalize $path] SHA256} output]} { fail "certutil failed: $output" }
    set hashes {}
    foreach raw [split $output "\n"] {
      set line [string trim $raw]
      if {[regexp -nocase {^[0-9a-f]{64}$} $line]} {
        lappend hashes [string tolower $line]
      } elseif {[regexp -nocase {^[0-9a-f]{2}([[:space:]]+[0-9a-f]{2}){31}$} $line]} {
        set canonical [string map [list " " "" "\t" ""] $line]
        if {![regexp -nocase {^[0-9a-f]{64}$} $canonical]} { fail "malformed spaced SHA256 line for $path" }
        lappend hashes [string tolower $canonical]
      }
    }
    if {[llength $hashes] != 1} { fail "ambiguous SHA256 output for $path" }
    return [lindex $hashes 0]
  }
  proc must_pass {name script ledger} {
    if {[catch {uplevel #0 $script} message]} { puts $ledger "$name\tFAIL\t[string map [list "\n" { } "\t" { }] $message]"; flush $ledger; fail "$name: $message" }
    puts $ledger "$name\tPASS\tverified"; flush $ledger
  }
  proc write_pairs {inventory pairs} {
    set code {
import json, sys
d=json.load(open(sys.argv[1], encoding="utf-8"))
fields=("kind","cdc_id","source_clock","destination_clock","source","destination","source_parent","destination_parent","bit")
pairs=d.get("expanded_exact_pairs")
assert isinstance(pairs,list) and pairs
with open(sys.argv[2],"w",encoding="utf-8",newline="\n") as f:
  f.write("\t".join(fields)+"\n")
  for p in pairs:
    assert all(k in p for k in fields)
    values=[str(p[k]) for k in fields]
    assert not any("\t" in v or "\n" in v for v in values)
    f.write("\t".join(values)+"\n")
print(len(pairs))
}
    if {[catch {exec -- $::receiver_v2_typed_cdc_gate::python -I -c $code $inventory $pairs} result]} { fail "cannot create exact pair TSV: $result" }
    if {![string is integer -strict [string trim $result]] || $result <= 0} { fail "invalid exact pair count: $result" }
    return [string trim $result]
  }
  proc validate_info {report manifest helpers} {
    set code {
import json, sys
sys.path.insert(0, sys.argv[3])
from cdc_info_manifest import info_rows
m=json.load(open(sys.argv[2], encoding="utf-8"))
assert m.get("schema")=="rfv2-cdc-info-manifest-v1" and m.get("reviewed") is True
assert m.get("info_rows")==info_rows(__import__("pathlib").Path(sys.argv[1]))
print(len(m["info_rows"]))
}
    if {[catch {exec -- $::receiver_v2_typed_cdc_gate::python -I -c $code $report $manifest $helpers} result]} { fail "Info manifest is absent/unreviewed/mismatched: $result" }
    if {![string is integer -strict [string trim $result]]} { fail "invalid reviewed Info count: $result" }
    return [string trim $result]
  }
  proc write_info_tsv {manifest output} {
    set code {
import csv, json, sys
fields=("cdc_id","severity","description","depth","exception","source_clock","destination_clock","source","destination")
data=json.load(open(sys.argv[1], encoding="utf-8"))
rows=data["info_rows"]
assert rows and all(set(row)==set(fields) and row["severity"]=="Info" for row in rows)
with open(sys.argv[2], "w", encoding="utf-8", newline="") as f:
    writer=csv.DictWriter(f, fieldnames=fields, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        assert not any("\t" in str(v) or "\n" in str(v) or "\r" in str(v) for v in row.values())
        writer.writerow(row)
print(len(rows))
}
    if {[catch {exec -- $::receiver_v2_typed_cdc_gate::python -I -c $code $manifest $output} result]} {fail "cannot freeze Info TSV: $result"}
    return [string trim $result]
  }
  proc validate_info_observations {manifest output expected_count} {
    set code {
import csv, json, sys
from collections import Counter
expected=json.load(open(sys.argv[1], encoding="utf-8"))["info_rows"]
with open(sys.argv[2], encoding="utf-8", newline="") as f:
    actual=list(csv.DictReader(f, delimiter="\t"))
count=int(sys.argv[3])
assert len(expected)==len(actual)==count
fields=("cdc_id","depth","exception","source","destination")
def expected_key(row): return tuple(str(row[k]) for k in fields)
def actual_key(row): return tuple(row["depth_report" if k=="depth" else k] for k in fields)
assert Counter(map(expected_key,expected))==Counter(map(actual_key,actual))
for row in actual:
    assert row["result"]=="PASS"
    chain=row["stage_chain"].split(";")
    assert len(chain)>=2 and chain[0]==row["destination"].rsplit("/",1)[0]
print(f"info_rows={count}")
}
    if {[catch {exec -- $::receiver_v2_typed_cdc_gate::python -I -c $code $manifest $output $expected_count} result]} {fail "exact Info physical output mismatch: $result"}
    if {[string trim $result] ne "info_rows=$expected_count"} {fail "invalid Info physical completion"}
  }
  proc validate_critical {report helpers} {
    # The reset contracts prove the full fan-in/effect cone.  This separate
    # report binding rejects any added Critical row, a renamed representative,
    # or a non-CDC-10 substitution before that structural evidence is used.
    set code {
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from cdc_typed_inventory import parse_report
from cdc_critical_contract import validate_critical_rows
_, rows = parse_report(Path(sys.argv[1]).read_text(encoding="utf-8", errors="strict"))
print(len(validate_critical_rows(rows)))
}
    if {[catch {exec -- $::receiver_v2_typed_cdc_gate::python -I -c $code $report $helpers} result]} { fail "Critical CDC rows are not the reviewed reset-cone representatives: $result" }
    if {[string trim $result] ne "2"} { fail "invalid reviewed Critical CDC count: $result" }
  }
  proc observer {vivado script args completion_re log journal} {
    if {[file exists $log] || [file exists $journal]} { fail "observer log/journal must be fresh: $log $journal" }
    # ``exec --`` passes each already-normalized path as a literal argument;
    # notably it avoids cmd.exe code-page processing for a Chinese workspace.
    if {[catch {exec -- $vivado -mode batch -log $log -journal $journal -source $script -tclargs {*}$args} output]} { fail "observer $script failed: $output" }
    if {![file isfile $log] || ![file isfile $journal]} { fail "observer did not create its dedicated log/journal: $script" }
    set log_fh [open $log r]; set evidence "$output\n[read $log_fh]"; close $log_fh
    if {[regexp -nocase -line {^(ERROR|FATAL):} $evidence]} { fail "observer emitted ERROR/FATAL: $script" }
    if {![regexp -line -- $completion_re $evidence]} { fail "observer completion line missing for $script" }
  }
  proc validate_physical_observations {pairs physical stages expected_count} {
    # Exact-pair observer data is a routed measurement, not a count-only
    # receipt.  Bind every generated bit endpoint and report clock to its
    # frozen inventory entry; require a finite nonnegative min/max value and
    # a max value within the actual destination-clock period.  The separate
    # HDMI mailbox contract retains its tighter 1.500 ns literal XDC proof.
    set code {
import csv, math, sys
from collections import Counter
from pathlib import Path

pair_path, physical_path, stage_path, expected_count = map(Path, sys.argv[1:5])
expected_count = int(str(expected_count))
with pair_path.open(encoding="utf-8", newline="") as f:
    expected = list(csv.DictReader(f, delimiter="\t"))
with physical_path.open(encoding="utf-8", newline="") as f:
    measured = list(csv.DictReader(f, delimiter="\t"))
with stage_path.open(encoding="utf-8", newline="") as f:
    stages = list(csv.DictReader(f, delimiter="\t"))
if len(expected) != expected_count or len(measured) != expected_count:
    raise RuntimeError(f"exact-pair count mismatch expected={len(expected)} measured={len(measured)} declared={expected_count}")
required_pair = {"kind", "cdc_id", "source_clock", "destination_clock", "source", "destination", "bit"}
if any(not required_pair.issubset(row) for row in expected):
    raise RuntimeError("frozen pair inventory has incomplete row")
required_measurement = {"kind", "cdc_id", "source_clock_report", "destination_clock_report", "source", "destination", "bit", "min_ns", "max_ns", "min_user_ignored", "max_user_ignored", "endpoint_clock"}
if any(not required_measurement.issubset(row) for row in measured):
    raise RuntimeError("exact-pair physical TSV header/row is incomplete")
def key_expected(row):
    return tuple(row[k] for k in ("kind", "cdc_id", "source_clock", "destination_clock", "source", "destination", "bit"))
def key_measured(row):
    return tuple(row[k] for k in ("kind", "cdc_id", "source_clock_report", "destination_clock_report", "source", "destination", "bit"))
if Counter(map(key_expected, expected)) != Counter(map(key_measured, measured)):
    raise RuntimeError("physical exact-pair set differs from frozen typed inventory")
period_ns = {"clk_fpga_0": 20.0, "clk_out1_receiver_v2_rf_sampling_clock_0": 62.5, "mmcm_clk_0_s": 6.75}
for row in measured:
    clock = row["endpoint_clock"]
    if row["destination_clock_report"] != clock or clock not in period_ns:
        raise RuntimeError(f"unreviewed destination/endpoint clock: {row['destination_clock_report']} / {clock}")
    if row["min_user_ignored"] != "0" or row["max_user_ignored"] != "0":
        raise RuntimeError(f"crossing measurement is user-ignored rather than a routed timed path: {row['source']} -> {row['destination']}")
    try:
        lo, hi = float(row["min_ns"]), float(row["max_ns"])
    except ValueError as exc:
        raise RuntimeError(f"non-numeric pair delay: {row}") from exc
    if not all(math.isfinite(v) and v >= 0 for v in (lo, hi)) or hi > period_ns[clock] + 1e-6:
        raise RuntimeError(f"payload bit misses one-period routed bound: {row['source']} -> {row['destination']} max={hi} period={period_ns[clock]}")
required_stage = {"kind", "bit", "m1", "m2", "m1_async_reg_raw", "m2_async_reg_raw", "normal_datapath_ns", "normal_slack_ns", "endpoint_clock", "fanout"}
if len(stages) != 15 or any(not required_stage.issubset(row) for row in stages):
    raise RuntimeError(f"synchronizer-stage TSV is not exactly 15 complete rows: {len(stages)}")
counts = Counter(row["kind"] for row in stages)
if counts != Counter({"wfifo_enable": 4, "hdmi_gray": 9, "wfifo_request_toggle": 1, "wfifo_overflow_return": 1}):
    raise RuntimeError(f"unexpected 15-stage synchronizer set: {counts}")
wfifo = "receiver_v2_i/rf_rx_fifo/inst/"
hdmi = "receiver_v2_i/axi_hdmi_core/inst/i_vdma/"
expected_stages = {( "wfifo_enable", str(i), wfifo+f"dout_enable_m1_reg[{i}]", wfifo+f"dout_enable_reg[{i}]", "clk_fpga_0") for i in range(4)}
expected_stages |= {( "hdmi_gray", str(i), hdmi+f"vdma_raddr_g_m1_reg[{i}]", hdmi+f"vdma_raddr_g_m2_reg[{i}]", "clk_fpga_0") for i in range(9)}
expected_stages |= {
    ("wfifo_request_toggle", "0", wfifo+"dout_req_t_m1_reg", wfifo+"dout_req_t_m2_reg", "clk_fpga_0"),
    ("wfifo_overflow_return", "0", wfifo+"din_ovf_m1_reg", wfifo+"din_ovf_reg", "clk_out1_receiver_v2_rf_sampling_clock_0"),
}
if Counter(tuple(row[k] for k in ("kind", "bit", "m1", "m2", "endpoint_clock")) for row in stages) != Counter(expected_stages):
    raise RuntimeError("synchronizer exact endpoints/bits/clocks differ from reviewed 15 stages")
for row in stages:
    if row["fanout"] != "SOLE_FANOUT" or not row["endpoint_clock"]:
        raise RuntimeError(f"synchronizer topology/effective clock missing: {row}")
    if row["m1_async_reg_raw"].strip().lower() not in {"1", "true", "yes"} or row["m2_async_reg_raw"].strip().lower() not in {"1", "true", "yes"}:
        raise RuntimeError(f"synchronizer ASYNC_REG absent: {row}")
    try:
        delay, slack = float(row["normal_datapath_ns"]), float(row["normal_slack_ns"])
    except ValueError as exc:
        raise RuntimeError(f"non-numeric normal synchronizer timing: {row}") from exc
    if not all(math.isfinite(v) and v >= 0 for v in (delay, slack)):
        raise RuntimeError(f"negative/nonfinite normal synchronizer timing: {row}")
print(f"pairs={len(measured)} stages={len(stages)}")
}
    if {[catch {exec -- $::receiver_v2_typed_cdc_gate::python -I -c $code $pairs $physical $stages $expected_count} result]} { fail "exact payload or 15-stage routed evidence rejected: $result" }
    if {![regexp {^pairs=[0-9]+ stages=15$} [string trim $result]]} { fail "invalid physical-observation receipt: $result" }
  }
  proc validate {out frozen_integration reviewed_info python_bin vivado_bin} {
    variable python
    set python [file normalize $python_bin]
    set out [file normalize $out]; set frozen_integration [file normalize $frozen_integration]
    set reviewed_info [file normalize $reviewed_info]; set vivado_bin [file normalize $vivado_bin]
    if {[file exists $out] || ![file isfile $reviewed_info] || ![file isfile $python] || ![file isfile $vivado_bin]} { fail "output must be fresh and tools/Info manifest must exist" }
    foreach name {capture_reset_guard_contract.tcl release_assertion_contract.tcl release_assertion_effect.tcl hdmi_epoch_cdc_contract.tcl hdmi_gray_pointer_contract.tcl hdmi_mailbox_exact_contract.tcl receiver_v2_external_io_contract.tcl} {
      if {![file isfile [file join $frozen_integration $name]]} { fail "frozen integration dependency absent: $name" }
    }
    foreach name {cdc_typed_inventory.py cdc_info_manifest.py cdc_typed_exact_pairs.tcl cdc_typed_sync_stages.tcl audit_adi_bundled_delay.tcl cdc_info_exact_stages.tcl cdc_critical_contract.py} {
      if {![file isfile [file join $frozen_integration tests $name]]} { fail "frozen typed helper absent: $name" }
    }
    set frozen_hashes [dict create]
    foreach name {capture_reset_guard_contract.tcl release_assertion_contract.tcl release_assertion_effect.tcl hdmi_epoch_cdc_contract.tcl hdmi_gray_pointer_contract.tcl hdmi_mailbox_exact_contract.tcl receiver_v2_external_io_contract.tcl} {
      dict set frozen_hashes $name [sha256 [file join $frozen_integration $name]]
    }
    foreach name {cdc_typed_inventory.py cdc_info_manifest.py cdc_typed_exact_pairs.tcl cdc_typed_sync_stages.tcl audit_adi_bundled_delay.tcl cdc_info_exact_stages.tcl cdc_critical_contract.py} {
      dict set frozen_hashes tests/$name [sha256 [file join $frozen_integration tests $name]]
    }
    file mkdir $out
    set ledger [open [file join $out gate_ledger.tsv] w]; puts $ledger "check\tresult\tdetail"
    set route [report_route_status -return_string]
    set route_fh [open [file join $out raw_route_status.rpt] w]; puts -nonewline $route_fh $route; close $route_fh
    assert_route_complete $route
    report_cdc -details -no_waiver -file [file join $out raw_report_cdc_no_waiver.rpt]
    report_timing_summary -delay_type min_max -report_unconstrained -file [file join $out raw_timing_summary.rpt]
    set check_timing [check_timing -verbose -return_string]
    set fh [open [file join $out raw_check_timing.rpt] w]; puts -nonewline $fh $check_timing; close $fh
    load_contracts $frozen_integration
    if {[llength [info procs ::receiver_v2_external_io_contract::validate_open_topology]] != 1} { close $ledger; fail "frozen external-I/O contract lacks validate_open_topology" }
    must_pass capture_reset_guard {receiver_v2_capture_contract::validate post_route} $ledger
    must_pass release_topology {receiver_v2_release_assertion::validate post_route 1} $ledger
    must_pass release_effect {receiver_v2_release_effect::validate 1} $ledger
    must_pass hdmi_epoch_ready {receiver_v2_hdmi_epoch_cdc::validate_ready} $ledger
    must_pass hdmi_epoch_ack {receiver_v2_hdmi_epoch_cdc::validate_ack} $ledger
    must_pass hdmi_gray {receiver_v2_gray_contract::validate} $ledger
    must_pass hdmi_mailbox {receiver_v2_mailbox_contract::validate post_route} $ledger
    set external_fh [open [file join $out external_io_open_topology.tsv] w]
    must_pass external_open_topology [list ::receiver_v2_external_io_contract::validate_open_topology $external_fh $check_timing] $ledger
    close $external_fh
    set snapshot [file join $out current_routed_typed_audit.dcp]
    write_checkpoint -force $snapshot
    set snapshot_hash [sha256 $snapshot]
    set inventory [file join $out typed_inventory.json]
    if {[catch {exec -- $python -I [file join $frozen_integration tests cdc_typed_inventory.py] --report [file join $out raw_report_cdc_no_waiver.rpt] --out $inventory} result]} { close $ledger; fail "typed CDC inventory rejected raw report: $result" }
    set info_count [validate_info [file join $out raw_report_cdc_no_waiver.rpt] $reviewed_info [file join $frozen_integration tests]]
    validate_critical [file join $out raw_report_cdc_no_waiver.rpt] [file join $frozen_integration tests]
    set info_tsv [file join $out exact_info.tsv]
    if {[write_info_tsv $reviewed_info $info_tsv] != $info_count} {fail "frozen Info TSV count mismatch"}
    observer $vivado_bin [file join $frozen_integration tests cdc_info_exact_stages.tcl] \
      [list $snapshot $info_tsv [file join $out info_stages]] \
      "^CDC_INFO_EXACT_STAGES_COMPLETE rows=$info_count physical_observation_only=1$" \
      [file join $out info_stages.vivado.log] [file join $out info_stages.vivado.jou]
    validate_info_observations $reviewed_info [file join $out info_stages cdc_info_exact_stages.tsv] $info_count
    set pairs [file join $out exact_pairs.tsv]; set pair_count [write_pairs $inventory $pairs]
    observer $vivado_bin [file join $frozen_integration tests cdc_typed_exact_pairs.tcl] [list $snapshot $pairs [file join $out exact_pairs]] {^CDC_TYPED_EXACT_PAIR_OBSERVATION_COMPLETE pairs=[1-9][0-9]* \(not CDC signoff\)$} [file join $out exact_pairs_console.log] [file join $out exact_pairs.jou]
    observer $vivado_bin [file join $frozen_integration tests cdc_typed_sync_stages.tcl] [list $snapshot [file join $out sync_stages]] {^CDC_TYPED_SYNC_STAGES_COMPLETE pairs=15 \(not CDC signoff\)$} [file join $out sync_stages_console.log] [file join $out sync_stages.jou]
    observer $vivado_bin [file join $frozen_integration tests audit_adi_bundled_delay.tcl] [list $snapshot [file join $out adi_bundled_delay]] {^ADI_BUNDLED_PROPAGATION_OBSERVED paths=[1-9][0-9]* exceeded_one_period=0 \(no waivers; not full CDC acceptance\)$} [file join $out adi_bundled_delay_console.log] [file join $out adi_bundled_delay.jou]
    validate_physical_observations $pairs [file join $out exact_pairs exact_pairs.tsv] [file join $out sync_stages synchronizer_stages.tsv] $pair_count
    foreach name [dict keys $frozen_hashes] {
      set path [file join $frozen_integration {*}[split $name /]]
      if {[sha256 $path] ne [dict get $frozen_hashes $name]} { close $ledger; fail "frozen helper changed during typed audit: $name" }
    }
    set status [open [file join $out typed_cdc_gate_receipt.tcldict] w]
    puts $status "status PASS"
    puts $status "current_routed_snapshot_sha256 $snapshot_hash"
    puts $status "exact_payload_pairs $pair_count"
    puts $status "exact_info_physical_rows $info_count"
    puts $status "raw_cdc_sha256 [sha256 [file join $out raw_report_cdc_no_waiver.rpt]]"
    puts $status "reviewed_info_sha256 [sha256 $reviewed_info]"
    puts $status "frozen_helpers_sha256 $frozen_hashes"
    close $status; close $ledger
    puts "RXV2_TYPED_CDC_GATE_PASS snapshot_sha256=$snapshot_hash pairs=$pair_count"
  }
}
