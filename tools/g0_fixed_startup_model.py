#!/usr/bin/env python3
"""Opt-in variable-M integer startup CFO/quality; no RTL service claim.

Inputs are integer NCO/RRC outputs, sampled at integer endpoints after the
existing causal warmup crop. Public training alone supplies despreading signs.
Quality rotation uses phase zero at the first retained symbol. The final NCO
restarts against an explicit global phase origin, independently of coarse phase.
"""
import operator
import numpy as np
from g0_sounder import RATE
from g0_fixed_training_model import nearest_div, unwrap_phase32
from phase_angle_cost_probe import cordic_phase

Q32=1<<32
MASK32=Q32-1
MASK48=(1<<48)-1


def final_nco_configuration(coarse_step_u32,num_s48,denominator,commit_endpoint,phase_origin_index=0):
    coarse_step_u32=operator.index(coarse_step_u32);num_s48=operator.index(num_s48)
    denominator=operator.index(denominator);commit_endpoint=operator.index(commit_endpoint)
    phase_origin_index=operator.index(phase_origin_index)
    if not 0<=coarse_step_u32<Q32 or not -(1<<47)<=num_s48<1<<47 or not 0<denominator<1<<16:
        raise ValueError('startup NCO arithmetic interface range')
    if not 0<=commit_endpoint<=MASK48 or not 0<=phase_origin_index<=MASK48:
        raise ValueError('startup NCO endpoint/origin outside unsigned48')
    coarse=coarse_step_u32 if coarse_step_u32<1<<31 else coarse_step_u32-Q32
    den=4*denominator;num=coarse*den-num_s48;offset=(commit_endpoint-phase_origin_index)&MASK48
    return {'step_u32':nearest_div(num,den)&MASK32,'phase_u32':nearest_div(num*offset,den)&MASK32,
        'phase_origin_index48':phase_origin_index,'known_index48':commit_endpoint,'offset_u48':offset,
        'exact_ADC_step_numerator':num,'exact_ADC_step_denominator':den,
        'coarse_signed_step':coarse,'fine_symbol_numerator_s48':num_s48,'fine_symbol_denominator_u16':denominator,
        'phase_equation':'nearest_ties_up((coarse_signed_step - num/(4*D))*((commit-origin) mod2^48)) mod2^32',
        'coarse_phase_continuity_required':False}


def estimate_startup(lanes,signs,first_symbol,coarse_step_u32,commit_endpoint,phase_origin_index=0):
    """Integer arithmetic only after signed21 input/sign validation.

    phase_step is per SYMBOL. CFO gate uses exact num/D; quality uses the
    rounded symbol step and existing nearest4096/Q16/F4 rotation arithmetic.
    """
    lanes=np.asarray(lanes);signs=np.asarray(signs);m=len(lanes)
    if lanes.shape!=(m,4) or not 128<=m<=448 or signs.shape!=(m,2):
        raise ValueError('startup requires128..448 four-lane vectors and public signs')
    if not np.issubdtype(lanes.dtype,np.integer) or np.any(lanes<-(1<<20)) or np.any(lanes>=1<<20):
        raise ValueError('startup input must be signed21')
    if np.any((signs!=1)&(signs!=-1)) or not 384<=operator.index(first_symbol)<=896-m:
        raise ValueError('startup public TX0 training bounds/signs')
    lanes=lanes.astype(np.int64);signs=signs.astype(np.int64);blocks=m//16
    d=8*blocks*(blocks*blocks-1)//3
    energy=[sum(int(row[2*r])**2+int(row[2*r+1])**2 for row in lanes) for r in range(2)]
    selected=int(energy[1]>energy[0]);de=np.empty_like(lanes)
    for r in range(2):
        i,q=lanes[:,2*r],lanes[:,2*r+1]
        de[:,2*r]=i*signs[:,0]+q*signs[:,1];de[:,2*r+1]=q*signs[:,0]-i*signs[:,1]
    block=de[:blocks*16].reshape(blocks,16,4).sum(axis=1)
    assert np.all(block>=-(1<<26)) and np.all(block<1<<26)
    phase_rows=[];unwrapped_rows=[];nums=[];bad=[];zero_counts=[]
    for r in range(2):
        angles=[cordic_phase(int(row[2*r]),int(row[2*r+1])) for row in block]
        phases=[p for p,e in angles];unwrapped,deltas,ambiguous=unwrap_phase32(phases)
        num=sum((2*b-(blocks-1))*p for b,p in enumerate(unwrapped))
        assert -(1<<47)<=num<1<<47
        zeros=sum(e for p,e in angles)
        phase_rows.append(phases);unwrapped_rows.append(unwrapped);nums.append(num)
        bad.append(bool(zeros or ambiguous));zero_counts.append(zeros)
    info={'M':m,'B':blocks,'unused_tail_for_OLS':m-blocks*16,'first_public_symbol':int(first_symbol),
        'selected_rx':selected,'energy_u50':energy,'weighted_numerators_s48':nums,'D_u16':d,
        'branch_invalid':bad,'zero_blocks':zero_counts,'block_sums_s27':block.tolist(),
        'phases_u32':phase_rows,'unwrapped_s38':unwrapped_rows,'quality_rotation_origin':'first_retained_symbol',
        'quality_initial_phase_u32':0,'quality_uses_all_M':True,'energy_uses_all_M':True,
        'scope':'integer startup arithmetic software model, not RTL dispatch/service','g0_passed':False}
    num=nums[selected];step=nearest_div(num,d)
    assert -(1<<31)<=step<1<<31
    info.update({'num_s48':num,'step_s32':step,'fine_CFO_correction_hz':num*RATE/(4*d*Q32),
                 'CFO_gate_lhs':50*abs(num),'CFO_gate_rhs':d*Q32})
    if bad[selected]:
        info.update({'accepted':False,'error':'selected_phase_invalid','coherent_sums_s36':None,
            'quality_lhs':None,'quality_rhs':None,'quality_float_diagnostic':None,'final_nco_config':None})
        return info
    totals=[0,0];phase=0
    for row in de:
        bin_index=((phase+0x80000)&MASK32)>>20;angle=bin_index*(2*np.pi/4096)
        c,s=int(np.rint(65536*np.cos(angle))),int(np.rint(65536*np.sin(angle)))
        i,q=int(row[2*selected]),int(row[2*selected+1])
        totals[0]+=nearest_div(i*c-q*s,4096);totals[1]+=nearest_div(i*s+q*c,4096)
        phase=(phase-step)&MASK32
    assert all(-(1<<35)<=v<1<<35 for v in totals)
    coherent_power=sum(v*v for v in totals);lhs=5*coherent_power;rhs=1024*m*energy[selected]
    cfo_ok=50*abs(num)<=d*Q32;quality_ok=energy[selected]>0 and lhs>=rhs
    info.update({'coherent_sums_s36':totals,'quality_lhs':lhs,'quality_rhs':rhs,
        'quality_float_diagnostic':coherent_power/(512*m*energy[selected]) if energy[selected] else None,
        'CFO_gate_passed':cfo_ok,'quality_gate_passed':quality_ok,'accepted':cfo_ok and quality_ok,
        'error':None if cfo_ok and quality_ok else ('CFO_out_of_range' if not cfo_ok else 'incoherent'),
        'final_nco_config':final_nco_configuration(coarse_step_u32,num,d,commit_endpoint,phase_origin_index)})
    return info


