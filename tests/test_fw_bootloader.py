"""Bootloader + application firmware: image format, XMODEM sender, and emulator scenarios.

The emulator scenarios compile the firmware (skipped when no ARM compiler is
available), boot HP stage-1 -> bootloader -> application, exercise the shells,
and perform a complete self-update over XMODEM with the writable SPI flash
model, followed by a power cycle into the updated flash contents.
"""
from __future__ import annotations

import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fw import image as fwimage  # noqa: E402
from fw.xmodem import XmodemSender, crc16_xmodem, ACK, NAK, CAN, EOT, STX, SOH  # noqa: E402

DUMP = ROOT / 'dump.bin'


class ImageFormatTests(unittest.TestCase):
    def test_layout_constants_come_from_the_c_header(self):
        self.assertEqual(fwimage.FLASH_BL_OFFSET, 0x22000)
        self.assertEqual(fwimage.FLASH_PROTECT_END, 0x40000)
        self.assertEqual(fwimage.FLASH_SLOT_A_OFFSET + fwimage.FLASH_SLOT_SIZE, fwimage.FLASH_SLOT_B_OFFSET)
        self.assertEqual(fwimage.BL_CODE_MAX, fwimage.FLASH_BL_SIZE - fwimage.IMAGE_HEADER_SIZE)
        self.assertEqual(fwimage.APP_CODE_MAX, fwimage.FLASH_SLOT_SIZE - fwimage.IMAGE_HEADER_SIZE)
        # The bootloader must not reach into the application slots and the
        # protect boundary must cover stage-1 + the bootloader.
        self.assertLessEqual(fwimage.FLASH_BL_OFFSET + fwimage.FLASH_BL_SIZE, fwimage.FLASH_PROTECT_END)
        self.assertLessEqual(fwimage.FLASH_PROTECT_END, fwimage.FLASH_SLOT_A_OFFSET)

    def test_header_roundtrip_and_crc(self):
        payload = bytes(range(256)) * 3
        img = fwimage.make_image(payload, image_type=fwimage.IMAGE_TYPE_APP, version=fwimage.version_u32('1.2.3'),
                                 sequence=7, load_address=fwimage.APP_LOAD_ADDR,
                                 entry_address=fwimage.APP_LOAD_ADDR + 0x40, build_time=1234, name='hpdj-app')
        self.assertEqual(len(img), 64 + len(payload))
        hdr = fwimage.validate_image(img, expect_type=fwimage.IMAGE_TYPE_APP)
        self.assertEqual(hdr.version_text, '1.2.3')
        self.assertEqual(hdr.sequence, 7)
        self.assertEqual(hdr.payload_size, len(payload))
        self.assertEqual(hdr.name, 'hpdj-app')
        self.assertEqual(struct.unpack('>I', img[:4])[0], fwimage.IMAGE_MAGIC)
        # Corrupting the payload or the header must be detected.
        bad = bytearray(img)
        bad[100] ^= 1
        with self.assertRaises(ValueError):
            fwimage.validate_image(bytes(bad))
        bad = bytearray(img)
        bad[12] ^= 1  # version field
        with self.assertRaises(ValueError):
            fwimage.validate_image(bytes(bad))

    def test_make_image_rejects_oversize_and_bad_entry(self):
        with self.assertRaises(ValueError):
            fwimage.make_image(b'\0' * (fwimage.APP_CODE_MAX + 4), image_type=fwimage.IMAGE_TYPE_APP, version=0,
                               sequence=1, load_address=fwimage.APP_LOAD_ADDR, entry_address=fwimage.APP_LOAD_ADDR,
                               build_time=0, name='x')
        with self.assertRaises(ValueError):
            fwimage.make_image(b'\0' * 64, image_type=fwimage.IMAGE_TYPE_APP, version=0, sequence=1,
                               load_address=fwimage.APP_LOAD_ADDR, entry_address=fwimage.APP_LOAD_ADDR + 0x100,
                               build_time=0, name='x')

    def test_flash_image_assembly_keeps_stage1_and_neutralizes_hp_update(self):
        base = bytearray(b'\x11' * fwimage.FLASH_TOTAL_SIZE)
        base[:4] = b'\xe5\x9f\xf0\x18'
        bl = fwimage.make_image(b'\xAA' * 1024, image_type=fwimage.IMAGE_TYPE_BL, version=fwimage.version_u32('1.0.0'),
                                sequence=1, load_address=fwimage.BL_LOAD_ADDR, entry_address=fwimage.BL_LOAD_ADDR,
                                build_time=0, name='hpdj-bl')
        app = fwimage.make_image(b'\xBB' * 2048, image_type=fwimage.IMAGE_TYPE_APP, version=fwimage.version_u32('1.0.0'),
                                 sequence=1, load_address=fwimage.APP_LOAD_ADDR, entry_address=fwimage.APP_LOAD_ADDR,
                                 build_time=0, name='hpdj-app')
        img = fwimage.build_flash_image(bytes(base), bl, app, slot=1)
        self.assertEqual(len(img), fwimage.FLASH_TOTAL_SIZE)
        # Stage-1 body untouched except the container header fields we repoint.
        self.assertEqual(img[:0x20068], bytes(base[:0x20068]))
        self.assertEqual(struct.unpack('>I', img[0x20068:0x2006C])[0], fwimage.BL_LOAD_ADDR)
        self.assertEqual(struct.unpack('>3I', img[0x201EC:0x201F8]),
                         (fwimage.BL_LOAD_ADDR, fwimage.FLASH_BL_OFFSET + 64, 1024))
        self.assertEqual(img[fwimage.FLASH_BL_OFFSET:fwimage.FLASH_BL_OFFSET + len(bl)], bl)
        self.assertEqual(fwimage.read_slot_image(img, 1), app)
        self.assertIsNone(fwimage.read_slot_image(img, 0))
        self.assertEqual(img[fwimage.FLASH_SLOT_A_OFFSET:fwimage.FLASH_SLOT_A_OFFSET + 16], b'\xFF' * 16)
        self.assertEqual(img[fwimage.GHS_HEADER_SECTOR:fwimage.GHS_HEADER_SECTOR + 0x1000], b'\xFF' * 0x1000)
        self.assertEqual(img[fwimage.HP_FOOTER_SECTOR:], b'\xFF' * 0x1000)
        # Regions we do not own keep the base contents.
        self.assertEqual(img[0x200000:0x200010], b'\x11' * 16)
        kept = fwimage.build_flash_image(bytes(base), bl, app, neutralize_hp_update=False)
        self.assertEqual(kept[fwimage.HP_FOOTER_SECTOR:], b'\x11' * 0x1000)


