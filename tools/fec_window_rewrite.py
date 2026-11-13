"""Exact loading-bank seal integration; all FEC arithmetic and ownership remain unchanged."""


def transform(source):
    changes = [
        ('// on overflow with an explicit counter instead of blocking the FEC engine.\n',
         '// on overflow with an explicit counter instead of blocking the FEC engine.\n'
         '// A CRC-good replacement range closes old out-of-range block admission.\n'
         '// Seal loading jobs, emit their complete sources and missing-row status, then\n'
         '// release normally. Queued/working owners finish; never overwrite a live bank.\n'),
        (' reg [31:0] bank_block[0:7];\n', ' reg [31:0] bank_block[0:7];\n reg [2:0] retire_bank;\n'),
        (' wire release_ready;\n', ' wire release_ready;\n wire seal_ready;\n'
         ' wire [31:0] retire_offset=bank_block[retire_bank]-range_first;\n'
         " wire retire_candidate=session_active && range_seen && bank_live[retire_bank] &&\n"
         "                       retire_offset>={28'd0,range_count};\n"
         ' // An ineligible queued/working owner cannot repeatedly block packet headers.\n'
         ' wire seal_valid=!rst && retire_candidate && seal_ready;\n'),
        ("  .seal_valid(1'b0),.seal_ready(),.seal_bank(3'd0),.seal_job(32'd0),",
         '  .seal_valid(seal_valid),.seal_ready(seal_ready),.seal_bank(retire_bank),.seal_job(bank_block[retire_bank]),'),
        ('range_count<=0;declare_index<=0;bank_live<=0;bank_known<=0;',
         'range_count<=0;declare_index<=0;bank_live<=0;bank_known<=0;retire_bank<=0;'),
        ('   if(event_valid && event_ready)event_pending<=0;',
         "   retire_bank<=retire_bank+1'b1;\n   if(event_valid && event_ready)event_pending<=0;"),
    ]
    for before, after in changes:
        assert source.count(before) == 1, before
        source = source.replace(before, after)
    return source
