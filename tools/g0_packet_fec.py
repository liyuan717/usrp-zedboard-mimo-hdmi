#!/usr/bin/env python3
"""Bounded GF256 RS20/32 S512 reference for finite G0 packet experiments.

Offline host model only; this is not a PS implementation or PL throughput
claim. The generator equals V*inverse(V[:20]) with V[r,c]=(r+1)^c.
"""
import numpy as np
from fec_oracle import multiply, inverse

K=20
N=32
SIZE=512
TABLE=np.array([[multiply(a,b) for b in range(256)] for a in range(256)],dtype=np.uint8)


def generator():
    matrix=np.empty((N,K),dtype=np.uint8)
    for row in range(N):
        for col in range(K):
            numerator=denominator=1
            for j in range(K):
                if j!=col:
                    numerator=multiply(numerator,(row+1)^(j+1))
                    denominator=multiply(denominator,(col+1)^(j+1))
            matrix[row,col]=multiply(numerator,inverse(denominator))
    if not np.array_equal(matrix[:K],np.eye(K,dtype=np.uint8)):
        raise ValueError('non-systematic matrix')
    return matrix


GENERATOR=generator()


def combine(matrix,data):
    data=np.asarray(data,dtype=np.uint8)
    return np.bitwise_xor.reduce(TABLE[matrix[:,:,None],data[None,:,:]],axis=1)


def source_pattern(block):
    # Each block/source/byte changes. Only the TX and the post-decode oracle
    # use this pattern; the receiver never calls it.
    r=np.arange(K,dtype=np.uint64)[:,None]
    b=np.arange(SIZE,dtype=np.uint64)[None,:]
    return ((block*71+r*29+b*17+b//11+(block+1)*(b%13))&255).astype(np.uint8)


def encode(source):
    if source.shape!=(K,SIZE):raise ValueError('fixed diagnostic RS20/32 S512 required')
    return combine(GENERATOR,source)


def recover(received):
    if len(received)<K:return None
    ids=sorted(received)
    if any(not 0<=i<N or len(received[i])!=SIZE for i in ids):
        raise ValueError('invalid coding symbol')
    chosen=ids[:K]
    a=GENERATOR[chosen].copy();inv=np.eye(K,dtype=np.uint8)
    for col in range(K):
        pivot=next((r for r in range(col,K) if a[r,col]),None)
        if pivot is None:raise ValueError('singular generator submatrix')
        a[[col,pivot]]=a[[pivot,col]];inv[[col,pivot]]=inv[[pivot,col]]
        scale=inverse(int(a[col,col]));a[col]=TABLE[scale,a[col]];inv[col]=TABLE[scale,inv[col]]
        for row in range(K):
            if row!=col:
                scale=a[row,col]
                a[row]^=TABLE[scale,a[col]];inv[row]^=TABLE[scale,inv[col]]
    data=np.array([np.frombuffer(received[i],dtype=np.uint8) for i in chosen])
    source=combine(inv,data)
    expected=combine(GENERATOR[ids],source)
    if any(expected[j].tobytes()!=received[i] for j,i in enumerate(ids)):
        raise ValueError('received equations inconsistent')
    return source
