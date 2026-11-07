"""Drive a flash image through the emulator like a person at the UART console.

``VirtualPrinter`` boots a 16 MiB image (HP stage-1 + our bootloader +
application) with the writable SPI flash model, feeds UART input reactively,
transfers images over XMODEM, and can "power cycle" into whatever the firmware
wrote to flash. Execution continues across calls, so a test can type a
command, wait for its output and continue.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

from .xmodem import XmodemSender

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from emulator import Emulator  # noqa: E402
from unicorn.arm_const import UC_ARM_REG_CPSR, UC_ARM_REG_PC  # noqa: E402

REBOOT_MARKER = b'[reboot] arming watchdog'


class HarnessError(RuntimeError):
    pass


class VirtualPrinter:
    """One powered-on emulated printer running ``flash_image``."""

    def __init__(self, flash_image: bytes, *, native_observer: bool = True, fast_uart: bool = True,
                 wip_read_latency: int = 2, echo: bool = False, eeprom: bytes | None = None,
                 spi_profile: str = 'byte-final', uart_frame_ticks: int | None = None,
                 init_dram: bytes | None = None, spi_rx_read_shift: bool = False,
                 wifi_profile: str = 'printer'):
        # Explicit board inputs (same levels as emulator.py --full-emulation);
        # the strict GPIO model faults on reads of unconfigured pins.
        # ``spi_profile`` selects the AXON serializer hypothesis the emulator
        # implements ('byte-final' or 'wide-final'); the firmware's "flash
        # mode" must match it for writes to succeed (see fw/common/spi_flash.h).
        self.spi_profile = spi_profile
        # Analog inputs: half scale on every MCASIC ADC selector the model does
        # not derive itself (0 = supply, 15 = reference), like --full-emulation.
        analog_inputs = {i: 512 for i in range(1, 20) if i != 15}
        self.emu = Emulator(flash_image, serial_panel_inputs=0x3FFFF, spi_profile=spi_profile,
                            spi_writable=True, spi_wip_read_latency=wip_read_latency,
                            gpio_inputs={1: 0, 2: 0, 3: 0, 8: 1, 11: 0, 15: 1},
                            analog_inputs=analog_inputs, init_dram=init_dram,
                            native_observer=native_observer, eeprom=eeprom, on_uart=self._on_uart)
        # The Wi-Fi chip as the owner's printer reported it (sdio.py profiles);
        # HP's own firmware runs keep the model they were built against.
        self.emu.board.sdio.card.set_profile(wifi_profile)
        # Reproduces the first-boot observation on the physical printer (see
        # spi.SPIController.rx_read_shift); kept across power cycles.
        self.spi_rx_read_shift = spi_rx_read_shift
        self.emu.board.spi.rx_read_shift = spi_rx_read_shift
        if spi_rx_read_shift and self.emu.spi_flash is not None:
            self.emu.spi_flash.relaxed_jedec = True
        # fast_uart makes every UART frame complete after one tick so the shell
        # output costs no virtual time (the firmware's UART-based timer
        # calibration then rejects the implausible result and keeps its
        # compiled default). uart_frame_ticks overrides it; 347 ticks is the
        # real 115200 8N1 frame at the emulator's 4 ticks per microsecond.
        if uart_frame_ticks is not None:
            self.emu.board.uart_frame_ticks = uart_frame_ticks
        elif fast_uart:
            self.emu.board.uart_frame_ticks = 1
        self.echo = echo
        self._started = False
        self._stop_pattern: bytes | re.Pattern | None = None
        self._stop_from = 0
        self._sender: XmodemSender | None = None
        self._halted = False
        self.transcript: list[tuple[str, str]] = []

    # -- UART plumbing ---------------------------------------------------------
    def _on_uart(self, value: int) -> None:
        if self.echo:
            sys.stdout.write(chr(value) if 0x09 <= value < 0x7F else f'<{value:02x}>')
            sys.stdout.flush()
        if self._sender is not None:
            self._sender.feed(value)
            out = self._sender.pending()
            if out:
                self.emu.board.queue_rx(out)
            if self._sender.done:
                self.emu.reason = 'xmodem-done'
                self.emu.uc.emu_stop()
                return
        if self._stop_pattern is not None and self._matches(self._stop_pattern, self._stop_from):
            self.emu.reason = 'pattern'
            self.emu.uc.emu_stop()

    def _matches(self, pattern: bytes | re.Pattern, start: int) -> bool:
        window = bytes(self.emu.uart[start:])
        if isinstance(pattern, bytes):
            return window.endswith(pattern) if pattern.endswith(b'> ') else pattern in window
        return pattern.search(window) is not None

    def uart_bytes(self, start: int = 0) -> bytes:
        return bytes(self.emu.uart[start:])

    def uart_text(self, start: int = 0) -> str:
        return self.uart_bytes(start).decode('latin1')

    def send(self, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode('latin1')
        self.emu.board.queue_rx(data)

    # -- execution ---------------------------------------------------------------
    def _instructions(self) -> int:
        obs = self.emu._native_observer
        return obs.state.instructions if obs is not None else self.emu.instructions

    def _continue(self, budget: int) -> str:
        emu = self.emu
        if self._halted:
            raise HarnessError('emulator halted on an error; power-cycle first')
        emu.reason = None
        if not self._started:
            self._started = True
            res = emu.run(max_instructions=budget, stop='budget')
            if res.reason == 'fault' or emu.error:
                self._halted = True
                raise HarnessError(f'emulator fault: {emu.error} at pc {res.pc:#x}\n{self.uart_text()[-2000:]}')
            return res.reason
        pc = emu._reg_read(UC_ARM_REG_PC)
        cpsr = emu._reg_read(UC_ARM_REG_CPSR)
        start = pc | (1 if cpsr & 0x20 else 0)
        emu._limit = self._instructions() + budget
        obs = emu._native_observer
        if obs is not None:
            # A stop requested from the UART callback leaves the observer's
            # ``stopped`` latch set; configure()+refresh() recompute it from
            # ``emu.reason`` (now None) and republish the new budget.
            obs.configure(emu._limit)
            obs.refresh()
            obs.state.stopped = 0
        try:
            emu.uc.emu_start(start, 0xFFFFFFFF)
            emu._finish_instruction()
        except Exception as exc:  # unicorn / model errors surface here
            self._halted = True
            raise HarnessError(f'emulator fault while continuing: {exc}\n{self.uart_text()[-2000:]}') from exc
        if emu.error:
            self._halted = True
            raise HarnessError(f'emulator error: {emu.error}')
        return emu.reason or 'instruction-limit'

    def run_until(self, pattern: bytes | str | re.Pattern, *, max_instructions: int = 20_000_000,
                  since: int | None = None) -> str:
        """Run until the UART output (from ``since``) matches ``pattern``.

        A ``bytes`` pattern ending in "> " must terminate the output (prompt);
        any other bytes pattern may appear anywhere. Returns the new output.
        """
        if isinstance(pattern, str):
            pattern = pattern.encode('latin1')
        start = len(self.emu.uart) if since is None else since
        self._stop_from = start
        self._stop_pattern = pattern
        try:
            if self._matches(pattern, start):
                return self.uart_text(start)
            reason = self._continue(max_instructions)
        finally:
            self._stop_pattern = None
        if reason != 'pattern':
            raise HarnessError(f'pattern {pattern!r} not seen within {max_instructions} instructions '
                               f'(reason {reason}); output:\n{self.uart_text(start)[-3000:]}')
        return self.uart_text(start)

    def run_steps(self, max_instructions: int) -> str:
        """Run for a bounded number of instructions and return new output."""
        start = len(self.emu.uart)
        self._continue(max_instructions)
        return self.uart_text(start)

    def command(self, line: str, prompt: bytes = b'> ', *, max_instructions: int = 20_000_000) -> str:
        """Type ``line`` at a prompt and return the response up to the next prompt."""
        start = len(self.emu.uart)
        self.send(line + '\r')
        out = self.run_until(prompt, max_instructions=max_instructions, since=start)
        self.transcript.append((line, out))
        return out

    def xmodem_send(self, data: bytes, *, block_size: int = 1024, max_instructions: int = 60_000_000) -> XmodemSender:
        """Serve an XMODEM transfer to a receiver that is already waiting (sent its C)."""
        sender = XmodemSender(data, block_size=block_size)
        if self.emu.uart.endswith(b'C'):
            # The receiver already asked for CRC mode; answer that request
            # instead of waiting for its next poll.
            sender.feed(ord('C'))
            self.emu.board.queue_rx(sender.pending())
        self._sender = sender
        try:
            reason = self._continue(max_instructions)
        finally:
            self._sender = None
        if reason != 'xmodem-done' or sender.error:
            raise HarnessError(f'xmodem transfer failed: reason {reason}, error {sender.error}, '
                               f'events {sender.events[-5:]}\n{self.uart_text()[-1500:]}')
        return sender

    # -- flash and power -------------------------------------------------------
    def flash_image(self) -> bytes:
        """Current contents of the (writable) SPI flash."""
        return bytes(self.emu.spi_flash.image)

    def flash_writes(self) -> list[dict]:
        return list(self.emu.spi_flash.writes)

    def dram_image(self) -> bytes:
        """Current contents of the 64 MiB DRAM."""
        return bytes(self.emu.memory._dram_mv)

    def power_cycle(self, *, warm: bool = False, **kwargs) -> 'VirtualPrinter':
        """A new printer booting from whatever is in flash now.

        ``warm=True`` keeps the DRAM contents (a watchdog reset does not clear
        DRAM), which is what the bootloader's crash-loop guard relies on; a cold
        cycle starts with zeroed DRAM like a real power-on with random contents.
        """
        kwargs.setdefault('spi_profile', self.spi_profile)
        kwargs.setdefault('spi_rx_read_shift', self.spi_rx_read_shift)
        if warm:
            kwargs.setdefault('init_dram', self.dram_image())
        return VirtualPrinter(self.flash_image(), native_observer=self.emu._native_observer is not None,
                              echo=self.echo, **kwargs)

    def run_image_from_ram(self, image: bytes, *, prompt: bytes | re.Pattern = re.compile(rb'[A-Za-z]+> $'),
                           block_size: int = 1024) -> str:
        """At the BL prompt: type "run", serve the image, wait for the app's prompt."""
        start = len(self.emu.uart)
        self.send('run\r')
        self.run_until(re.compile(rb'run from RAM only \.\.\.\r\nC'), since=start)
        self.xmodem_send(image, block_size=block_size)
        out = self.run_until(prompt, since=start)
        self.transcript.append(('run', out))
        return out

    def update_over_xmodem(self, image: bytes, *, slot: str | None = None, prompt: bytes = b'app> ',
                           block_size: int = 1024) -> str:
        """Type "update [slot]", serve the transfer, and return the shell output."""
        start = len(self.emu.uart)
        self.send('update' + (f' {slot}' if slot else '') + '\r')
        self.run_until(re.compile(rb'target slot [AB] \.\.\.\r\nC'), since=start)
        self.xmodem_send(image, block_size=block_size)
        out = self.run_until(prompt, since=start)
        self.transcript.append(('update', out))
        return out


