#!/usr/bin/env python3
"""Finite60s dual-TX G0 measurement, RX/CRC/FEC stays in PL. Not video signoff."""
import argparse,hashlib,json,re,socket,subprocess,threading,time,zlib,uuid
from pathlib import Path
from board_command import command
from hardware_lease import HardwareLease
from run_g0_source_air import rx_setup_for_bandwidth,SERIAL,HOST,BOARD
from g0_rf_setup import read_rf_setup
from review_g0_source_long import review
from workspace_storage_guard import check,GIB

def qualification(a):
    platform=a.platform.resolve();assets=a.ram_assets.resolve();boot=a.boot_log.resolve()
    route=json.loads((platform/'route-review.json').read_text())
    assert route['finite_RX_only_G0_candidate_review_passed']
    assert route['route_complete_no_errors'] and route['typed_RF_HDMI_counter65_contracts_passed']
    assert min(route[k] for k in ['WNS_ns','WHS_ns','WPWS_ns'])>=0
    bit=(platform/'project/g0_source.bit').read_bytes()
    assert bit==(assets/'hdmi_pipeline.bit').read_bytes()
    crc=f'{zlib.crc32(bit):08x}'
    manifest=json.loads((assets/'BOOT_MANIFEST.json').read_text())
    row=next(r for r in manifest['files'] if r['file']=='hdmi_pipeline.bit')
    assert row['crc32']==crc and row['bytes']==len(bit)
    identity=json.loads((boot/'receipt.json').read_text())
    if identity.get('PL_configured_by_SD_FSBL'):
        bundle=getattr(a,'receiver_sd_bundle',None)
        if bundle is None:
            raise ValueError('SD-configured PL requires the qualified receiver SD bundle')
        expected=json.loads((bundle/'RECEIVER_BOOT.json').read_text())
        image=(bundle/'BOOT.BIN').read_bytes()
        assert expected['files']['BOOT.BIN']=={
            'bytes':len(image),'sha256':hashlib.sha256(image).hexdigest()}
        image_crc=f'{zlib.crc32(image):08x}'
        assert expected['PL_bit_crc32']==crc and identity['SD_boot_bin_crc32']==image_crc
        log=(boot/'03-SD-BOOT.BIN.log').read_text()
        assert re.search(r'==>\s*'+image_crc+r'\b',log) and re.search(r'(?m)^SD_FILE_OK$',log)
        assert identity['SOURCE_PL_idle_verified'] and identity['root_on_readonly_RAM']
    else:
        log='\n'.join(p.read_text(errors='replace') for p in boot.glob('03-*-hdmi_pipeline.bit.log'))
        assert re.search(r'==>\s*'+crc+r'\b',log)
        assert 'FPGA_RC=0' in (boot/'04-fpga.log').read_text()
    assert not identity['SD_written'] and not identity['RF_started']
    native=json.loads((a.native_control/'receipt.json').read_text())
    assert native['boot_id']==identity['boot_id'] and native['native_padding_DMA_control_passed']
    assert native['cleanup_LOs_off']
    assert a.tx_binary.is_file()
    return identity['boot_id'],crc

def numeric_line(text,prefix):
    rows=[s.strip() for s in text.splitlines() if s.startswith(prefix+' ')]
    assert len(rows)==1,(prefix,rows)
    return {k:int(v,16 if k=='crc' else 10) for k,v in re.findall(r'(\w+)=([0-9a-f]+)',rows[0])}

def verify_tx_mapping(log,mapping,required):
    observed=re.findall(r'(?m)^G0_TX_IQ_MAPPING rf0=([01]) rf1=([01])$',log)
    if (required or observed) and observed!=[tuple(map(str,mapping))]:
        raise ValueError('TX probe did not report the requested logical IQ mapping')

def verify_tx_gains(log,gains):
    rows=re.findall(r'(?m)^TX_GAIN channel=([01]) requested_db=(\S+) actual_db=(\S+) antenna=TX/RX ',log)
    if [(channel,float(requested),float(actual)) for channel,requested,actual in rows]!=[
            (str(channel),float(gain),float(gain)) for channel,gain in enumerate(gains)]:
        raise ValueError('TX gain or antenna readback differs from requested per-port setting')


def transfer_command(remote,port):
    # Stream the tmpfs evidence through the socket. Do not allocate another
    # file-sized Python bytes object on the Cortex-A9.
    return f'''python3 -c "import socket; s=socket.create_connection(('{HOST}',{port}),5); s.settimeout(20); f=open('{remote}.v3bc','rb'); s.sendfile(f); f.close(); s.shutdown(socket.SHUT_WR); s.close()"'''