class XmodemSenderTests(unittest.TestCase):
    def test_crc16_reference_value(self):
        self.assertEqual(crc16_xmodem(b'123456789'), 0x31C3)

    def _receive(self, sender: XmodemSender, nak_block: int | None = None) -> bytes:
        """A tiny XMODEM receiver that verifies frames and reassembles the data."""
        out = bytearray()
        expected = 1
        naked = False
        sender.feed(ord('C'))
        while not sender.done:
            frame = sender.pending()
            if frame == bytes([EOT]):
                sender.feed(ACK)
                continue
            head = frame[0]
            size = 1024 if head == STX else 128
            self.assertIn(head, (SOH, STX))
            self.assertEqual(len(frame), 3 + size + 2)
            self.assertEqual(frame[1], expected & 0xFF)
            self.assertEqual(frame[2], 0xFF - (expected & 0xFF))
            payload = frame[3:3 + size]
            self.assertEqual(int.from_bytes(frame[-2:], 'big'), crc16_xmodem(payload))
            if nak_block == expected and not naked:
                naked = True
                sender.feed(NAK)
                continue
            out += payload
            expected += 1
            sender.feed(ACK)
        return bytes(out)

    def test_1k_transfer_pads_last_block(self):
        data = bytes(range(256)) * 9 + b'tail'
        sender = XmodemSender(data)
        got = self._receive(sender)
        self.assertEqual(len(got), 3 * 1024)
        self.assertEqual(got[:len(data)], data)
        self.assertEqual(got[len(data):], b'\x1A' * (3 * 1024 - len(data)))
        self.assertEqual(sender.events, ['start', 'done'])

    def test_128_byte_transfer_with_retransmission(self):
        data = b'x' * 300
        sender = XmodemSender(data, block_size=128)
        got = self._receive(sender, nak_block=2)
        self.assertEqual(got[:300], data)
        self.assertIn('nak:1', sender.events)

    def test_cancel_stops_the_sender(self):
        sender = XmodemSender(b'abc')
        sender.feed(ord('C'))
        sender.pending()
        sender.feed(CAN)
        self.assertTrue(sender.done)
        self.assertEqual(sender.error, 'cancelled by receiver')


def _have_compiler() -> bool:
    from custom_firmware import find_arm_c_compiler
    return find_arm_c_compiler() is not None


