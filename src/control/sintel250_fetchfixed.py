"""Add the finite-video readiness record to this private fetch namespace only."""
from sintel250 import make_video25 as original_make,wf

def make_video25(out):
    cls,build=original_make(out)
    namespace=cls.fetch.__globals__
    assert 'RF_READY.json' not in namespace['EVIDENCE_NAMES'] or 'RF_READY.json' in wf.EVIDENCE_NAMES
    namespace['EVIDENCE_NAMES']=namespace['EVIDENCE_NAMES']|{'RF_READY.json'}
    return cls,build
