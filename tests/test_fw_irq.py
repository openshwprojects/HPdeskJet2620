"""First hardware interrupt (fw/app/irq.c): a timer tick through HP's interrupt
controller in the emulator, and everything put back afterwards."""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_fw_bootloader import DUMP, _have_compiler  # noqa: E402

PROMPT = b'app> '


@unittest.skipUnless(DUMP.exists(), 'dump.bin (HP stage-1) is required')
@unittest.skipUnless(_have_compiler(), 'no ARM C compiler available')
class IrqTickTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from fw.build import build_all
        cls.tmp = tempfile.TemporaryDirectory()
        manifest = build_all(Path(cls.tmp.name) / 'irq', app_version='1.0.0', app_sequence=1,
                             build_time=1_700_000_000)
        cls.flash = manifest['_flash_path'].read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_timer_tick_and_restore(self):
        from fw.harness import boot_to_app
        vp = boot_to_app(self.flash)
        before = vp.command('irq', PROMPT)
        lit = re.search(r'vector 0x18 e59ff018 literal 0x38 ([0-9a-f]{8})', before)
        self.assertIsNotNone(lit, before)                                        # ldr pc,[pc,#0x18]
        literal = lit.group(1)                       # 00000168 in the emulator; whatever it is, it comes back
        self.assertIn('mpu r1 data AP 6', before)
        out = vp.command('irq tick 1000 1', PROMPT, max_instructions=3_000_000_000)
        m = re.search(r'irq: (\d+) ticks in (\d+) ms \(expected ~(\d+)\), spurious (\d+), foreign (\d+)', out)
        self.assertIsNotNone(m, out)
        ticks, ms, expect, spurious, foreign = map(int, m.groups())
        self.assertGreater(ticks, expect * 0.9, out)
        self.assertLess(ticks, expect * 1.1 + 2, out)
        self.assertEqual((spurious, foreign), (0, 0), out)
        self.assertNotIn('FAULT', out)
        # Everything put back: original literal, all sources masked, CPU interrupts off.
        after = out[out.rindex('vector 0x18'):]
        self.assertIn(f'literal 0x38 {literal}', after)
        self.assertIn('intc mask c40..c4c: ffffffff ffffffff ffffffff ffffffff', after)
        self.assertRegex(after, r'cpsr [0-9a-f]{6}d3')                         # SVC, I and F set
        # The shell still works normally afterwards.
        self.assertIn('HPDJ app v1.0.0', vp.command('version', PROMPT))
        self.assertEqual(vp.flash_writes(), [])

    def test_demo_keeps_ticking_while_the_main_loop_is_blind(self):
        from fw.harness import boot_to_app
        vp = boot_to_app(self.flash)
        out = vp.command('irq demo 4', PROMPT, max_instructions=6_000_000_000)
        self.assertIn('main loop busy for 2 s, not polling anything', out)
        busy = re.search(r'back: (\d+) ticks happened meanwhile', out)
        self.assertIsNotNone(busy, out)
        self.assertGreater(int(busy.group(1)), 1900, out)          # the interrupt did not wait for the loop
        m = re.search(r'irq demo: (\d+) ticks in (\d+) ms \(expected ~(\d+)\), spurious 0, foreign 0', out)
        self.assertIsNotNone(m, out)
        ticks, _, expect = map(int, m.groups())
        self.assertGreater(ticks, expect * 0.9, out)
        self.assertNotIn('FAULT', out)
        self.assertIn('intc mask c40..c4c: ffffffff ffffffff ffffffff ffffffff', vp.command('irq', PROMPT))


if __name__ == '__main__':
    unittest.main()
