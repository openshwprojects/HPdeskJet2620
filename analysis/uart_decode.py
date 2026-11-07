"""Read-only decoder for the bootloader's LZSS-packed UART code.

The algorithm is translated from the firmware's Thumb routine at flash 0x3100.
This is an analysis helper, not an emulator boot bypass.
"""
from pathlib import Path


_UNPACK_CACHE: tuple[bytes | None, bytes] = (None, b'')


def unpack(packed: bytes) -> bytes:
    global _UNPACK_CACHE
    if type(packed) is bytes and (packed is _UNPACK_CACHE[0] or packed == _UNPACK_CACHE[0]):
        return _UNPACK_CACHE[1]
    output = bytearray()
    out_append = output.append
    out_extend = output.extend
    packed_len = len(packed)
    cursor = flags = 0
    ring = 0xFEE
    while cursor < packed_len:
        flags >>= 1
        if not flags & 0x100:
            flags = 0xFF00 | packed[cursor]
            cursor += 1
        value = packed[cursor]
        cursor += 1
        if flags & 1:
            out_append(value)
            ring = (ring + 1) & 0xFFF
        else:
            second = packed[cursor]
            cursor += 1
            offset = value | ((second & 0xF0) << 4)
            length = (second & 0x0F) + 3
            out_len = len(output)
            source = out_len + offset - ring
            if ring <= offset:
                source -= 0x1000
            if source >= 0 and source + length <= out_len:
                out_extend(output[source : source + length])
            else:
                for _ in range(length):
                    out_append(output[source] if source >= 0 else 0)
                    source += 1
            ring = (ring + length) & 0xFFF
    result = bytes(output)
    if type(packed) is bytes:
        _UNPACK_CACHE = (packed, result)
    return result


if __name__ == "__main__":
    import argparse
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_BIG_ENDIAN

    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", type=Path, default=Path(__file__).resolve().parents[1] / "dump.bin")
    parser.add_argument("--start", type=lambda x: int(x, 0), default=0xA000FED2)
    parser.add_argument("--end", type=lambda x: int(x, 0), default=0xA001004C)
    parser.add_argument("--words", action="store_true")
    args = parser.parse_args()
    image = unpack(args.dump.read_bytes()[0x39EC:0xC9DB])
    base = 0xA0005720
    print(f"Expanded {len(image):#x} bytes to {base:#x}..{base+len(image):#x}")
    if args.words:
        for address in range(args.start, args.end, 4):
            value = int.from_bytes(image[address-base:address-base+4], "big")
            print(f"{address:#010x}: {value:#010x}")
    else:
        disassembler = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_BIG_ENDIAN)
        for insn in disassembler.disasm(image[args.start-base:args.end-base], args.start):
            print(f"{insn.address:#010x}: {insn.mnemonic:8} {insn.op_str}")
