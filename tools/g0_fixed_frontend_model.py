#!/usr/bin/env python3
"""Opt-in G0 integer NCO/RRC islands inside the existing floating receiver.

Calibration/notch, optional post-NCO DC blocker, CFO estimation, interpolation,
channel/detection/pilots remain FLOAT. This is not a complete fixed-point PHY.
Models produce endpoint-indexed arrays for the old reference; RTL RRC tags
window centers n-20, explicitly accounted for rather than changing timing.
"""
import numpy as np
import operator
from scipy import signal
from g0_sounder import RATE,rrc


def checked_round(values,bits):
    rounded=np.rint(values).astype(np.int64)
    if np.any(rounded<-(1<<(bits-1))) or np.any(rounded>=(1<<(bits-1))):
        raise ValueError(f'candidate {bits}-bit interface overflow; no silent clipping')
    return rounded


def fixed_frontend(source,cfo,start,dc_block=False,nco_config=None):
    source=np.asarray(source,dtype=complex)
    if source.ndim!=2 or source.shape[1]!=2 or not 0<=start<len(source):raise ValueError('frontend shape/boundary')
    x=np.empty((len(source)-start,4),dtype=np.int64)
    x[:,0::2]=checked_round(source[start:].real,16);x[:,1::2]=checked_round(source[start:].imag,16)
    if nco_config is None:
        step=int(round(-cfo/RATE*(1<<32)))&0xffffffff
        phase0=int(round(-cfo*start/RATE*(1<<32)))&0xffffffff
    else:
        step=operator.index(nco_config['step_u32']);phase0=operator.index(nco_config['phase_u32'])
        if not 0<=step<1<<32 or not 0<=phase0<1<<32:
            raise ValueError('explicit NCO configuration outside unsigned32')
    phase=(np.arange(len(x),dtype=np.uint64)*np.uint64(step)+np.uint64(phase0))&np.uint64(0xffffffff)
    bins=((phase+np.uint64(1<<19))>>np.uint64(20))&np.uint64(4095)
    angle=bins.astype(float)*(2*np.pi/4096)
    c=np.rint(65536*np.cos(angle)).astype(np.int64);s=np.rint(65536*np.sin(angle)).astype(np.int64)
    mixed=np.empty_like(x)
    for rx in range(2):
        i=x[:,2*rx];q=x[:,2*rx+1]
        mixed[:,2*rx]=(i*c-q*s+16384)//32768
        mixed[:,2*rx+1]=(i*s+q*c+16384)//32768
    if np.any(abs(mixed)>92682):raise ValueError('NCO declared range exceeded')
    if dc_block:
        # This stage is explicitly NOT fixed point. Re-quantize the resulting
        # values at the declared signed18 RRC interface, retaining scale2.
        mixed=checked_round(signal.lfilter([1,-1],[1,-.995],mixed,axis=0),18)
    taps=np.rint(rrc()*131072).astype(np.int64)
    filtered=np.empty_like(mixed)
    for lane in range(4):
        accum=np.convolve(mixed[:,lane],taps,mode='full')[:len(mixed)]
        filtered[:,lane]=(accum+65536)//131072
    if np.any(filtered<-(1<<19)) or np.any(filtered>=(1<<19)):raise ValueError('RRC output overflow')
    result=np.zeros_like(source)
    result[start:]=(filtered[:,0::2]+1j*filtered[:,1::2])/2
    result[:start+40]=0 # RTL emits no partial-history output.
    signed_step=step if step<(1<<31) else step-(1<<32)
    info={'scope':'integer NCO/RRC models surrounded by float acquisition/notch/DC/interpolation/PHY',
        'commit_endpoint_sample':int(start),'first_valid_endpoint_sample':int(start+40),
        'RRC_first_output_center_relative_sample':int(start+20),
        'reference_array_coordinate':'original ADC endpoint; RTL center=endpoint-20',
        'float_to_integer_adapter_rounding':'nearest ties-even (software adapter, not yet RTL)',
        'NCO_phase_u32':phase0,'NCO_step_u32':step,'requested_CFO_hz':float(cfo),
        'quantized_correction_hz':signed_step*RATE/(1<<32),
        'frequency_quantization_error_hz':signed_step*RATE/(1<<32)+float(cfo),
        'input_max_absolute':int(abs(x).max()),'RRC_input_max_absolute':int(abs(mixed).max()),
        'post_NCO_DC_blocker_still_float':bool(dc_block),'fixed_point_full_PHY':False,'g0_passed':False}
    if nco_config is not None:
        info['explicit_NCO_configuration']=dict(nco_config)
        info['NCO_configuration_reconstructed_from_float_Hz']=False
    return result,info