def validate_native_review(summary, reviewed, video):
    if video:
        # The native compact probe saves complete batches, but its callback
        # counts only its fixed synthetic SOURCE pattern. Its record CRC
        # therefore covers non-SOURCE records in this real-video capture.
        assert summary['source']==summary['exact_bytes']==summary['restored_bytes']==0
        assert summary['bad_source']==reviewed['counts'].get('kind_1',0)
        assert summary['records']+summary['bad_source']==reviewed['records']
        assert summary['crc']==int(reviewed['non_SOURCE_record_crc32'],16)
    else:
        assert int(reviewed['record_crc32'],16)==summary['crc']
        assert reviewed['records']==summary['records']


def main(a):
    # Offline evidence checks precede HardwareLease or any device/network access.
    boot_id,bit_crc=qualification(a)
    erase=getattr(a,'erase_sources',0)
    swap=getattr(a,'swap_iq_channels',False)
    if swap and b'--swap-iq-channels\0' not in a.tx_binary.read_bytes():
        raise ValueError('TX probe does not support swapping logical IQ channels')
    mapping=[1,0] if swap else [0,1]
    rf0_gain=getattr(a,'rf0_gain_db',None)
    if rf0_gain is not None and (not 0<=rf0_gain<=a.gain_db
            or b'--rf0-gain-db\0' not in a.tx_binary.read_bytes()):
        raise ValueError('RF0 gain requires a supporting endpoint and must not exceed the shared gain')
    gains=[a.gain_db if rf0_gain is None else rf0_gain,a.gain_db]
    bandwidth=getattr(a,'rx_bandwidth_hz',4_000_000)
    rx_setup=rx_setup_for_bandwidth(bandwidth)
    setup_raw,rf_setup=read_rf_setup(getattr(a,'rf_setup',None),a.tx_binary,rx_setup,mapping)
    if not 0<=erase<=12:raise ValueError('finite 0..12 intentional SOURCE CRC erasures per block')
    archive=getattr(a,'archived_camera_run',None)
    video_source=None
    lane_proof=getattr(a,'archived_lane_control_proof',None)
    if lane_proof and not archive:
        raise ValueError('Coded-slot control requires the qualified archived camera run')
    detach_receiver_ethernet=bool(archive or getattr(a,'receiver_ethernet_down_during_rf',False))
    if detach_receiver_ethernet:
        from run_g0_video_air import host_management_route,restore_host_management
        if not host_management_route('enxec1ac33f53f0'):
            raise ValueError('The receiver management link must be ready before isolated RF')
    if archive:
        from replay_g0_camera_iq import load_replay,load_coded_lane_proof
        if erase or swap or a.session is not None:
            raise ValueError('Original video replay preserves session, contents and IQ mapping')
        plan,session,_,expected_iq=load_replay(archive)
        video_source=(archive/'wave/camera/source-symbols.bin').read_bytes()
        if lane_proof:
            expected_iq=load_coded_lane_proof(lane_proof,session,expected_iq)
    else:
        session=a.session if a.session is not None else uuid.uuid4().int&0xffffffff
        plan={'periods':3000,'TX_samples':240000000,'TX_duration_s':60}
    assert 0<=session<=0xffffffff
    v=Path(__file__).resolve().parents[1];out=a.output.resolve()
    assert re.fullmatch(r'[A-Za-z0-9_-]{1,64}',out.name)
    out.mkdir(parents=True,exist_ok=False);remote='/tmp/v3-source/'+out.name
    before=check('before-long-finite-RF');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
    assert before['disk']['available_bytes']>=5*GIB
    result={'scope':__doc__,'completed':False,'RF_started':False,'SD_written':False,
            'FMCOMMS3_TX_enabled':False,'feedback_enabled':False,'TDD_enabled':False,
            'frequency_hz':5500000000,'TX_gain_db':a.gain_db,'TX_gain_by_RF_db':gains,'TX_LO_offset_hz':1000000,
            'RX_gain_db':60,'RX_bandwidth_hz':bandwidth,'sample_rate_hz':4000000,
            'TX_seconds':plan['TX_duration_s'],'TX_samples':plan['TX_samples'],'RX_batches':65536,
            'ports':'X310 RF0/TX-RX vertical,RF1/TX-RX horizontal; FMCOMMS3 RX1A vertical,RX2A horizontal',
            'boot_id':boot_id,'bit_crc32':bit_crc,'fixed_FEC_banks':8,'session':session,
            'logical_iq_streams_by_X310_RF_port':mapping,
            'intentional_SOURCE_CRC_erasures_per_block':erase,
            'cleanup_LOs_off':False,'full_G0_or_video_passed':False,'events':[]}
    if lane_proof:
        result['coded_lanes_swapped']=True
        result['coded_lane_control_proof_sha256']=hashlib.sha256(lane_proof.read_bytes()).hexdigest()
    if rf_setup:
        (out/'RF-setup.json').write_bytes(setup_raw)
        result['RF_setup']=rf_setup
    producer=tx=None;producer_log=None;threads=[];uart_ready=True;ethernet_down=False;preflight_passed=False
    try:
        with HardwareLease() as lease:
            def run(name,text,timeout=30):
                nonlocal uart_ready
                subprocess.run(['sh','-n'],input=text,text=True,check=True)
                (out/(name+'.sh')).write_text(text+'\n')
                result['events'].append({'name':name,'begin':time.monotonic()})
                try:return command(SERIAL,text,out/(name+'.log'),timeout,lease=lease).decode(errors='replace')
                except TimeoutError:
                    uart_ready=False;result['UART_timeout_requires_inspection']=True;raise
            def restore_ethernet():
                nonlocal ethernet_down
                run('RX-eth0-restore','ip link set dev eth0 up\nip -o -4 addr show dev eth0 | grep -F " 169.254.209.84/16 "')
                ethernet_down=False
                restore_host_management('enxec1ac33f53f0','codex-zedboard-integration')
                result['receiver_ethernet_restored']=True
            try:
                run('preflight',f'''test "$(cat /proc/sys/kernel/random/boot_id)" = {boot_id}
grep ' / ext4 ro,' /proc/mounts
test -c /dev/v3-g0-source-ring
test -x /tmp/v3-source/probe
test "$(devmem 0x7c430008)" = 0x00000000
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
test ! -e {remote}.rc
python3 -c "import os; s=os.statvfs('/tmp'); assert s.f_bavail*s.f_frsize>=67108864"''')
                preflight_passed=True
                run('RX-setup',rx_setup)
                wave=out/'wave'
                producer_log=(out/'producer.log').open('w')
                if archive:
                    control_flags=['--swap-coded-lanes'] if lane_proof else []
                    producer=subprocess.Popen(['/usr/bin/python3',str(v/'tools/replay_g0_camera_iq.py'),
                        '--archive',str(archive.resolve()),'--output',str(wave),'--paced','--wait-go',*control_flags],
                        stdin=subprocess.PIPE,text=True,stdout=producer_log,stderr=subprocess.STDOUT)
                else:
                    producer=subprocess.Popen(['/usr/bin/python3',str(v/'tools/g0_fec_session_stream.py'),
                        '--output',str(wave),'--frames','3000','--session',str(session),'--erase-sources',str(erase)],stdout=producer_log,stderr=subprocess.STDOUT)
                deadline=time.monotonic()+20
                while not (wave/'iq.fifo').exists():
                    if producer.poll() is not None:raise RuntimeError('producer stopped before FIFO')
                    if time.monotonic()>deadline:raise TimeoutError('FIFO initialization')
                    time.sleep(.05)
                args=[str(a.tx_binary.resolve()),'--iq',str(wave/'iq.fifo'),'--freq-hz','5500000000',
                      '--gain-db',str(a.gain_db),'--tx-lo-offset-hz','1000000',
                      '--max-samples',str(plan['TX_samples']),'--enable-tx']
                if swap:args.append('--swap-iq-channels')
                if rf0_gain is not None:args.extend(['--rf0-gain-db',str(rf0_gain)])
                (out/'TX-command.json').write_text(json.dumps(args,indent=2)+'\n')
                tx=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
                ready=threading.Event()
                def read_tx():
                    with (out/'TX.log').open('w') as f:
                        for line in tx.stdout:
                            f.write(line);f.flush()
                            if line.strip()=='G0_READY WAIT_GO':ready.set()
                t=threading.Thread(target=read_tx,daemon=True);t.start();threads.append(t)
                assert ready.wait(30) and tx.poll() is None,'TX READY absent; no GO'
                tx_log=(out/'TX.log').read_text()
                verify_tx_mapping(tx_log,mapping,swap or bool(rf_setup))
                if rf0_gain is not None:verify_tx_gains(tx_log,gains)
                run('RX-arm',f'''(
/tmp/v3-source/probe 65536 110000 {remote}.v3bc --compact > {remote}.log 2>&1 &
v3_probe=$!
echo "$v3_probe" > {remote}.pid
if wait "$v3_probe"; then echo 0 > {remote}.rc; else echo $? > {remote}.rc; fi
) &
sleep 0.05
test "$(devmem 0x7c430008)" = 0x00000001
echo RX_UPTIME
cat /proc/uptime
echo PRE_GO_BATCHES
devmem 0x7c430010''')
                if detach_receiver_ethernet:
                    ethernet_down=True
                    run('RX-eth0-down','ip link set dev eth0 down\n'
                        'v3_link=$(ip -o link show dev eth0)\n'
                        'if echo "$v3_link" | grep -Eq "<([^>]*,)?UP[,>]"; then exit 1; fi')
                    result['receiver_ethernet_down_during_RF']=True
                if archive:
                    producer.stdin.write('GO\n');producer.stdin.flush()
                result['events'].append({'name':'GO','host_monotonic':time.monotonic()})
                result['RF_started']=True
                tx.stdin.write('GO\n');tx.stdin.flush()
                assert tx.wait(timeout=100)==0,'Inspect TX failure; no capacity pass'
                t.join(5);assert not t.is_alive()
                assert producer.wait(timeout=10)==0
                producer_log.close()
                wave_result=json.loads((wave/'receipt.json').read_text())
                assert wave_result['completed'] and wave_result['bytes_written']==plan['TX_samples']*16
                if archive:
                    assert all(wave_result[key]==expected_iq[key] for key in ('IQ_sha256','IQ_crc32'))
                    result['archived_coded_lane_control_IQ_byteexact' if lane_proof else
                           'archived_camera_IQ_byteexact']=True
                else:
                    assert wave_result['intentional_SOURCE_CRC_erasures_per_block']==erase
                result['waveform']=wave_result
                txlog=(out/'TX.log').read_text()
                verify_tx_mapping(txlog,mapping,swap or bool(rf_setup))
                assert 'TX_BURST_ACK channel_mask=3' in txlog
                assert f"TX completed: {plan['TX_samples']} dual-stream samples" in txlog
                result['EOB_acknowledged_channels']=[0,1]
                raw=run('RX-result',f'''for i in $(seq 1 500); do test ! -e {remote}.rc || break; sleep 0.1; done
test "$(cat {remote}.rc)" = 0
cat {remote}.log
busybox crc32 {remote}.v3bc
test "$(devmem 0x7c430008)" = 0x00000000
echo RX_END_UPTIME
cat /proc/uptime''',timeout=55)
                summary=numeric_line(raw,'SOURCE_DMA');compact=numeric_line(raw,'SOURCE_COMPACT')
                assert summary['batches']==summary['hw_batches']==65536
                for k in ['exit','running','queued','user','poisoned','pinned']:assert summary[k]==0,(k,summary)
                assert compact['omitted_batches']==0
                size=compact['dump_bytes'];assert 16<=size<=64*1024*1024
                if ethernet_down:
                    run('RX-eth0-at-stop','v3_link=$(ip -o link show dev eth0)\n'
                        'if echo "$v3_link" | grep -Eq "<([^>]*,)?UP[,>]"; then exit 1; fi')
                    restore_ethernet()
                transfer={}
                with socket.socket() as server:
                    server.bind((HOST,0));server.listen(1);server.settimeout(20);port=server.getsockname()[1]
                    def receive():
                        try:
                            conn,peer=server.accept();assert peer[0]==BOARD
                            count=crc=0
                            with conn,(out/'air.v3bc').open('wb') as f:
                                conn.settimeout(15)
                                while True:
                                    data=conn.recv(min(65536,size+1-count))
                                    if not data:break
                                    count+=len(data);assert count<=size
                                    f.write(data);crc=zlib.crc32(data,crc)
                            assert count==size;transfer.update(bytes=count,crc32=f'{crc:08x}')
                        except BaseException as error:transfer['error']=repr(error)
                    t=threading.Thread(target=receive,daemon=True);t.start()
                    run('transfer',transfer_command(remote,port),timeout=30)
                    t.join(20);assert not t.is_alive() and 'error' not in transfer,transfer
                assert re.search(r'(?m)^'+transfer['crc32']+' '+re.escape(remote)+r'\.v3bc\r?$',raw)
                run('release-copied-capture',f'''test "$(busybox crc32 {remote}.v3bc | cut -d ' ' -f1)" = {transfer['crc32']} &&
rm -- {remote}.v3bc &&
test ! -e {remote}.v3bc''')
                result['remote_capture_copy_released']=True
                reviewed=review(out/'air.v3bc',plan['periods'],session,erase,video_source,bool(lane_proof))
                (out/'capture-review.json').write_text(json.dumps(reviewed,indent=2)+'\n')
                result.update(native_summary=summary,compact_summary=compact,transfer=transfer)
                validate_native_review(summary,reviewed,bool(archive))
                clean=all(reviewed['counts'].get(k,0)==0 for k in [
                    'bad_batches','damaged_batches','missing_batches','duplicate_batches','late_batches',
                    'bad_SOURCE_identity','bad_SOURCE_payload','bad_FEC_terminal_record',
                    'direct_SOURCE_from_intentionally_bad_CRC'])
                clean=clean and all(summary.get(k,0)==0 for k in ['bad_metadata','bad_record'])
                if not archive:clean=clean and summary.get('bad_source',0)==0
                byte_join=clean and reviewed['SOURCE_unique']>0 and all(reviewed['direct_unique_SOURCE_by_TX'].values())
                rate=reviewed['fixed_steady_window']['unique_Bps']
                minimum=reviewed['fixed_steady_window']['minimum_five_second_unique_Bps']
                rolling=reviewed['rolling_service_in_fixed_steady_window']
                result.update(completed=True,native_summary=summary,compact_summary=compact,
                              transfer=transfer,capture_review=reviewed,
                              actual_SOURCE_byte_join_passed=byte_join,
                              measured_steady_SOURCE_rate_meets_current_video_margin=bool(not archive and byte_join and rate is not None and rate>=149504/.7),
                              each_fixed_five_second_SOURCE_rate_meets_current_video_margin=bool(not archive and byte_join and minimum is not None and minimum>=149504/.7),
                              each_rolling_five_second_SOURCE_rate_meets_current_video_margin=bool(
                                  not archive and byte_join and rolling and rolling['5']['minimum_all_offsets_unique_Bps']>=149504/.7))
            finally:
                if tx is not None and tx.poll() is None:
                    # EOF before GO prevents emission; after GO allow the finite
                    # waveform to finish normally and preserve EOB before fallback.
                    tx.stdin.close()
                    try:tx.wait(timeout=100 if result['RF_started'] else 5)
                    except subprocess.TimeoutExpired:
                        tx.terminate();tx.wait(timeout=5);result['forced_TX_exit_EOB_unconfirmed']=True
                if producer is not None and producer.poll() is None:
                    producer.terminate();producer.wait(timeout=5)
                for thread in threads:thread.join(5)
                if producer_log is not None:producer_log.close()
                if uart_ready and preflight_passed:
                    run('RX-idle',f'''if test -e {remote}.pid && test ! -e {remote}.rc; then kill -TERM "$(cat {remote}.pid)" 2>/dev/null || true; sleep 1; fi
echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown
echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(devmem 0x7c430008)" = 0x00000000
devmem 0x7c43000c''')
                    result['cleanup_LOs_off']=True
                    if ethernet_down:restore_ethernet()
    except BaseException as error:
        result['error']=repr(error);raise
    finally:
        (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
        (out/'disk-after.json').write_text(json.dumps(check('after-long-finite-RF'),indent=2)+'\n')
    print(json.dumps(result),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['platform','ram-assets','boot-log','native-control','tx-binary','output']:
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--archived-camera-run',type=Path,help='capture SOURCE identities from the verified R22 camera IQ')
    p.add_argument('--archived-lane-control-proof',type=Path,help='offline proof for coded payload lane swap only')
    p.add_argument('--gain-db',type=int,choices=(12,18),default=12)
    p.add_argument('--rx-bandwidth-hz',type=int,choices=(2000000,4000000),default=4000000)
    p.add_argument('--receiver-ethernet-down-during-rf',action='store_true',
                   help='isolate the receiver Ethernet during the synthetic SOURCE capacity measurement')
    p.add_argument('--rf0-gain-db',type=int,help='diagnostic RF0 attenuation only, RF1 retains the shared gain')
    p.add_argument('--receiver-sd-bundle',type=Path,help='qualified SD bundle when the boot reuses the FSBL PL')
    p.add_argument('--rf-setup',type=Path,help='measured antenna geometry JSON shared with the video measurement')
    p.add_argument('--erase-sources',type=int,choices=range(13),default=0,help='intentional CRC failures on the first SOURCE symbols of each RS block')
    p.add_argument('--swap-iq-channels',action='store_true',help='diagnostic only: send the two logical IQ streams on opposite X310 RF ports')
    p.add_argument('--session',type=lambda s:int(s,0),help='u32; default new random session per finite run')
    main(p.parse_args())