def boot_to_app(flash_image: bytes, **kwargs) -> VirtualPrinter:
    """Boot and wait for the application prompt."""
    vp = VirtualPrinter(flash_image, **kwargs)
    vp.run_until(b'app> ')
    return vp


def boot_to_bootloader(flash_image: bytes, **kwargs) -> VirtualPrinter:
    """Boot, interrupt autoboot with a key press, and wait for the BL prompt."""
    vp = VirtualPrinter(flash_image, **kwargs)
    vp.run_until(b'press any key to stop... ')
    vp.send(b' ')
    vp.run_until(b'BL> ')
    return vp


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description='Run a flash image in the emulator and type commands at it')
    parser.add_argument('image', type=Path, help='16 MiB flash image (build/hpdj_flash.bin)')
    parser.add_argument('--cmd', action='append', default=[], help='command to type at the app prompt (repeatable)')
    parser.add_argument('--bl', action='store_true', help='stop autoboot and type the commands at the BL prompt')
    parser.add_argument('--send', type=Path, help='image to send when a command starts an XMODEM transfer')
    parser.add_argument('--quiet', action='store_true', help='do not echo UART output while running')
    parser.add_argument('--interactive', action='store_true', help='attach stdin to the UART after the commands')
    args = parser.parse_args(argv)

    flash = args.image.read_bytes()
    t0 = time.time()
    if args.bl:
        vp = boot_to_bootloader(flash, echo=not args.quiet)
        prompt: bytes | re.Pattern = b'BL> '
    else:
        vp = boot_to_app(flash, echo=not args.quiet)
        prompt = b'app> '
    any_prompt = re.compile(rb'(?:app|BL)> $')   # "bl" and "boot" switch shells
    for cmd in args.cmd:
        start = len(vp.emu.uart)
        vp.send(cmd + '\r')
        if cmd.split()[0] in ('update', 'run') and args.send:
            vp.run_until(re.compile(rb'\.\.\.\r\nC'), since=start)
            vp.xmodem_send(args.send.read_bytes())
        if cmd.split()[0] == 'reboot':
            vp.run_until(REBOOT_MARKER, since=start)
            break
        vp.run_until(any_prompt, since=start)
    if args.quiet:
        print(vp.uart_text())
    print(f'\n[harness] {vp._instructions()} instructions in {time.time() - t0:.1f}s, '
          f'{len(vp.flash_writes())} flash writes', file=sys.stderr)
    if args.interactive:
        from .emu_console import interactive_loop
        interactive_loop(vp, prompt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
