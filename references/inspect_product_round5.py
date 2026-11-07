"""Offline image word/string inspection, never uses UART."""
from pathlib import Path
import sys
import re

data = (Path(__file__).resolve().parents[1] / 'artifacts/ghs/dram.bin').read_bytes()
for arg in sys.argv[1:]:
    address = int(arg, 0)
    print('\n', hex(address), 'references', [hex(x.start()+0xa0000000) for x in re.finditer(re.escape(address.to_bytes(4,'big')),data)][:50])
    for a in range(address, address+192, 16):
        row=[]
        for j in range(4):
            off=(a+4*j)&0x3ffffff
            word=int.from_bytes(data[off:off+4],'big')
            label=hex(word)
            if 0xa0000000 <= word < 0xa4000000:
                v=data[word&0x3ffffff:(word&0x3ffffff)+100].split(b'\0')[0]
                if v and all(32<=x<127 or x in [10,13] for x in v): label+=' '+repr(v.decode())
            row.append(label)
        print(hex(a), ' | '.join(row))