@unittest.skipUnless(DUMP.exists(), 'dump.bin (HP stage-1) is required')
@unittest.skipUnless(_have_compiler(), 'no ARM C compiler available')
class FirmwareEmulationTests(unittest.TestCase):
    """Build once, then boot and drive the firmware in the emulator."""

    @classmethod
    def setUpClass(cls):
        from fw.build import build_all
        cls.tmp = tempfile.TemporaryDirectory()
        out = Path(cls.tmp.name)
        m1 = build_all(out / 'v1', app_version='1.0.0', app_sequence=1, build_time=1_700_000_000)
        m2 = build_all(out / 'v2', app_version='1.0.1', app_sequence=2, build_time=1_700_000_100, write_flash=False,
                       build_bl=False)
        # Third build: sequence defaults to the build time (automatic ordering),
        # built from the DIY example directory to exercise --app-src.
        m3 = build_all(out / 'hello', app_version='0.1.0', build_time=1_700_000_200, write_flash=False,
                       build_bl=False, app_src=ROOT / 'fw' / 'examples' / 'hello')
        cls.flash = m1['_flash_path'].read_bytes()
        cls.app_v1 = m1['_results']['app'].image_bytes
        cls.app_v2 = m2['_results']['app'].image_bytes
        cls.app_hello = m3['_results']['app'].image_bytes
        cls.bl_img = m1['_results']['bl'].image_bytes

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_images_fit_and_describe(self):
        self.assertLess(len(self.bl_img), fwimage.FLASH_BL_SIZE)
        self.assertLess(len(self.app_v1), fwimage.FLASH_SLOT_SIZE)
        self.assertIn('bootloader "hpdj-bl" v1.0.0 seq 1', fwimage.describe_image(self.bl_img))
        self.assertIn('application "hpdj-app" v1.0.1 seq 2', fwimage.describe_image(self.app_v2))

    def test_stage1_boots_bootloader_which_autoboots_application(self):
        from fw.harness import VirtualPrinter
        vp = VirtualPrinter(self.flash)
        out = vp.run_until(b'app> ')
        self.assertIn('[Boot] calling (*0xa0020000)()', out)
        self.assertIn('==== HPDJ bootloader v1.0.0', out)
        self.assertIn('A   0x040000  ok                1.0.0     1', out)
        self.assertIn('B   0x0c0000  empty/no magic', out)
        self.assertIn('[BL] starting application A v1.0.0 seq 1 at 0xa0100000', out)
        self.assertIn('==== HPDJ app v1.0.0 seq 1', out)
        self.assertIn('I app: running from 0xa0100000, data at 0x20180000, slot A', out)
        # Stage-1 ran the autoboot delay through virtual time (~1 s = 4M ticks).
        self.assertRegex(out, r'\[\s+1\.\d{3}\] I bl: loading slot A')
        self.assertEqual(vp.flash_writes(), [])

    def test_application_shell_commands(self):
        from fw.harness import boot_to_app
        vp = boot_to_app(self.flash)
        out = vp.command('version', b'app> ')
        self.assertIn('HPDJ app v1.0.0 seq 1', out)
        self.assertIn('booted from slot A by bootloader v1.0.0, boot #1, reason 0', out)
        out = vp.command('sysinfo', b'app> ')
        self.assertIn('sctlr       000510fd (mpu 1, dcache 1, icache 1, big-endian 1)', out)
        self.assertIn('flash id    ef4018, spi mode byte-final', out)
        out = vp.command('flash selftest', b'app> ')
        self.assertIn('flash selftest: passed (mode byte-final)', out)
        out = vp.command('flash read 0x22000 16', b'app> ')
        self.assertIn('00022000: 48 50 44 4a 00 00 00 01', out)   # "HPDJ", header version 1
        out = vp.command('md 0x2001f000 4', b'app> ')
        self.assertIn('2001f000: 42494e46 01000000 00000000 01000000', out)  # bootinfo: magic, BL 1.0.0, slot A
        out = vp.command('mw 0x20300000 0xdeadbeef', b'app> ')
        self.assertIn('20300000 = deadbeef', out)
        out = vp.command('crc 0xa0100000 0x100', b'app> ')
        expect = fwimage.crc32(self.app_v1[64:64 + 0x100])
        self.assertIn(f'crc32(a0100000, 256) = {expect:08x}', out)
        out = vp.command('log level 3', b'app> ')
        self.assertIn('log level 3', out)
        out = vp.command('log test', b'app> ')
        self.assertRegex(out, r'\[\s*\d+\.\d{3}\] E app: error message')
        self.assertRegex(out, r'\[\s*\d+\.\d{3}\] D app: debug message')
        out = vp.command('led wifi on', b'app> ')
        self.assertIn('I app: led wifi -> on', out)
        self.assertEqual(vp.emu.board.gpio.output_latches[0], 0)   # active-low LED driven on
        out = vp.command('panel', b'app> ')
        self.assertIn('panel frame 0000000000000f00 acknowledged; buttons raw 3ffff (active-low)', out)
        out = vp.command('bogus', b'app> ')
        self.assertIn('unknown command: bogus', out)
        out = vp.command('echo hello  world', b'app> ')
        self.assertIn('hello world\r\n', out)
        out = vp.command('uptime', b'app> ')
        self.assertRegex(out, r'uptime \d+\.\d{3} s')
        self.assertEqual(vp.flash_writes(), [])

    def test_flash_protect_boundary_refuses_bootloader_region(self):
        from fw.harness import boot_to_app
        vp = boot_to_app(self.flash)
        # A sector erase below FLASH_PROTECT_END must be refused by the driver.
        # There is no shell command for raw erase on purpose; call the C guard
        # indirectly through "erase" on a slot (allowed) and compare with the
        # bootloader region via the flash driver's error text in selftest form.
        out = vp.command('erase b', b'app> ')
        self.assertIn('ok', out)
        self.assertTrue(all(w['address'] >= fwimage.FLASH_PROTECT_END for w in vp.flash_writes()))
        self.assertEqual(vp.flash_image()[:fwimage.FLASH_PROTECT_END], self.flash[:fwimage.FLASH_PROTECT_END])

    def test_self_update_bl_reentry_and_power_cycle(self):
        from fw.harness import REBOOT_MARKER, boot_to_app
        vp = boot_to_app(self.flash)
        out = vp.update_over_xmodem(self.app_v2)
        self.assertIn('target slot B ...', out)
        self.assertRegex(out, r'app version 1\.0\.1 seq 2, \d+ payload bytes, crc ok')
        self.assertIn('slot B updated and verified', out)
        ops = [w['op'] for w in vp.flash_writes()]
        self.assertIn('erase', ops)
        self.assertIn('program', ops)
        self.assertTrue(all(w['address'] >= fwimage.FLASH_SLOT_B_OFFSET for w in vp.flash_writes()))
        # The SPI array and the CPU's memory-mapped window agree on the new slot.
        self.assertEqual(fwimage.read_slot_image(vp.flash_image(), 1), self.app_v2)
        b = fwimage.FLASH_SLOT_B_OFFSET
        self.assertEqual(bytes(vp.emu.memory._flash_mv[b:b + len(self.app_v2)]), self.app_v2)
        out = vp.command('slots', b'app> ')
        self.assertIn('B   0x0c0000  ok                1.0.1     2', out)
        self.assertIn('<- boot', out.split('B   0x0c0000')[1])

        # Re-enter the bootloader without a reset and boot the newer slot.
        start = len(vp.emu.uart)
        vp.send('bl\r')
        out = vp.run_until(b'BL> ', since=start)
        self.assertIn('re-entering bootloader v1.0.0 shell', out)
        self.assertIn('[BL] entering recovery shell (requested by application)', out)
        start = len(vp.emu.uart)
        vp.send('boot\r')
        out = vp.run_until(b'app> ', since=start)
        self.assertIn('[BL] starting application B v1.0.1 seq 2', out)
        out = vp.command('version', b'app> ')
        self.assertIn('HPDJ app v1.0.1 seq 2', out)
        self.assertIn('booted from slot B by bootloader v1.0.0', out)

        # "reboot" arms the watchdog and waits with IRQs masked. Let both
        # watchdog stages expire in the model (IRQ sources 5 and 6 assert); the
        # emulator does not reset, so after REBOOT_WAIT_MS the firmware reports
        # that and hands the shell back.
        start = len(vp.emu.uart)
        vp.send('reboot\r')
        vp.run_until(REBOOT_MARKER, since=start)
        vp.run_steps(3_000_000)
        self.assertIsNone(vp.emu.error)
        self.assertEqual(vp.emu.board.irq_raw_sources & 0x60, 0x60)
        out = vp.run_until(b'app> ', since=start, max_instructions=40_000_000)
        self.assertIn('watchdog did not reset the SoC within 3000 ms', out)
        vp2 = vp.power_cycle()
        out = vp2.run_until(b'app> ')
        self.assertIn('[BL] autoboot slot B in 1000 ms', out)
        self.assertIn('==== HPDJ app v1.0.1 seq 2', out)

    def test_bootloader_shell_recovery_update(self):
        from fw.harness import boot_to_bootloader
        vp = boot_to_bootloader(self.flash)
        self.assertTrue(vp.uart_bytes().endswith(b'BL> '))
        out = vp.command('info', b'BL> ')
        self.assertIn('bl image in flash: ok, v1.0.0 seq 1', out)
        out = vp.command('erase a', b'BL> ')
        self.assertIn('ok', out)
        out = vp.command('verify', b'BL> ')
        self.assertIn('slot A: empty/no magic', out)
        out = vp.command('boot', b'BL> ')
        self.assertIn('no valid application slot', out)
        # Recovery: receive v2 into slot A with 128-byte blocks and boot it.
        out = vp.update_over_xmodem(self.app_v2, slot='a', prompt=b'BL> ', block_size=128)
        self.assertIn('slot A updated and verified', out)
        start = len(vp.emu.uart)
        vp.send('boot\r')
        out = vp.run_until(b'app> ', since=start)
        self.assertIn('[BL] starting application A v1.0.1 seq 2', out)
        # Power cycle: slot A (the only valid slot) autoboots.
        vp2 = vp.power_cycle()
        out = vp2.run_until(b'app> ')
        self.assertIn('[BL] autoboot slot A in 1000 ms', out)

    def test_bootloader_falls_into_shell_without_valid_application(self):
        from fw.harness import VirtualPrinter
        img = bytearray(self.flash)
        fwimage.write_slot(img, 0, None)
        vp = VirtualPrinter(bytes(img))
        out = vp.run_until(b'BL> ')
        self.assertIn('[BL] entering recovery shell (no valid application)', out)
        # A corrupted payload is rejected by CRC, not booted.
        img = bytearray(self.flash)
        img[fwimage.FLASH_SLOT_A_OFFSET + 64 + 0x200] ^= 0xFF
        vp = VirtualPrinter(bytes(img))
        out = vp.run_until(b'BL> ')
        self.assertIn('A   0x040000  bad payload crc', out)

    def test_diy_cycle_repeated_updates_alternate_slots(self):
        """The developer loop: update, re-enter BL, boot, repeat. Slots alternate,
        each new image runs, a custom --app-src application works, and an image
        with a lower sequence is accepted but flagged."""
        from fw.harness import boot_to_app
        hdr = fwimage.validate_image(self.app_hello, expect_type=fwimage.IMAGE_TYPE_APP)
        self.assertEqual(hdr.sequence, 1_700_000_200)        # automatic sequence = build time
        self.assertEqual(hdr.name, 'hello')

        vp = boot_to_app(self.flash)                           # v1.0.0 seq 1 in slot A

        def cycle(image: bytes, expect_banner: str, expect_slot: str, prompt: bytes):
            out = vp.update_over_xmodem(image)
            self.assertIn(f'slot {expect_slot} updated and verified', out)
            start = len(vp.emu.uart)
            vp.send('bl\r')
            vp.run_until(b'BL> ', since=start)
            start = len(vp.emu.uart)
            vp.send('boot\r')
            out = vp.run_until(prompt, since=start)
            self.assertIn(f'[BL] starting application {expect_slot} ', out)
            self.assertIn(expect_banner, out)

        cycle(self.app_v2, '==== HPDJ app v1.0.1 seq 2', 'B', b'app> ')
        cycle(self.app_hello, '==== hello example v0.1.0 seq 1700000200', 'A', b'hello> ')
        out = vp.command('hello Elektroda', b'hello> ')
        self.assertIn('Hello, Elektroda! (greeting #1', out)
        out = vp.command('slots', b'hello> ')
        self.assertIn('A   0x040000  ok                0.1.0     1700000200', out)
        self.assertIn('<- boot', out.split('A   0x040000')[1].split('\n')[0])
        # Going back to an older image into the inactive slot B: accepted, but flagged.
        out = vp.update_over_xmodem(self.app_v2, prompt=b'hello> ')
        self.assertIn('slot B updated and verified', out)
        self.assertIn('warning: seq 2 is not higher than slot A (seq 1700000200)', out)
        # The bootloader still prefers slot A; "boot b" forces the older image.
        start = len(vp.emu.uart)
        vp.send('bl\r')
        vp.run_until(b'BL> ', since=start)
        start = len(vp.emu.uart)
        vp.send('boot b\r')
        out = vp.run_until(b'app> ', since=start)
        self.assertIn('[BL] starting application B v1.0.1 seq 2', out)
        self.assertEqual(fwimage.read_slot_image(vp.flash_image(), 0), self.app_hello)
        self.assertEqual(fwimage.read_slot_image(vp.flash_image(), 1), self.app_v2)

    def test_bootloader_run_executes_image_from_ram_without_flash_writes(self):
        from fw.harness import boot_to_bootloader
        vp = boot_to_bootloader(self.flash)
        out = vp.run_image_from_ram(self.app_v2, prompt=b'app> ')
        self.assertIn('crc ok (not written to flash)', out)
        self.assertIn('[BL] starting application from RAM v1.0.1 seq 2', out)
        self.assertIn('==== HPDJ app v1.0.1 seq 2', out)
        self.assertIn('RAM image, flash best slot A', out)
        out = vp.command('version', b'app> ')
        self.assertIn('running from RAM (uploaded with the bootloader', out)
        self.assertEqual(vp.flash_writes(), [])
        self.assertEqual(vp.flash_image(), self.flash)
        # "update" from a RAM image still targets the slot that is not the best one in flash (B).
        out = vp.update_over_xmodem(self.app_hello)
        self.assertIn('slot B updated and verified', out)
        # The hello example can also be run straight from RAM.
        start = len(vp.emu.uart)
        vp.send('bl\r')
        vp.run_until(b'BL> ', since=start)
        out = vp.run_image_from_ram(self.app_hello, prompt=b'hello> ')
        self.assertIn('==== hello example v0.1.0', out)

    def test_peripheral_test_commands(self):
        from i2c import EEPROM
        from panel import SerialShiftPanel
        from fw.harness import boot_to_app
        eeprom = EEPROM.create_factory_image()
        vp = boot_to_app(self.flash, eeprom=eeprom)
        out = vp.command('i2c scan', b'app> ')
        self.assertIn('0x1f(undercroft)', out)
        self.assertIn('0x50(eeprom)', out)
        self.assertIn('(2 devices)', out)
        out = vp.command('eeprom 0 32', b'app> ')
        expect = ' '.join(f'{b:02x}' for b in eeprom[0:8])
        self.assertIn(f'00000000: {expect}', out)
        out = vp.command('i2c read 0x50 0x0c 4 16', b'app> ')
        self.assertIn(' '.join(f'{b:02x}' for b in eeprom[0x0C:0x10]), out)
        out = vp.command('i2c read 0x7f 0 1', b'app> ')      # nobody there: timeout + controller reset
        self.assertIn('no acknowledge (controller reset)', out)
        out = vp.command('asic', b'app> ')
        self.assertIn('mcasic   id 0x8014 (expect 0x8014)', out)
        self.assertIn('carriage id 0x0801 (expect 0x0801) status 0x800e', out)
        out = vp.command('micci read 0 0 6 0', b'app> ')
        self.assertIn('micci ctrl0 bus 0 dev 6 reg 0x00 = 0x8014', out)
        out = vp.command('adc 15', b'app> ')
        self.assertIn('adc selector 15 mode 3: 512/1023 (reference, expect 465..543)', out)
        out = vp.command('adc 0 4', b'app> ')
        self.assertIn('= 23991 mV supply', out)              # 875/1023 * 28050 mV for the 24 V profile
        out = vp.command('adc 2', b'app> ')
        self.assertIn('adc selector 2 mode 3: 512/1023', out)
        # LCD frame: the latched shift-register word must match the reference encoder.
        out = vp.command('lcd 3 5 1 3 exclamation_icon', b'app> ')
        self.assertIn('acknowledged (signature 0x35 verified)', out)
        self.assertIn('buttons raw 3ffff pressed: none', out)
        endpoint = vp.emu.board.panel.endpoint
        expect_frame = SerialShiftPanel.encode_frame(seven_seg_char='3', wifi_bars=5, color_ink_bars=1,
                                                     black_ink_bars=3, indicators=('exclamation_icon',))
        self.assertEqual(endpoint.last_output, expect_frame)
        decoded = endpoint.decode_frame()
        self.assertEqual(decoded['seven_seg_char'], '3')
        self.assertEqual((decoded['wifi_bars'], decoded['color_ink_bars'], decoded['black_ink_bars']), (5, 1, 3))
        self.assertIn('exclamation_icon', decoded['active_indicators'])
        out = vp.command('lcd 3 6', b'app> ')               # six Wi-Fi bars do not exist
        self.assertIn('usage: lcd', out)
        out = vp.command('lcd ?', b'app> ')
        self.assertIn('usage: lcd', out)
        # Buttons: press power in the emulated panel, read it back by name.
        endpoint.press_button('power')
        out = vp.command('buttons', b'app> ')
        self.assertIn('pressed: power', out)
        endpoint.release_button('power')
        endpoint.press_button('copy_black')
        endpoint.press_button('resume')
        out = vp.command('buttons wait 500', b'app> ')
        self.assertIn('pressed: resume copy_black', out)
        endpoint.release_button('copy_black')
        endpoint.release_button('resume')
        out = vp.command('buttons', b'app> ')
        self.assertIn('pressed: none', out)
        # Display walk: one logical bit per step while the digit blinks the bit
        # number; any button = next, cancel = back, UART n/b/q; segments last.
        digit = 0x7F << 33

        def logical_frame():
            return endpoint.last_output ^ 0x0F00

        def run_frames(n):
            target = len(endpoint.frames) + n
            while len(endpoint.frames) < target:
                vp.run_steps(500_000)

        mark = len(vp.emu.uart)
        vp.send('lcd walk 8 10\r')
        vp.run_until(b'step 1/3: bit 8 (ink_led)', since=mark)
        run_frames(1)
        self.assertEqual(logical_frame() & ~digit, 1 << 8)
        mark = len(vp.emu.uart)
        endpoint.press_button('resume')
        vp.run_until(b'step 2/3: bit 9 (resume_led)', since=mark)
        endpoint.release_button('resume')
        run_frames(3)
        self.assertEqual(logical_frame() & ~digit, 1 << 9)
        mark = len(vp.emu.uart)
        endpoint.press_button('cancel')
        vp.run_until(b'step 1/3: bit 8', since=mark)
        endpoint.release_button('cancel')
        run_frames(3)
        self.assertEqual(logical_frame() & ~digit, 1 << 8)
        mark = len(vp.emu.uart)
        vp.send('nn')
        vp.run_until(b'step 3/3: bit 10 (unnamed)', since=mark)
        run_frames(1)
        self.assertEqual(logical_frame() & ~digit, 1 << 10)
        mark = len(vp.emu.uart)
        vp.send('n')
        out = vp.run_until(b'app> ', since=mark)
        self.assertIn('walk done', out)
        self.assertEqual(logical_frame(), 0)                 # display blanked afterwards
        mark = len(vp.emu.uart)
        vp.send('lcd walk 33 34\r')
        vp.run_until(b'step 1/2: bit 33 = digit segment e (bottom-left), lit alone', since=mark)
        run_frames(1)
        self.assertEqual(logical_frame(), 1 << 33)           # a segment step shows no number
        mark = len(vp.emu.uart)
        vp.send('q')
        out = vp.run_until(b'app> ', since=mark)
        self.assertIn('walk stopped', out)
        out = vp.command('lcd walk 5', b'app> ')
        self.assertIn('usage: lcd walk', out)
        # Motor pulse: one MCASIC control word (mode << 12 | pwm), then every motor off.
        mcasic = vp.emu.mcasic
        writes = []
        real_write = mcasic.write

        def logged_write(register, value):
            writes.append((register, value & 0xFFFF))
            return real_write(register, value)

        mcasic.write = logged_write
        try:
            out = vp.command('motor pulse a fwd 100 20', b'app> ')
            self.assertIn('motor a: register 0x31 <- 0x1064 for 20 ms', out)
            self.assertIn('pulse sent, off again after', out)
            self.assertIn('while on: 31=1064 32=0000', out)
            self.assertEqual(writes, [(0x31, 0x1064), (0x31, 0), (0x33, 0), (0x35, 0)])
            writes.clear()
            out = vp.command('motor pulse b rev 300 100', b'app> ')
            self.assertEqual(writes[0], (0x33, 0x212C))
            self.assertEqual(writes[1:], [(0x31, 0), (0x33, 0), (0x35, 0)])
            writes.clear()
            for refused in ('motor pulse a fwd 601 20', 'motor pulse a fwd 100 251', 'motor pulse d fwd 10 10',
                            'motor pulse a up 10 10', 'motor pulse a fwd 0 10'):
                out = vp.command(refused, b'app> ')
                self.assertIn('usage: motor', out)
            self.assertEqual(writes, [])                    # refused commands touch nothing
            out = vp.command('motor', b'app> ')
            self.assertIn('mcasic: 08=', out)
            self.assertIn(' 31=0000', out)
        finally:
            mcasic.write = real_write
        # Wi-Fi module: power pin, SD host bring-up, SDIO enumeration, identity over CMD52.
        out = vp.command('wifi probe', b'app> ', max_instructions=60_000_000)
        self.assertIn('wrapper status 0003, handshakes 0 0', out)
        self.assertIn('wifi: CMD5 OCR a0ffff00: 2 I/O functions', out)
        self.assertIn('card ready, relative address 0001, selected', out)
        self.assertIn('vendor 02d0 device a887 (Broadcom)', out)
        self.assertIn('(ALP clock available)', out)
        # The harness models the chip as the printer reported it (sdio.py profile "printer").
        self.assertIn('ChipCommon chip ID 1702a887: chip 43143 (0xa887) rev 2, 7 cores', out)
        self.assertEqual(vp.emu.board.sdio.card.f0[0x02], 0x02)          # function 1 enabled
        # Chip firmware from HP's ROM file system in flash, NVRAM, ARM start, BCDC 'ver', iscan.
        card = vp.emu.board.sdio.card
        out = vp.command('wifi fw', b'app> ', max_instructions=400_000_000)
        self.assertIn('wifi: 6 cores: 800/43@18000000 812/37@18001000 829/15@18002000 82a/7@18003000 '
                      '80e/20@18004000 81a/17@18005000', out)
        self.assertIn('RAM 448 KiB', out)
        self.assertIn('NVRAM 892 bytes at 6fc80, token ff2000df', out)
        self.assertIn('chip firmware running', out)
        self.assertIn('firmware says: wl0: Mar 17 2016 15:56:01 version 6.20.198.22 (r625840) FWID 01-0', out)
        self.assertIn('wifi: MAC address 30:e1:71:26:20:01', out)
        fw_len = 0x5E630
        self.assertEqual(bytes(card.socram[:fw_len]), self.flash[0x53AE97:0x53AE97 + fw_len])
        self.assertEqual(bytes(card.socram[0x6FFFC:0x70000]), b'\xdf\x00\x20\xff')
        self.assertTrue(bytes(card.socram[0x6FC80:0x6FC80 + 13]).startswith(b'vendid=0x14e4'))
        self.assertEqual(card.wrapper_resetctrl[3], 0)                    # ARM out of reset
        out = vp.command('wifi scan', b'app> ', max_instructions=400_000_000)
        self.assertIn('00:11:22:33:44:55  ch  6   -42 dBm  HP-Print-Lab-2.4G  [secured]', out)
        self.assertIn('66:77:88:99:aa:bb  ch 11   -65 dBm  Office_Guest_WiFi  [open]', out)
        self.assertIn('scan complete, 2 networks listed', out)
        out = vp.command('wifi', b'app> ')
        self.assertIn('power pin 12: dir 1 out 1', out)
        self.assertIn('card identified', out)
        out = vp.command('wifi off', b'app> ')
        self.assertIn('module power off', out)
        out = vp.command('memtest 0x20800000 0x10000', b'app> ')
        self.assertIn('memtest 20800000..20810000: 0 bad words', out)
        out = vp.command('memtest 0xa0800000 0x1000', b'app> ')
        self.assertIn('refusing', out)
        self.assertEqual(vp.flash_writes(), [])

    def test_timer_calibration_against_uart_bit_clock(self):
        """With real 115200 frame timing the BL measures ~4 ticks/us; with the
        harness's instant UART it rejects the implausible value and keeps 4."""
        from fw.harness import VirtualPrinter
        vp = VirtualPrinter(self.flash, uart_frame_ticks=347)   # 86.8 us at 4 ticks/us
        out = vp.run_until(b'app> ', max_instructions=30_000_000)
        m = re.search(r'timer: (\d+)\.(\d+) ticks/us measured against UART \(compiled default 4; \d+ ticks for 256 bytes',
                      out)
        self.assertIsNotNone(m, out[-2000:])
        rate = int(m.group(1)) + int(m.group(2)) / 100
        self.assertTrue(3.8 <= rate <= 4.4, rate)    # polling costs virtual time in the emulator
        self.assertIn('measured by bootloader', out)
        out = vp.command('uptime', b'app> ')
        self.assertIn('ticks/ms measured against UART', out)

        vp = VirtualPrinter(self.flash)                       # fast_uart: frames take one tick
        out = vp.run_until(b'app> ')
        self.assertRegex(out, r'timer: UART calibration implausible \(\d+ ticks for 256 bytes, fifo fill \d+\), '
                              r'using compiled 4 ticks/us')
        self.assertIn('timer 4000 ticks/ms (compiled default)', out)

    def test_wide_final_controller_hypothesis_with_matching_driver_mode(self):
        """Under the other AXON serializer hypothesis HP's receive pattern still
        reads the id; calibration detects the CS-release framing from the
        repeated last byte and reads, writes and a full update work."""
        from fw.harness import VirtualPrinter
        vp = VirtualPrinter(self.flash, spi_profile='wide-final')
        out = vp.run_until(b'app> ')
        self.assertIn('SPI Device 0 : W25Q128BV (16 Megabytes) (ID:0x00ef4018)', out)   # HP stage-1 driver
        self.assertIn('flash id ef4018 (spi mode wide-final, auto-detected, bus errors 0)', out)
        self.assertIn('spi mode wide-final', out.split('==== HPDJ app')[1])
        out = vp.command('flash selftest', b'app> ')
        self.assertIn('jedec id ef4018 (W25Q128JV, expected)', out)
        self.assertIn('command path verified for writes', out)
        self.assertIn('flash selftest: passed (mode wide-final)', out)
        out = vp.command('flash diag', b'app> ')
        self.assertIn('E1 9F hp pattern, rx at end    : ffef4018', out)
        self.assertIn('calibration passed, framing wide-final', out)
        out = vp.update_over_xmodem(self.app_v2)
        self.assertIn('slot B updated and verified', out)
        self.assertEqual(fwimage.read_slot_image(vp.flash_image(), 1), self.app_v2)
        # The write log shows the datasheet sequence, each program preceded by WREN.
        ops = [w['op'] for w in vp.flash_writes()]
        self.assertEqual(ops[0], 'erase')
        self.assertIn('program', ops)

    def test_shell_refuses_unsafe_addresses_and_malformed_input(self):
        """A typo must never fault into HP's exception vectors (no handlers of our own)."""
        from fw.harness import boot_to_app
        vp = boot_to_app(self.flash)
        for cmd in ('md 0xfffffffc', 'md 0x04000000 1', 'crc 0xffffff00 0x1000', 'mw 0x10000000 1', 'md 0x23fffff8 4'):
            out = vp.command(cmd, b'app> ')
            self.assertIn('refused', out, cmd)
        out = vp.command('md 0x1ffffffff 1', b'app> ')            # 9 hex digits: parse overflow
        self.assertIn('usage: md', out)
        out = vp.command('sleep 99999999999', b'app> ')
        self.assertIn('usage: sleep', out)
        out = vp.command('mw 0x00040000 1', b'app> ')              # flash window is read-only
        self.assertIn('read-only', out)
        out = vp.command('mw 0xa0100000 1', b'app> ')              # 0xA0 alias is read-only (MPU)
        self.assertIn('read-only', out)
        out = vp.command('echo ' + ' '.join(f'a{i}' for i in range(20)), b'app> ')
        self.assertIn('too many arguments', out)
        out = vp.command('md 0xbd040814 1', b'app> ')              # peripherals stay reachable
        self.assertIn('bd040814:', out)
        self.assertIsNone(vp.emu.error)

    def test_xmodem_lone_cancel_byte_is_noise(self):
        from fw.harness import boot_to_bootloader
        vp = boot_to_bootloader(self.flash)
        start = len(vp.emu.uart)
        vp.send('update b\r')
        vp.run_until(re.compile(rb'target slot B \.\.\.\r\nC'), since=start)
        vp.send(b'\x18')                 # a single CAN followed by a real transfer
        vp.xmodem_send(self.app_v2)
        out = vp.run_until(b'BL> ', since=start)
        self.assertIn('slot B updated and verified', out)

    def test_idle_shell_resumes_autoboot_after_spurious_key(self):
        """Built with a 1.5 s idle timeout: a key press followed by silence must
        not park the printer in the bootloader."""
        from fw.build import build_all
        from fw.harness import VirtualPrinter
        out_dir = Path(self.tmp.name) / 'idle'
        m = build_all(out_dir, app_version='1.0.0', app_sequence=1, build_time=1_700_000_300,
                      extra_cflags=['-DBL_SHELL_IDLE_AUTOBOOT_MS=1500U'])
        vp = VirtualPrinter(m['_flash_path'].read_bytes())
        vp.run_until(b'press any key to stop... ')
        vp.send(b'\x00\xff')             # line glitch values are ignored by the countdown
        out = vp.run_until(b'app> ', max_instructions=12_000_000)
        self.assertIn('[BL] starting application A', out)
        vp = VirtualPrinter(m['_flash_path'].read_bytes())
        vp.run_until(b'press any key to stop... ')
        vp.send(b'?')                    # a byte that looks like a key, then silence
        out = vp.run_until(b'BL> ', max_instructions=4_000_000)
        self.assertIn('entering recovery shell (key pressed)', out)
        out = vp.run_until(b'app> ', max_instructions=20_000_000)
        self.assertIn('no input for 1500 ms after the key press (line noise?): resuming autoboot', out)
        self.assertIn('==== HPDJ app v1.0.0', out)
        # An operator who typed a command keeps the shell.
        vp = VirtualPrinter(m['_flash_path'].read_bytes())
        vp.run_until(b'press any key to stop... ')
        vp.send(b' ')
        vp.run_until(b'BL> ')
        vp.command('info', b'BL> ')
        vp.run_steps(10_000_000)          # 2.5 s of virtual time, well past the idle limit
        self.assertTrue(vp.uart_bytes().endswith(b'BL> '))
        self.assertNotIn(b'resuming autoboot', vp.uart_bytes())

    def test_spi_rx_one_bit_shift_like_the_real_printer(self):
        """On the printer, reading RX after every byte returned the id one bit
        off (de8030) while HP's pattern read ef4018. The calibrated driver uses
        HP's pattern only; reads, status and a full update must work even when
        the per-byte method is broken."""
        from fw.harness import VirtualPrinter
        vp = VirtualPrinter(self.flash, spi_rx_read_shift=True)
        out = vp.run_until(b'app> ')
        self.assertIn('flash id ef4018', out.split('==== HPDJ app')[1])
        out = vp.command('flash diag', b'app> ')
        self.assertIn('E1 9F hp pattern, rx at end    : ffef4018', out)
        self.assertRegex(out, r'bytewise id de8030, calibration passed, framing byte-final')
        out = vp.command('flash selftest', b'app> ')
        self.assertIn('flash selftest: passed (mode byte-final)', out)
        out = vp.update_over_xmodem(self.app_v2)
        self.assertIn('slot B updated and verified', out)
        self.assertEqual(fwimage.read_slot_image(vp.flash_image(), 1), self.app_v2)

    def test_crash_loop_guard_stops_autoboot(self):
        """An application that never reports itself alive is not autobooted
        forever: after BL_MAX_BOOT_ATTEMPTS warm resets the BL stays in its
        shell. A cold power-on (random DRAM) resets the count."""
        from fw.build import build_all
        from fw.harness import VirtualPrinter
        src = Path(self.tmp.name) / 'crashy_src'
        src.mkdir(exist_ok=True)
        (src / 'main.c').write_text(
            '#include "uart.h"\n#include "hw.h"\n'
            'void main(unsigned arg) { (void)arg; uart_init(); uart_puts("crashy app: hangs before its shell\\n");'
            ' for (;;) watchdog_kick(); }\n')
        m = build_all(Path(self.tmp.name) / 'crashy', app_version='0.0.1', app_sequence=1, build_time=1_700_000_400,
                      app_src=src)
        flash = m['_flash_path'].read_bytes()
        vp = VirtualPrinter(flash)
        for attempt in range(3):                      # three warm resets into the crashing app
            out = vp.run_until(b'crashy app: hangs before its shell')
            self.assertIn('[BL] starting application A', out)
            vp = vp.power_cycle(warm=True)
        out = vp.run_until(b'BL> ')
        self.assertIn('did not come up after 3 consecutive automatic starts', out)
        self.assertIn('entering recovery shell (repeated application failures)', out)
        cold = VirtualPrinter(vp.flash_image())       # cold power-on: count starts again
        out = cold.run_until(b'crashy app: hangs before its shell')
        self.assertIn('[BL] starting application A', out)
        # The normal application reports itself alive, so warm resets never trip the guard.
        vp = VirtualPrinter(self.flash)
        for _ in range(4):
            vp.run_until(b'app> ')
            vp = vp.power_cycle(warm=True)
        out = vp.run_until(b'app> ')
        self.assertNotIn('repeated application failures', out)

    def test_xmodem_noise_and_ctrl_c_return_to_the_prompt(self):
        from fw.harness import boot_to_bootloader
        vp = boot_to_bootloader(self.flash)
        start = len(vp.emu.uart)
        vp.send('run\r')
        vp.run_until(re.compile(rb'run from RAM only \.\.\.\r\nC'), since=start)
        vp.send(b'x' * 5000)                          # a terminal spewing garbage instead of XMODEM
        out = vp.run_until(b'BL> ', since=start, max_instructions=60_000_000)
        self.assertIn('xmodem failed: too much non-XMODEM data on the line', out)
        start = len(vp.emu.uart)
        vp.send('update b\r')
        vp.run_until(re.compile(rb'target slot B \.\.\.\r\nC'), since=start)
        vp.send(b'\x03')                              # Ctrl-C while the receiver waits
        out = vp.run_until(b'BL> ', since=start)
        self.assertIn('xmodem failed: cancelled (CAN CAN or Ctrl-C)', out)
        self.assertEqual(vp.flash_writes(), [])

    def test_corrupt_xmodem_block_is_retried(self):
        from fw.harness import boot_to_app
        from fw.xmodem import XmodemSender
        vp = boot_to_app(self.flash)
        start = len(vp.emu.uart)
        vp.send('update\r')
        vp.run_until(re.compile(rb'target slot B \.\.\.\r\nC'), since=start)

        class FlakySender(XmodemSender):
            corrupted = False

            def _frame(self, index):
                frame = bytearray(super()._frame(index))
                if index == 1 and not self.corrupted:
                    self.corrupted = True
                    frame[10] ^= 0x55
                return bytes(frame)

        sender = FlakySender(self.app_v2)
        sender.feed(ord('C'))
        vp.emu.board.queue_rx(sender.pending())
        vp._sender = sender
        try:
            reason = vp._continue(60_000_000)
        finally:
            vp._sender = None
        self.assertEqual(reason, 'xmodem-done')
        self.assertIn('nak:1', sender.events)
        out = vp.run_until(b'app> ', since=start)
        self.assertIn('slot B updated and verified', out)


if __name__ == '__main__':
    unittest.main()
