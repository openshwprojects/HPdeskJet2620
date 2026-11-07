from pathlib import Path
from analysis.uart_decode import unpack
from capstone import Cs,CS_ARCH_ARM,CS_MODE_THUMB,CS_MODE_ARM,CS_MODE_BIG_ENDIAN
import sys,re
raw=Path('dump.bin').read_bytes(); exp=unpack(raw[0x39ec:0xc9db]);base=0xa0005720
start=int(sys.argv[1],0);end=int(sys.argv[2],0);thumb=len(sys.argv)<4 or sys.argv[3]!='arm'
data=exp if start>=base else raw;db=base if start>=base else 0
c=Cs(CS_ARCH_ARM,(CS_MODE_THUMB if thumb else CS_MODE_ARM)|CS_MODE_BIG_ENDIAN)
for i in c.disasm(data[start-db:end-db],start):
    annotation=''
    m=re.search(r'\[pc, #(0x[0-9a-f]+|\d+)\]',i.op_str)
    if m:
        target=((i.address+4)&~3 if thumb else i.address+8)+int(m[1],0)
        if db<=target<db+len(data):
            value=int.from_bytes(data[target-db:target-db+4],'big');annotation=f' ; [{target:#x}]={value:#010x}'
    print(f'{i.address:#010x}: {i.mnemonic:8} {i.op_str}{annotation}')
