"""Offline scanner review notes and bounded disassembly; no printer access."""
import json, re, hashlib
from pathlib import Path
from datetime import datetime, timezone
from capstone import Cs, CS_ARCH_ARM, CS_MODE_BIG_ENDIAN, CS_MODE_THUMB
ROOT = Path(__file__).resolve().parents[1]
DATA = (ROOT/'artifacts/ghs/dram.bin').read_bytes()
MD = Cs(CS_ARCH_ARM, CS_MODE_BIG_ENDIAN|CS_MODE_THUMB)
def string(a):
    return DATA[a&0x3ffffff:(a&0x3ffffff)+240].split(b'\0')[0].decode('ascii',errors='replace')
def u32(a):
    return int.from_bytes(DATA[a&0x3ffffff:(a&0x3ffffff)+4],'big')
def dis(a,n):
    result=[]
    for x in MD.disasm(DATA[a&0x3ffffff:(a&0x3ffffff)+n],a):
        ann=''
        m=re.search(r'\[pc, #(0x[0-9a-f]+|\d+)\]',x.op_str)
        if m:
            loc=((x.address+4)&~3)+int(m[1],0)
            v=u32(loc);ann=f' [{loc:#x}]={v:#x}'
        if x.mnemonic=='adr':
            m=re.search(r'#(0x[0-9a-f]+|\d+)',x.op_str)
            if m:
                loc=((x.address+4)&~3)+int(m[1],0);ann=f' {string(loc)!r}'
        result.append(f'{x.address:#x}: {x.mnemonic} {x.op_str}{ann}')
    return '\n'.join(result)
if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    if len(sys.argv)>1:
        print(dis(int(sys.argv[1],0),int(sys.argv[2],0)))
