"""Pure-Python bare-metal ARMv5 big-endian firmware builder for HP DeskJet 2620.

Builds a standalone 16 MiB flash image (or compact ROM binary) that boots from
the hardware reset vector ``0x00000000`` on the unmodified ``Emulator`` (with
exact parity between ``native_observer=False`` and ``native_observer=True``).

Upon reset, the custom firmware:
1. Executes the standard ARMv5 reset vector at ``0x00000000`` -> ``0x00002070``.
2. Initializes ``SP = 0x20100000`` in DDR DRAM.
3. Configures GPIO 28 (``power_led``) and GPIO 0 (``wireless_led``) as outputs
   and drives their requested LED states.
4. Arms the front-panel shift register (``0xBD0B1418 = 0x0009`` + GPIO 26 strobe
   ``0 -> 1``), shifts out a 64-bit encoded display frame while sampling the
   panel's MISO line (``0xBD0B1410``), and latches the frame onto the LCD/LEDs
   with a second GPIO 26 strobe pulse.
5. Extracts the 8-bit hardware signature (bits ``18..25``, expected ``0x35``)
   and 18-bit active-low button mask (bits ``0..17``), recording them in the
   DRAM mailbox at ``0x20000100..0x20000110``.
6. Transmits a diagnostic status banner over UART0 (``0xBD0B3C0E`` / ``0xBD0B3C06``),
   waits for the final UART frame to complete, and enters a clean halt loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from memory import FLASH_SIZE
from panel import SerialShiftPanel


MAILBOX_BASE = 0x20000100
MAILBOX_RAW_INPUT = 0x20000100
MAILBOX_SIGNATURE = 0x20000104
MAILBOX_BUTTON_MASK = 0x20000108
MAILBOX_STATUS_MAGIC = 0x2000010C
STATUS_MAGIC_OK = 0xC0DE0035
STATUS_MAGIC_BAD_SIG = 0xC0DEBAD0


@dataclass(frozen=True)
class CustomFirmwareMetadata:
    reset_entry: int
    halt_pc: int
    frame_64: int
    banner: bytes
    mailbox_base: int = MAILBOX_BASE


class ArmBeAssembler:
    """Minimal deterministic ARMv5 big-endian machine-code emitter."""

    def __init__(self, base_address: int = 0):
        self.base_address = base_address
        self.words: list[int] = []
        self.labels: dict[str, int] = {}
        self._fixups: list[tuple[int, str, str, int]] = []

    @property
    def pc(self) -> int:
        return self.base_address + len(self.words) * 4

    def label(self, name: str) -> int:
        addr = self.pc
        self.labels[name] = addr
        return addr

    def emit(self, word: int) -> int:
        addr = self.pc
        self.words.append(word & 0xFFFFFFFF)
        return addr

    def mov_imm(self, rd: int, imm8: int, rot_right_even: int = 0, *, cond: int = 0xE) -> int:
        rot_field = (rot_right_even // 2) & 0xF
        return self.emit((cond << 28) | (0x3A << 20) | (rd << 12) | (rot_field << 8) | (imm8 & 0xFF))

    def mov_reg(self, rd: int, rm: int, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | (0x1A << 20) | (rd << 12) | rm)

    def mov_lsl_imm(self, rd: int, rm: int, shift: int, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | (0x1A << 20) | (rd << 12) | ((shift & 0x1F) << 7) | rm)

    def mov_lsr_imm(self, rd: int, rm: int, shift: int, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | (0x1A << 20) | (rd << 12) | ((shift & 0x1F) << 7) | (1 << 5) | rm)

    def mov_lsr_reg(self, rd: int, rm: int, rs: int, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | (0x1A << 20) | (rd << 12) | (rs << 8) | (1 << 5) | (1 << 4) | rm)

    def mov_lsl_reg(self, rd: int, rm: int, rs: int, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | (0x1A << 20) | (rd << 12) | (rs << 8) | (0 << 5) | (1 << 4) | rm)

    def add_imm(self, rd: int, rn: int, imm8: int, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | (0x28 << 20) | (rn << 16) | (rd << 12) | (imm8 & 0xFF))

    def sub_imm(self, rd: int, rn: int, imm8: int, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | (0x24 << 20) | (rn << 16) | (rd << 12) | (imm8 & 0xFF))

    def and_imm(self, rd: int, rn: int, imm8: int, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | (0x20 << 20) | (rn << 16) | (rd << 12) | (imm8 & 0xFF))

    def and_reg(self, rd: int, rn: int, rm: int, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | (0x00 << 20) | (rn << 16) | (rd << 12) | (rm & 0xF))

    def mvn_imm(self, rd: int, imm8: int, rot_right_even: int = 0, *, cond: int = 0xE) -> int:
        rot_field = (rot_right_even // 2) & 0xF
        return self.emit((cond << 28) | (0x3E << 20) | (rd << 12) | (rot_field << 8) | (imm8 & 0xFF))

    def orr_imm(self, rd: int, rn: int, imm8: int, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | (0x38 << 20) | (rn << 16) | (rd << 12) | (imm8 & 0xFF))

    def orr_reg(self, rd: int, rn: int, rm: int, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | (0x18 << 20) | (rn << 16) | (rd << 12) | rm)

    def cmp_imm(self, rn: int, imm8: int, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | (0x35 << 20) | (rn << 16) | (imm8 & 0xFF))

    def tst_imm(self, rn: int, imm8: int, rot_right_even: int = 0, *, cond: int = 0xE) -> int:
        rot_field = (rot_right_even // 2) & 0xF
        return self.emit((cond << 28) | (0x31 << 20) | (rn << 16) | (rot_field << 8) | (imm8 & 0xFF))

    def str_imm(self, rd: int, rn: int, offset: int = 0, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | (0x58 << 20) | (rn << 16) | (rd << 12) | (offset & 0xFFF))

    def strh_imm(self, rd: int, rn: int, offset: int = 0, *, cond: int = 0xE) -> int:
        hi = (offset >> 4) & 0xF
        lo = offset & 0xF
        return self.emit((cond << 28) | (0x1C << 20) | (rn << 16) | (rd << 12) | (hi << 8) | 0xB0 | lo)

    def ldrh_imm(self, rd: int, rn: int, offset: int = 0, *, cond: int = 0xE) -> int:
        hi = (offset >> 4) & 0xF
        lo = offset & 0xF
        return self.emit((cond << 28) | (0x1D << 20) | (rn << 16) | (rd << 12) | (hi << 8) | 0xB0 | lo)

    def ldrb_post_inc(self, rd: int, rn: int, step: int = 1, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | (0x4D << 20) | (rn << 16) | (rd << 12) | (step & 0xFFF))

    def ldr_lit(self, rd: int, label_name: str, *, cond: int = 0xE) -> int:
        idx = len(self.words)
        self._fixups.append((idx, 'ldr_lit', label_name, (cond << 4) | rd))
        return self.emit(0)

    def adr(self, rd: int, label_name: str, *, cond: int = 0xE) -> int:
        idx = len(self.words)
        self._fixups.append((idx, 'adr', label_name, (cond << 4) | rd))
        return self.emit(0)

    def b(self, label_name: str, *, cond: int = 0xE) -> int:
        idx = len(self.words)
        self._fixups.append((idx, 'b', label_name, cond))
        return self.emit(0)

    def bl(self, label_name: str, *, cond: int = 0xE) -> int:
        idx = len(self.words)
        self._fixups.append((idx, 'bl', label_name, cond))
        return self.emit(0)

    def bx(self, rm: int, *, cond: int = 0xE) -> int:
        return self.emit((cond << 28) | 0x012FFF10 | (rm & 0xF))

    def finalize(self) -> bytes:
        for idx, kind, label_name, meta in self._fixups:
            if label_name not in self.labels:
                raise KeyError(f'undefined label: {label_name}')
            target = self.labels[label_name]
            insn_pc = self.base_address + idx * 4 + 8
            delta = target - insn_pc
            if kind in ('b', 'bl'):
                cond = meta & 0xF
                imm24 = (delta >> 2) & 0xFFFFFF
                op = 0xB if kind == 'bl' else 0xA
                self.words[idx] = (cond << 28) | (op << 24) | imm24
            elif kind == 'ldr_lit':
                cond = (meta >> 4) & 0xF
                rd = meta & 0xF
                up = 1 if delta >= 0 else 0
                off = abs(delta)
                if off > 0xFFF:
                    raise ValueError(f'literal pool offset out of range: {off}')
                self.words[idx] = (cond << 28) | (0x51 << 20) | (up << 23) | (0xF << 16) | (rd << 12) | off
            elif kind == 'adr':
                cond = (meta >> 4) & 0xF
                rd = meta & 0xF
                if delta >= 0 and delta <= 0xFF:
                    self.words[idx] = (cond << 28) | (0x28 << 20) | (0xF << 16) | (rd << 12) | delta
                elif delta >= 0 and (delta & 3) == 0 and (delta >> 2) <= 0xFF:
                    # Rotate right by 30 == shift left by 2
                    self.words[idx] = (cond << 28) | (0x28 << 20) | (0xF << 16) | (rd << 12) | (15 << 8) | (delta >> 2)
                else:
                    raise ValueError(f'adr delta out of range: {delta}')
        return b''.join(w.to_bytes(4, 'big') for w in self.words)


def build_custom_firmware_payload(
    *,
    base_address: int = 0x00002070,
    banner: bytes = b'[CustomFW] HP DeskJet 2620 Bare-Metal ARMv5 OS Ready (panel=0x35)\r\n',
    seven_seg: str = 'A',
    wifi_bars: int = 5,
    color_ink_bars: int = 3,
    black_ink_bars: int = 3,
    indicators: Iterable[str | int] = (),
    power_led: bool = True,
    wireless_led: bool = True,
    init_uart_hw: bool = True,
    feed_watchdog: bool = True,
) -> tuple[bytes, CustomFirmwareMetadata]:
    """Assemble position-configured ARMv5 big-endian custom firmware payload bytes and metadata."""
    frame_64 = SerialShiftPanel.encode_frame(
        seven_seg_char=seven_seg,
        wifi_bars=wifi_bars,
        color_ink_bars=color_ink_bars,
        black_ink_bars=black_ink_bars,
        indicators=tuple(indicators),
    )
    frame_lo = frame_64 & 0xFFFFFFFF
    frame_hi = (frame_64 >> 32) & 0xFFFFFFFF

    asm = ArmBeAssembler(base_address=base_address)

    # 1. Initialize stack pointer and peripheral base pointers
    asm.label('_reset_entry')
    asm.ldr_lit(13, 'lit_stack_top')       # sp = 0x20100000
    asm.ldr_lit(8, 'lit_gpio_base')        # r8 = 0xBD0B2000 (GPIO controller)
    asm.ldr_lit(9, 'lit_panel_base')       # r9 = 0xBD0B1400 (Front-panel controller)
    asm.ldr_lit(10, 'lit_uart_base')       # r10 = 0xBD0B3C00 (UART0 controller)
    asm.ldr_lit(11, 'lit_mailbox_base')    # r11 = 0x20000100 (DRAM mailbox)

    # 1b. Feed/clear dual-stage hardware watchdog (0xBD040400 = 0x000C)
    if feed_watchdog:
        asm.ldr_lit(12, 'lit_wdog_base')   # r12 = 0xBD040400
        asm.mov_imm(0, 0x0C)
        asm.strh_imm(0, 12, 0x00)          # [0xBD040400] = 0x000C

    # 1c. Initialize AXON UART0 pad-mux, 115200 8N1 framing, mask, and baud/FIFO divisors
    if init_uart_hw:
        asm.label('uart_pre_init_flush')
        asm.ldrh_imm(1, 10, 0x0E)          # Wait for any in-flight Stage-1 UART byte to finish
        asm.tst_imm(1, 1, rot_right_even=24)
        asm.b('uart_pre_init_flush', cond=0x0)

        asm.ldr_lit(2, 'lit_padmux_base')  # r2 = 0xBD060800
        asm.mov_imm(0, 1)
        asm.strh_imm(0, 2, 0x0E)           # [0xBD06080E] = 0x0001 (UART0 TX pad-mux)
        asm.strh_imm(0, 2, 0x10)           # [0xBD060810] = 0x0001 (UART0 RX pad-mux)

        asm.mov_imm(0, 0x10, rot_right_even=24)  # r0 = 0x1000
        asm.strh_imm(0, 10, 0x00)          # [0xBD0B3C00] = 0x1000 (UART0 115200 8N1)
        asm.mov_imm(0, 0)
        asm.strh_imm(0, 10, 0x08)          # [0xBD0B3C08] = 0x0000
        asm.ldr_lit(0, 'lit_uart_mask')
        asm.strh_imm(0, 10, 0x04)          # [0xBD0B3C04] = 0x03FF (mask all UART0 IRQs)

        asm.ldr_lit(2, 'lit_uart_fifo_base')  # r2 = 0xBD0B4C00
        asm.ldr_lit(0, 'lit_uart_baud_div')   # r0 = 0x2111
        asm.strh_imm(0, 2, 0x00)           # [0xBD0B4C00] = 0x2111
        asm.strh_imm(0, 2, 0x02)           # [0xBD0B4C02] = 0x2111

    # 2. Configure GPIO 28 (power_led, active-low) and GPIO 0 (wireless_led, active-low)
    asm.mov_imm(0, 1)
    asm.mov_imm(1, 0 if power_led else 1)
    asm.strh_imm(1, 8, 28 * 8 + 2)         # GPIO 28 output latch (0 = ON)
    asm.strh_imm(0, 8, 28 * 8 + 4)         # GPIO 28 direction = output (1)

    asm.mov_imm(1, 0 if wireless_led else 1)
    asm.strh_imm(1, 8, 0 * 8 + 2)          # GPIO 0 output latch (0 = ON)
    asm.strh_imm(0, 8, 0 * 8 + 4)          # GPIO 0 direction = output (1)

    # 3. Arm the front-panel serial shift register (OUTPUT=9, Strobe 0 -> 1)
    asm.mov_imm(1, 9)
    asm.strh_imm(1, 9, 0x18)               # 0xBD0B1418 = 0x0009
    asm.mov_imm(1, 0)
    asm.strh_imm(1, 8, 26 * 8 + 2)         # GPIO 26 output latch = 0
    asm.strh_imm(0, 8, 26 * 8 + 4)         # GPIO 26 direction = output (1)
    asm.strh_imm(0, 8, 26 * 8 + 2)         # GPIO 26 output latch = 1 (loads panel shift input)

    # 4. Shift 64 bits out to the front-panel while sampling MISO bits 0..25
    asm.ldr_lit(5, 'lit_frame_lo')         # r5 = low 32 bits of 64-bit display frame
    asm.ldr_lit(6, 'lit_frame_hi')         # r6 = high 32 bits of 64-bit display frame
    asm.mov_imm(4, 0)                      # r4 = bit index (0..63)
    asm.mov_imm(7, 0)                      # r7 = sampled 26-bit panel input word

    asm.label('shift_loop')
    # Extract bit r4 from (r5, r6)
    asm.cmp_imm(4, 32)
    asm.b('high_word_bit', cond=0x2)       # BHS (r4 >= 32)
    asm.mov_lsr_reg(1, 5, 4)               # r1 = r5 >> r4
    asm.b('drive_mosi')
    asm.label('high_word_bit')
    asm.sub_imm(2, 4, 32)                  # r2 = r4 - 32
    asm.mov_lsr_reg(1, 6, 2)               # r1 = r6 >> r2

    asm.label('drive_mosi')
    asm.and_imm(1, 1, 1)                   # r1 = bit & 1
    asm.mov_lsl_imm(1, 1, 2)               # r1 = (bit & 1) << 2 (MOSI in bit 2)
    asm.orr_imm(1, 1, 0x08)                # r1 |= 0x08 (reset bit 3 deasserted, SCLK bit 1 = 0)
    asm.strh_imm(1, 9, 0x18)               # 0xBD0B1418 = RESET_N | MOSI | SCLK(0)
    asm.orr_imm(2, 1, 0x02)                # r2 = RESET_N | MOSI | SCLK(1)
    asm.strh_imm(2, 9, 0x18)               # 0xBD0B1418 = RESET_N | MOSI | SCLK(1) (rising edge)

    # Sample MISO (bit 8 of 0xBD0B1410) after rising clock edge for bits 0..25
    asm.cmp_imm(4, 26)
    asm.b('skip_miso', cond=0x2)           # BHS (if r4 >= 26, skip MISO accumulation)
    asm.ldrh_imm(0, 9, 0x10)               # r0 = [0xBD0B1410] (Panel INPUT)
    asm.mov_lsr_imm(0, 0, 8)               # r0 >>= 8 (MISO is bit 8)
    asm.and_imm(0, 0, 1)                   # r0 &= 1
    asm.mov_lsl_reg(0, 0, 4)               # r0 <<= r4
    asm.orr_reg(7, 7, 0)                   # r7 |= r0
    asm.label('skip_miso')

    asm.add_imm(4, 4, 1)                   # r4 += 1
    asm.cmp_imm(4, 64)
    asm.b('shift_loop', cond=0x3)          # BLO (r4 < 64)

    # 5. Pulse GPIO 26 strobe (0 -> 1) to latch the 64-bit display frame
    asm.mov_imm(1, 0)
    asm.strh_imm(1, 8, 26 * 8 + 2)         # GPIO 26 = 0
    asm.mov_imm(1, 1)
    asm.strh_imm(1, 8, 26 * 8 + 2)         # GPIO 26 = 1 (latches 64-bit frame)

    # 6. Write sampled panel input, signature, and button mask to DRAM mailbox
    asm.str_imm(7, 11, 0x00)               # [0x20000100] = raw 32-bit panel input
    asm.mov_lsr_imm(1, 7, 18)
    asm.and_imm(1, 1, 0xFF)                # r1 = (r7 >> 18) & 0xFF (panel signature)
    asm.str_imm(1, 11, 0x04)               # [0x20000104] = signature (0x35)
    asm.ldr_lit(2, 'lit_button_mask')
    asm.and_reg(2, 7, 2)                   # and r2, r7, r2
    asm.str_imm(2, 11, 0x08)               # [0x20000108] = 18-bit button mask

    # Verify panel signature == 0x35
    asm.cmp_imm(1, 0x35)
    asm.ldr_lit(3, 'lit_magic_ok', cond=0x0)       # MOVEQ r3, =STATUS_MAGIC_OK
    asm.ldr_lit(3, 'lit_magic_bad', cond=0x1)      # MOVNE r3, =STATUS_MAGIC_BAD_SIG
    asm.str_imm(3, 11, 0x0C)               # [0x2000010C] = status magic

    # 7. Transmit banner over UART0 (polling UART_STATUS bit 8 at +0x0E, writing +0x06)
    asm.ldr_lit(4, 'lit_banner_addr')
    asm.label('uart_next_byte')
    asm.ldrb_post_inc(0, 4, 1)             # ldrb r0, [r4], #1
    asm.cmp_imm(0, 0)
    asm.b('uart_flush', cond=0x0)          # BEQ uart_flush
    asm.label('uart_wait_tx_ready')
    asm.ldrh_imm(1, 10, 0x0E)              # r1 = [0xBD0B3C0E] (UART_STATUS)
    asm.tst_imm(1, 1, rot_right_even=24)   # tst r1, #0x100
    asm.b('uart_wait_tx_ready', cond=0x0)  # BEQ uart_wait_tx_ready
    asm.strh_imm(0, 10, 0x06)              # [0xBD0B3C06] = r0 (UART_TX)
    asm.b('uart_next_byte')

    # Wait for the final byte's UART frame to complete
    asm.label('uart_flush')
    asm.ldrh_imm(1, 10, 0x0E)
    asm.tst_imm(1, 1, rot_right_even=24)
    asm.b('uart_flush', cond=0x0)

    if feed_watchdog:
        asm.mov_imm(0, 0x0C)
        asm.strh_imm(0, 12, 0x00)          # Feed watchdog before halting

    # 8. Clean terminal halt instruction
    halt_pc = asm.label('halt_loop')
    asm.b('halt_loop')

    # Literal pool
    asm.label('lit_stack_top')
    asm.emit(0x20100000)
    asm.label('lit_gpio_base')
    asm.emit(0xBD0B2000)
    asm.label('lit_panel_base')
    asm.emit(0xBD0B1400)
    asm.label('lit_uart_base')
    asm.emit(0xBD0B3C00)
    asm.label('lit_mailbox_base')
    asm.emit(MAILBOX_BASE)
    if feed_watchdog:
        asm.label('lit_wdog_base')
        asm.emit(0xBD040400)
    if init_uart_hw:
        asm.label('lit_padmux_base')
        asm.emit(0xBD060800)
        asm.label('lit_uart_mask')
        asm.emit(0x000003FF)
        asm.label('lit_uart_fifo_base')
        asm.emit(0xBD0B4C00)
        asm.label('lit_uart_baud_div')
        asm.emit(0x00002111)
    asm.label('lit_frame_lo')
    asm.emit(frame_lo)
    asm.label('lit_frame_hi')
    asm.emit(frame_hi)
    asm.label('lit_button_mask')
    asm.emit(0x0003FFFF)
    asm.label('lit_magic_ok')
    asm.emit(STATUS_MAGIC_OK)
    asm.label('lit_magic_bad')
    asm.emit(STATUS_MAGIC_BAD_SIG)
    asm.label('lit_banner_addr')
    banner_addr = asm.pc + 4
    asm.emit(banner_addr)

    code_bytes = asm.finalize()
    banner_Null = bytes(banner) + b'\x00'
    if len(banner_Null) % 4:
        banner_Null += b'\x00' * (4 - (len(banner_Null) % 4))

    meta = CustomFirmwareMetadata(
        reset_entry=base_address,
        halt_pc=halt_pc,
        frame_64=frame_64,
        banner=bytes(banner),
    )
    return code_bytes + banner_Null, meta


def build_custom_firmware_rom(
    *,
    banner: bytes = b'[CustomFW] HP DeskJet 2620 Bare-Metal ARMv5 OS Ready (panel=0x35)\r\n',
    seven_seg: str = 'A',
    wifi_bars: int = 5,
    color_ink_bars: int = 3,
    black_ink_bars: int = 3,
    indicators: Iterable[str | int] = (),
    power_led: bool = True,
    wireless_led: bool = True,
    init_uart_hw: bool = True,
    feed_watchdog: bool = True,
) -> tuple[bytes, CustomFirmwareMetadata]:
    """Assemble a bare-metal ARMv5 big-endian ROM prefix (0x00000000..end) and its metadata."""
    reset_entry = 0x00002070
    payload, meta = build_custom_firmware_payload(
        base_address=reset_entry,
        banner=banner,
        seven_seg=seven_seg,
        wifi_bars=wifi_bars,
        color_ink_bars=color_ink_bars,
        black_ink_bars=black_ink_bars,
        indicators=indicators,
        power_led=power_led,
        wireless_led=wireless_led,
        init_uart_hw=init_uart_hw,
        feed_watchdog=feed_watchdog,
    )
    return _build_reset_vector_rom(payload, reset_entry), meta


def _build_reset_vector_rom(
    payload: bytes,
    reset_entry: int,
    entry_addr: int | None = None,
) -> bytes:
    """Wrap a bare-metal payload in an ARMv5 hardware reset vector stub at 0x00000000."""
    if entry_addr is None:
        entry_addr = reset_entry
    rom = bytearray(reset_entry + len(payload))
    # Hardware reset vector at 0x00000000: ldr pc, [pc, #0x18] -> loads [0x00000020]
    rom[0x00:0x04] = b'\xE5\x9F\xF0\x18'
    for vec_off in range(0x04, 0x20, 4):
        rom[vec_off:vec_off + 4] = b'\xEA\xFF\xFF\xFE'  # b .
    rom[0x20:0x24] = entry_addr.to_bytes(4, 'big')
    rom[reset_entry:reset_entry + len(payload)] = payload
    return bytes(rom)


def _patch_stage2_container(
    base_dump: bytes,
    payload: bytes,
    *,
    load_address: int,
    entry_addr: int,
    flash_payload_offset: int,
) -> bytes:
    """Patch the HP Stage-1 container header at 0x00020030 to load and jump to `payload`."""
    import struct

    if len(base_dump) != FLASH_SIZE:
        raise ValueError(f'base_dump must be {FLASH_SIZE} bytes, got {len(base_dump)}')
    img = bytearray(base_dump)
    img[flash_payload_offset:flash_payload_offset + len(payload)] = payload

    # Point Stage-1 container header (0x00020030) to load and jump to our custom payload:
    # +0x38 (0x20068): Entry point address
    # +0x48..+0x50 (0x20078..0x20080): Zero-fill scatter range (empty)
    # +0x54..+0x5C (0x20084..0x2008C): Raw-copy scatter range (1 entry at 0x201EC..0x201F8)
    # +0x60..+0x68 (0x20090..0x20098): LZSS scatter range (empty)
    struct.pack_into('>I', img, 0x00020068, entry_addr)
    struct.pack_into('>3I', img, 0x00020078, 0x00020180, 0x00020180, 0x00020180)
    struct.pack_into('>3I', img, 0x00020084, 0x000201EC, 0x000201F8, 0x000201EC)
    struct.pack_into('>3I', img, 0x00020090, 0x000202D0, 0x000202D0, 0x000202D0)
    struct.pack_into('>3I', img, 0x000201EC, load_address, flash_payload_offset, len(payload))
    return bytes(img)


def build_custom_firmware_image(**kwargs) -> tuple[bytes, CustomFirmwareMetadata]:
    """Build a full 16 MiB flash image suitable for booting directly in ``Emulator``."""
    rom_prefix, meta = build_custom_firmware_rom(**kwargs)
    image = rom_prefix + (b'\xFF' * (FLASH_SIZE - len(rom_prefix)))
    return image, meta


def build_stage2_container_image(
    base_dump: bytes,
    *,
    load_address: int = 0xA0020000,
    flash_payload_offset: int = 0x00022000,
    **kwargs,
) -> tuple[bytes, CustomFirmwareMetadata]:
    """Package custom ARMv5 firmware inside the HP Stage-1 bootloader container at ``0x00020030``.

    Preserves the original HP Stage-1 bootloader (``0x00000000..0x0002002F``) so that
    cold reset initializes PLLs, 64 MiB DDR SDRAM, UART0 (115200 8N1), MICCI2, I2C,
    GPIO, Front-Panel, and SPI Flash before copying the custom payload to ``load_address``
    and jumping to it via ``[Boot] calling (*0xa0020000)()``.
    """
    if len(base_dump) != FLASH_SIZE:
        raise ValueError(f'base_dump must be {FLASH_SIZE} bytes, got {len(base_dump)}')
    payload, meta = build_custom_firmware_payload(base_address=load_address, **kwargs)
    img = _patch_stage2_container(
        base_dump,
        payload,
        load_address=load_address,
        entry_addr=load_address,
        flash_payload_offset=flash_payload_offset,
    )
    return img, meta


_CACHED_COMPILER: str | None = None


def find_arm_c_compiler() -> str | None:
    """Locate a C compiler capable of cross-compiling ARMv5 big-endian code."""
    global _CACHED_COMPILER
    if _CACHED_COMPILER is not None:
        return _CACHED_COMPILER
    import os
    from pathlib import Path
    import shutil
    env_cc = os.environ.get('ARM_CC')
    if env_cc:
        resolved = shutil.which(env_cc) or (env_cc if Path(env_cc).is_file() else None)
        if resolved:
            _CACHED_COMPILER = resolved
            return resolved
    for candidate in ('zig', 'clang', 'arm-none-eabi-gcc'):
        path = shutil.which(candidate)
        if path:
            _CACHED_COMPILER = path
            return path
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        arduino_gcc_root = Path(local_app_data) / 'Arduino15' / 'packages' / 'arduino' / 'tools' / 'arm-none-eabi-gcc'
        if arduino_gcc_root.is_dir():
            for bin_dir in sorted(arduino_gcc_root.glob('*/bin'), reverse=True):
                path = shutil.which('arm-none-eabi-gcc', path=str(bin_dir))
                if path:
                    _CACHED_COMPILER = path
                    return path
    return None


_C_COMPILE_CACHE: dict[tuple, tuple[bytes, int]] = {}


def compile_c_firmware(
    c_source: Path | str = 'firmware.c',
    *,
    load_address: int = 0xA0020000,
    entry_symbol: str = '_start',
    extra_flags: list[str] | None = None,
) -> tuple[bytes, int]:
    """Compile C source to freestanding ARMv5TE big-endian binary using zig cc or clang."""
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    compiler = find_arm_c_compiler()
    if not compiler:
        raise RuntimeError('No ARM C compiler found (zig, clang, or arm-none-eabi-gcc required)')

    c_path = Path(c_source).resolve()
    if not c_path.exists():
        raise FileNotFoundError(f'C source file not found: {c_path}')

    cache_key = (
        str(c_path),
        c_path.stat().st_mtime_ns,
        load_address,
        entry_symbol,
        tuple(extra_flags) if extra_flags else (),
    )
    if cache_key in _C_COMPILE_CACHE:
        return _C_COMPILE_CACHE[cache_key]

    with tempfile.TemporaryDirectory() as td:
        t_dir = Path(td)
        ld_path = t_dir / 'linker.ld'
        elf_path = t_dir / 'firmware.elf'
        bin_path = t_dir / 'firmware.bin'

        ld_path.write_text(f"""ENTRY({entry_symbol})