class FixedStartupEstimator:
    def __init__(self,retain_vectors=False):
        self.calls=[];self.vectors=[];self.retain_vectors=retain_vectors

    def __call__(self,values,positions,training_indices,public,coarse_nco_config,commit_endpoint,phase_origin_index=0):
        values=np.asarray(values);positions=np.asarray(positions);indices=np.asarray(training_indices)
        m=len(values)
        if values.shape!=(m,2) or positions.shape!=(m,) or indices.shape!=(m,):raise ValueError('startup adapter shape')
        if not np.all(np.isfinite(positions)) or not np.all(positions==np.floor(positions)) or not np.all(np.diff(positions)==4):
            raise ValueError('startup requires exact integer endpoints at4 samples/symbol')
        if not np.issubdtype(indices.dtype,np.integer) or not np.all(np.diff(indices)==1):raise ValueError('startup public indices')
        lanes=np.empty((m,4));lanes[:,0::2]=4*values.real;lanes[:,1::2]=4*values.imag
        if not np.all(np.isfinite(lanes)) or not np.all(lanes==np.floor(lanes)) or np.any(lanes<-(1<<20)) or np.any(lanes>=1<<20):
            raise ValueError('startup requires exact signed21/scale4, no implicit rounding')
        sequence=np.asarray(public)[indices,0]
        if not np.allclose(abs(sequence.real),1/np.sqrt(2),atol=1e-14,rtol=0) or not np.allclose(abs(sequence.imag),1/np.sqrt(2),atol=1e-14,rtol=0):
            raise ValueError('startup public QPSK')
        signs=np.column_stack((np.where(sequence.real>0,1,-1),np.where(sequence.imag>0,1,-1)))
        lanes=lanes.astype(np.int64)
        info=estimate_startup(lanes,signs,int(indices[0]),coarse_nco_config['step_u32'],commit_endpoint,phase_origin_index)
        info.update({'first_endpoint':int(positions[0]),'last_endpoint':int(positions[-1]),
            'first_RTL_center':int(positions[0])-20,'last_RTL_center':int(positions[-1])-20,
            'commit_endpoint':int(commit_endpoint),'coarse_nco_config':dict(coarse_nco_config),
            'input_max_absolute':int(abs(lanes).max()),'adapter_exact_scale4':True})
        self.calls.append(info)
        if self.retain_vectors:self.vectors.append({'lanes':lanes,'signs':signs,'endpoints':positions.astype(np.uint64),
            'public_indices':indices.astype(np.int64),'report':info})
        return info