SECTIONS
{{
    . = {hex(load_address)};
    .text : {{
        *(.text.entry)
        *(.text*)
    }}
    .rodata : {{
        *(.rodata*)
    }}
    .data : {{
        *(.data*)
    }}
    .bss : {{
        *(.bss*)
        *(COMMON)
    }}
}}
""")

        compiler_name = Path(compiler).name.lower()
        is_zig = 'zig' in compiler_name
        is_gcc = 'gcc' in compiler_name and not is_zig
        if is_zig:
            cmd = [
                compiler, 'cc',
                '-target', 'armeb-freestanding-eabi',
                '-mcpu=arm946e_s',
                '-O2', '-ffreestanding', '-nostdlib',
                f'-Wl,-T,{ld_path}',
                '-Wl,--nmagic',
                f'-I{c_path.parent}',
                str(c_path),
                '-o', str(elf_path),
            ]
        elif is_gcc:
            cmd = [
                compiler,
                '-mbig-endian',
                '-mcpu=arm946e-s',
                '-O2', '-ffreestanding', '-nostdlib',
                '-Wl,--nmagic',
                f'-T{ld_path}',
                f'-I{c_path.parent}',
                str(c_path),
                '-o', str(elf_path),
            ]
        else:
            cmd = [
                compiler,
                '-target', 'armeb-none-eabi',
                '-mcpu=arm946e-s',
                '-O2', '-ffreestanding', '-nostdlib',
                f'-T{ld_path}',
                f'-I{c_path.parent}',
                str(c_path),
                '-o', str(elf_path),
            ]

        if extra_flags:
            cmd.extend(extra_flags)

        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f'C compilation failed (code {p.returncode}):\n{p.stderr}\nCommand: {" ".join(cmd)}')

        # Convert ELF to raw binary
        if is_zig:
            objcopy = [compiler, 'objcopy', '-O', 'binary', str(elf_path), str(bin_path)]
        else:
            compiler_dir = str(Path(compiler).parent)
            objcopy_bin = (
                shutil.which('arm-none-eabi-objcopy', path=compiler_dir)
                or shutil.which('llvm-objcopy', path=compiler_dir)
                or shutil.which('arm-none-eabi-objcopy')
                or shutil.which('llvm-objcopy')
                or 'objcopy'
            )
            objcopy = [objcopy_bin, '-O', 'binary', str(elf_path), str(bin_path)]

        p_obj = subprocess.run(objcopy, capture_output=True, text=True)
        if p_obj.returncode != 0:
            raise RuntimeError(f'objcopy failed (code {p_obj.returncode}):\n{p_obj.stderr}')

        elf_bytes = elf_path.read_bytes()
        entry_addr = int.from_bytes(elf_bytes[24:28], 'big')
        bin_bytes = bin_path.read_bytes()

        _C_COMPILE_CACHE[cache_key] = (bin_bytes, entry_addr)
        return bin_bytes, entry_addr


def build_c_stage2_container_image(
    base_dump: bytes,
    c_source: Path | str = 'firmware.c',
    *,
    load_address: int = 0xA0020000,
    flash_payload_offset: int = 0x00022000,
    **compile_kwargs,
) -> tuple[bytes, int]:
    """Compile C firmware and package it into the HP Stage-1 bootloader container."""
    if len(base_dump) != FLASH_SIZE:
        raise ValueError(f'base_dump must be {FLASH_SIZE} bytes, got {len(base_dump)}')

    c_bin, entry_addr = compile_c_firmware(c_source, load_address=load_address, **compile_kwargs)
    img = _patch_stage2_container(
        base_dump,
        c_bin,
        load_address=load_address,
        entry_addr=entry_addr,
        flash_payload_offset=flash_payload_offset,
    )
    return img, entry_addr


def build_c_custom_firmware_rom(
    c_source: Path | str = 'firmware.c',
    *,
    reset_entry: int = 0x00002070,
    **compile_kwargs,
) -> tuple[bytes, int]:
    """Compile C firmware into a standalone bare-metal ROM prefix (0x00000000..end)."""
    c_bin, entry_addr = compile_c_firmware(c_source, load_address=reset_entry, **compile_kwargs)
    return _build_reset_vector_rom(c_bin, reset_entry, entry_addr), entry_addr


def build_c_custom_firmware_image(
    c_source: Path | str = 'firmware.c',
    *,
    reset_entry: int = 0x00002070,
    **compile_kwargs,
) -> tuple[bytes, int]:
    """Compile C firmware and package it into a full 16 MiB flash image booting from reset vector 0x00000000."""
    from pathlib import Path
    rom, entry_addr = build_c_custom_firmware_rom(c_source, reset_entry=reset_entry, **compile_kwargs)
    image = rom + (b'\xFF' * (FLASH_SIZE - len(rom)))
    return image, entry_addr


def main(argv: list[str] | None = None) -> int:
    import argparse
    import struct
    from pathlib import Path
    from emulator import Emulator

    parser = argparse.ArgumentParser(description='Build and run custom bare-metal ARMv5 firmware for HP DeskJet 2620')
    parser.add_argument('--seven-seg', default='A', help='7-segment character to display (default: A)')
    parser.add_argument('--wifi-bars', type=int, default=5, help='Wi-Fi signal bars 0..5 (default: 5)')
    parser.add_argument('--color-bars', type=int, default=3, help='Color ink bars 0..3 (default: 3)')
    parser.add_argument('--black-bars', type=int, default=3, help='Black ink bars 0..3 (default: 3)')
    parser.add_argument('--out', type=Path, help='Optional output file path for the 16 MiB flash image')
    parser.add_argument('--compile-c', action='store_true', help='Compile C firmware and boot via Stage-1 container')
    parser.add_argument('--compile-c-standalone', action='store_true', help='Compile C firmware and boot standalone from ROM reset vector 0x00000000')
    parser.add_argument('--c-source', type=Path, default=Path('firmware.c'), help='Path to C firmware source (default: firmware.c)')
    args = parser.parse_args(argv)

    if args.compile_c_standalone:
        img, entry = build_c_custom_firmware_image(args.c_source)
        if args.out is not None:
            args.out.write_bytes(img)
        print(f'Compiled {args.c_source} -> Standalone ROM entry at 0x{entry:08x}')
        emu = Emulator(img, serial_panel_inputs=0x3FFFF, native_observer=True)
        emu.run(max_instructions=100_000, stop='budget')
        uart_text = bytes(emu.uart).decode('latin1', errors='replace')
        print(uart_text)
        dram = bytes(emu.memory._dram_mv)
        sig = struct.unpack_from('>I', dram, 0x00000104)[0]
        magic = struct.unpack_from('>I', dram, 0x0000010C)[0]
        print(f'DRAM Mailbox (0x20000100): signature=0x{sig:02x}, status=0x{magic:08x}')
        return 0

    if args.compile_c:
        from analysis.uart_selftest import load_default_dump
        dump = load_default_dump()
        img, entry = build_c_stage2_container_image(dump, args.c_source)
        if args.out is not None:
            args.out.write_bytes(img)
        print(f'Compiled {args.c_source} -> Stage-2 entry at 0x{entry:08x}')
        emu = Emulator(img, serial_panel_inputs=0x3FFFF, spi_profile='byte-final', native_observer=True)
        emu.run(max_instructions=4_500_000, stop='budget')
        uart_text = bytes(emu.uart).decode('latin1', errors='replace')
        print(uart_text[uart_text.find('[Boot] calling'):])
        dram = bytes(emu.memory._dram_mv)
        sig = struct.unpack_from('>I', dram, 0x00000104)[0]
        magic = struct.unpack_from('>I', dram, 0x0000010C)[0]
        print(f'DRAM Mailbox (0x20000100): signature=0x{sig:02x}, status=0x{magic:08x}')
        return 0

    image, meta = build_custom_firmware_image(
        seven_seg=args.seven_seg,
        wifi_bars=args.wifi_bars,
        color_ink_bars=args.color_bars,
        black_ink_bars=args.black_bars,
    )
    if args.out is not None:
        args.out.write_bytes(image)
    emu = Emulator(image, serial_panel_inputs=0x3FFFF, native_observer=True)
    emu.run(max_instructions=20_000, stop='budget')
    uart_text = bytes(emu.uart).decode('latin1', errors='replace')
    print(uart_text, end='')
    print(emu.board.panel.endpoint.render_display(show_temporal=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
