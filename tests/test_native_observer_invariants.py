"""Small differential programs for observer ordering, not firmware shortcuts."""
import gc
import unittest

from cpu import CONTROL
from emulator import Emulator, FLASH_SIZE, REGISTERS
from unicorn.arm_const import (
    UC_ARM_REG_CPSR, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
    UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6,
    UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9,
)


UART_TX = 0xBD0B3C06
TIMER = 0xBD040814


class _TestImage(bytearray):
    _dirty_end = 0x10000


class NativeObserverInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from native_observer import build_native_observer, NativeObserverError
        try:
            build_native_observer()
        except NativeObserverError as error:
            raise unittest.SkipTest(str(error)) from error
        image = _TestImage(FLASH_SIZE)
        image[:4] = bytes.fromhex('e59ff018')
        image[0x20:0x24] = bytes.fromhex('00002070')
        cls.base_image = image

    @staticmethod
    def _release(emu: Emulator) -> None:
        if emu._native_observer is not None:
            emu._native_observer._release()
            emu._native_observer = None
        if emu.memory is not None:
            emu.memory._release()
            emu.memory.uc = None
            emu.memory = None
        emu.uc._callbacks.clear()

    @classmethod
    def tearDownClass(cls):
        gc.collect()

    def compare(self, program, *, budget=16, stop='budget', timer_step=1,
                configure=None, check=None, extra=None):
        image = self.base_image
        code = bytes.fromhex(program)
        image[0x2070:0x2070 + len(code)] = code
        max_end = max(0x10000, 0x2070 + len(code))
        for address, data in (extra or {}).items():
            image[address:address + len(data)] = data
            if address + len(data) > max_end:
                max_end = address + len(data)
        image._dirty_end = max_end
        try:
            results = []
            for native in (False, True):
                events = []
                emu = Emulator(image, timer_step=timer_step, native_observer=native,
                               on_event=events.append)
                if configure:
                    configure(emu)
                result = emu.run(max_instructions=budget, stop=stop)
                if check:
                    check(emu, result)
                results.append({
                    'result': result,
                    'registers': tuple(emu.uc.reg_read(reg) for reg in REGISTERS),
                    'cpsr': emu.uc.reg_read(UC_ARM_REG_CPSR),
                    'ticks': emu.board.ticks,
                    'events': events,
                    'uart_writes': emu.uart_writes,
                    'pending_uart': emu.board.uart_tx_pending,
                    'recent_pcs': tuple(emu.recent_pcs),
                    'cp15': dict(emu.cp15.values),
                    'maintenance_count': emu.cp15.maintenance_count,
                    'data': bytes(emu.uc.mem_read(0xA0002000, 16)),
                    'flash_unchanged': emu.memory.check_flash(),
                })
                self._release(emu)
                del emu
            self.assertEqual(results[0], results[1])
            return results[0]
        finally:
            image[0x2070:0x2070 + len(code)] = b'\x00' * len(code)
            for address, data in (extra or {}).items():
                image[address:address + len(data)] = b'\x00' * len(data)

    def test_budget_can_end_inside_a_long_arm_block(self):
        # A block longer than both the instruction budget and PC-history ring.
        result = self.compare('e2800001 ' * 80 + 'eafffffe', budget=53)
        self.assertEqual(result['result'].instructions, 53)
        self.assertEqual(result['registers'][0], 52)  # Reset vector also counts.
        self.assertEqual(len(result['recent_pcs']), 32)

    def test_mixed_arm_thumb_instruction_sizes_and_partial_block(self):
        def configure(emu):
            emu.uc.reg_write(UC_ARM_REG_R1, 0x2081)
        result = self.compare('e12fff11', budget=23, configure=configure,
                              extra={0x2080: bytes.fromhex('3001 3002 e7fc')})
        self.assertEqual(result['result'].reason, 'instruction-limit')
        self.assertTrue(result['cpsr'] & 0x20)

    def test_uart_deadline_precedes_next_instruction_and_budget_check(self):
        # Completion occurs exactly at the budget, with a non-unit clock step.
        # It must report the UART milestone rather than count another opcode.
        def configure(emu):
            emu.uc.reg_write(UC_ARM_REG_R0, ord('A'))
            emu.uc.reg_write(UC_ARM_REG_R1, UART_TX)
            emu.board.uart_frame_ticks = 7
        result = self.compare('e1c100b0 ' + 'e2822001 ' * 20 + 'eafffffe',
                              budget=4, stop='first-byte', timer_step=3,
                              configure=configure)
        self.assertEqual(result['result'].uart, b'A')
        self.assertEqual(result['result'].reason, 'first-byte')
        self.assertEqual(result['result'].instructions, 4)
        self.assertEqual(result['ticks'], 12)

    def test_uart_pending_at_budget_is_not_completed_early(self):
        def configure(emu):
            emu.uc.reg_write(UC_ARM_REG_R0, ord('A'))
            emu.uc.reg_write(UC_ARM_REG_R1, UART_TX)
            emu.board.uart_frame_ticks = 20
        result = self.compare('e1c100b0 eafffffe', budget=2, configure=configure)
        self.assertEqual(result['result'].uart, b'')
        self.assertEqual(result['pending_uart'], ord('A'))
        self.assertEqual(result['ticks'], 2)

    def test_fetch_fault_precedes_pending_uart_completion(self):
        # The branch finishes at the UART deadline, but the next fetch fails
        # before a new code hook. Final cleanup completes the pending branch;
        # it must retain the fetch fault rather than substitute a UART stop.
        def configure(emu):
            emu.uc.reg_write(UC_ARM_REG_R0, ord('A'))
            emu.uc.reg_write(UC_ARM_REG_R1, UART_TX)
            emu.uc.reg_write(UC_ARM_REG_R2, 0x10000000)  # Unmapped at reset.
            emu.board.uart_frame_ticks = 2
        result = self.compare('e1c100b0 e12fff12', stop='first-byte',
                              configure=configure)
        self.assertEqual(result['result'].reason, 'fault')
        self.assertEqual(result['result'].instructions, 3)
        self.assertEqual(result['result'].uart, b'A')
        self.assertEqual(result['ticks'], 3)

    def test_cp15_emulation_and_wait_count_once_each(self):
        # MRC processor ID; MCR wait-for-interrupt; unreachable increment.
        result = self.compare('ee100f10 ee070f90 e2822001 eafffffe')
        self.assertEqual(result['result'].reason, 'wait-for-interrupt')
        self.assertEqual(result['result'].instructions, 3)
        self.assertEqual(result['registers'][0], 0x41059461)
        self.assertEqual(result['registers'][2], 0)

    def test_false_condition_user_cp15_does_not_trap_or_change_control(self):
        def configure(emu):
            emu.uc.reg_write(UC_ARM_REG_CPSR, 0x2D0)  # User, Z clear.
        result = self.compare('0e010f10 e2800001 eafffffe', budget=4,
                              configure=configure)
        self.assertIsNone(result['result'].error)
        self.assertEqual(result['registers'][0], 1)
        self.assertEqual(result['cp15'][CONTROL], 0xF8)

    def test_self_modified_ram_block_discovers_new_cp15_instruction(self):
        # Call ADD/BX in RAM once, replace ADD with MCR, and call the same
        # address again. Cached observer metadata must notice the new opcode.
        def configure(emu):
            emu.uc.mem_write(0xA0002000, bytes.fromhex('e2822001 e12fff1e'))
            emu.uc.reg_write(UC_ARM_REG_R0, 0xEE013F10)  # MCR p15,r3,c1.
            emu.uc.reg_write(UC_ARM_REG_R1, 0xA0002000)
            emu.uc.reg_write(UC_ARM_REG_R3, 0x100F8)  # Enable DTCM.
            emu.uc.reg_write(UC_ARM_REG_R5, 0x20002000)  # Same physical RAM.
        result = self.compare('e12fff31 e5850000 e12fff31 eafffffe',
                              budget=12, configure=configure)
        self.assertIsNone(result['result'].error)
        self.assertEqual(result['registers'][2], 1)
        self.assertEqual(result['cp15'][CONTROL], 0x100F8)
        self.assertTrue(any(event['kind'] == 'cp15-write'
                            for event in result['events']))

    def test_alias_write_changes_cpu_instruction_in_previously_executed_block(self):
        def configure(emu):
            emu.uc.mem_write(0xA0002000, bytes.fromhex('e2822001 e12fff1e'))
            emu.uc.reg_write(UC_ARM_REG_R0, 0xE2822007)  # ADD r2,r2,#7.
            emu.uc.reg_write(UC_ARM_REG_R1, 0xA0002000)
            emu.uc.reg_write(UC_ARM_REG_R5, 0x20002000)
        result = self.compare('e12fff31 e5850000 e12fff31 eafffffe',
                              budget=12, configure=configure)
        self.assertIsNone(result['result'].error)
        self.assertEqual(result['registers'][2], 8)

    def test_cached_block_checks_changed_cpu_mode_before_fetch(self):
        # Execute MSR/ADD/BX once in SVC, then execute the same MSR with a User
        # mode value. Its previously cached successor must lose fetch access.
        def configure(emu):
            emu.uc.mem_write(0xA0002000,
                             bytes.fromhex('e121f000 e2822001 e12fff1e'))
            emu.uc.reg_write(UC_ARM_REG_R0, 0xD3)
            emu.uc.reg_write(UC_ARM_REG_R1, 0xA0002000)
            emu.cp15.write((0, 6, 0, 0), 0x3F)
            emu.cp15.write((0, 6, 1, 0), 0xA0002017)
            emu.cp15.write((0, 5, 0, 2), 0x33)
            emu.cp15.write((0, 5, 0, 3), 0x13)
            emu.cp15.write(CONTROL, emu.cp15.control | 1)
        result = self.compare('e12fff31 e3a000d0 e12fff31 eafffffe',
                              budget=16, configure=configure)
        self.assertEqual(result['result'].reason, 'fault')
        self.assertEqual(result['registers'][2], 1)
        self.assertEqual(result['cpsr'] & 0x1F, 0x10)

    def test_denied_mmio_write_has_no_device_side_effect(self):
        def configure(emu):
            emu.uc.reg_write(UC_ARM_REG_R0, ord('A'))
            emu.uc.reg_write(UC_ARM_REG_R1, UART_TX)
            emu.cp15.write((0, 6, 0, 0), 0x0000002F)
            emu.cp15.write((0, 6, 1, 0), 0xBD0B3017)
            emu.cp15.write((0, 5, 0, 2), 0x56)
            emu.cp15.write((0, 5, 0, 3), 0x06)
            emu.cp15.write(CONTROL, emu.cp15.control | 1)
        result = self.compare('e1c100b0 eafffffe', configure=configure)
        self.assertEqual(result['result'].reason, 'fault')
        self.assertEqual(result['uart_writes'], [])
        self.assertIsNone(result['pending_uart'])

    def test_user_permission_override_clears_before_following_mmio_store(self):
        # LDRT reads a user-readable timer region, then a normal privileged
        # STRH writes the UART, which User mode must not be allowed to write.
        def configure(emu):
            for reg, value in ((UC_ARM_REG_R0, ord('A')),
                               (UC_ARM_REG_R1, TIMER), (UC_ARM_REG_R3, UART_TX)):
                emu.uc.reg_write(reg, value)
            emu.cp15.write((0, 6, 0, 0), 0x3F)
            emu.cp15.write((0, 6, 1, 0), 0xBD040017)
            emu.cp15.write((0, 5, 0, 2), 0x31)
            emu.cp15.write((0, 5, 0, 3), 3)
            emu.cp15.write(CONTROL, emu.cp15.control | 1)
        result = self.compare('e4b12000 e1c300b0 eafffffe', budget=5,
                              configure=configure)
        self.assertIsNone(result['result'].error)
        self.assertEqual(len(result['uart_writes']), 1)
        self.assertEqual(result['pending_uart'], ord('A'))

    def test_tcm_load_store_restores_outer_bus_before_next_instruction(self):
        def configure(emu):
            for reg, value in ((UC_ARM_REG_R0, 0xC00F8), (UC_ARM_REG_R2, 0),
                               (UC_ARM_REG_R3, 0xDEADBEEF),
                               (UC_ARM_REG_R5, 0x400F8), (UC_ARM_REG_R6, 0xF8)):
                emu.uc.reg_write(reg, value)
        def check(emu, result):
            self.assertEqual(emu.uc.reg_read(UC_ARM_REG_R7), 0xE59FF018)
            self.assertEqual(emu.uc.reg_read(UC_ARM_REG_R8), 0xDEADBEEF)
            self.assertEqual(emu.uc.reg_read(UC_ARM_REG_R9), 0xE59FF018)
            self.assertEqual(emu.memory._pending_restore, [])
            self.assertIsNone(result.error)
        self.compare('ee010f10 e5823000 e5924000 e1a07004 ee015f10 '
                     'e5928000 ee016f10 e5929000 eafffffe', budget=12,
                     configure=configure, check=check)

    def test_fast_path_without_on_event_matches_reference(self):
        # Exercises the C fast paths when on_event is None:
        # - MCR p15, 0, r0, c7, c10, 4 (drain write buffer: 0xEE070F9A)
        # - Repeated TIMER (0xBD040814) reads
        # - Cached UART_STATUS (0xBD0B3C0E) and UART_RX (0xBD0B3C0C) reads
        image = self.base_image
        code = bytes.fromhex(
            'ee070f9a e5912000 e0877002 e1d340b0 '
            'e0888004 e1d650b0 e0899005 eafffff7'
        )
        image[0x2070:0x2070 + len(code)] = code
        try:
            snapshots = []
            for native in (False, True):
                emu = Emulator(image, timer_step=3, native_observer=native, on_event=None)
                emu.board.write(0xBD0B4C00, 2, 1)
                emu.board.queue_rx(b'XY')
                emu.uc.reg_write(UC_ARM_REG_R1, TIMER)
                emu.uc.reg_write(UC_ARM_REG_R3, 0xBD0B3C0E)
                emu.uc.reg_write(UC_ARM_REG_R6, 0xBD0B3C0C)
                res = emu.run(max_instructions=33, stop='budget')
                snapshots.append({
                    'result': res,
                    'registers': tuple(emu.uc.reg_read(reg) for reg in REGISTERS),
                    'cpsr': emu.uc.reg_read(UC_ARM_REG_CPSR),
                    'ticks': emu.board.ticks,
                    'maintenance_count': emu.cp15.maintenance_count,
                    'uart_rx': bytes(emu.board.uart_rx),
                    'recent_pcs': tuple(emu.recent_pcs),
                })
                self._release(emu)
            self.assertEqual(snapshots[0], snapshots[1])
            self.assertEqual(snapshots[1]['maintenance_count'], 4)
            self.assertEqual(snapshots[1]['uart_rx'], b'')
        finally:
            image[0x2070:0x2070 + len(code)] = b'\x00' * len(code)

    def test_serial_panel_and_timer_reschedule_fast_paths_match_reference(self):
        # Exercises 4 KiB page fetch cache, cross-block CPSR caching,
        # cancelled timer event pruning (0xBD04082E), and PanelPort
        # (0xBD0B1418 read/write -> 0xBD0B1410 read) fast paths when on_event is None.
        image = self.base_image
        code = bytes.fromhex(
            # Loop: write R2 (0x8) to [R1] (0xBD0B1418), read [R1] into R9,
            # write R3 (0xA) to [R1], write R4 (250) to [R5] (0xBD04082E),
            # read [R6] (0xBD0B1410) into R7, accumulate R7+R9 into R8, branch back.
            'e1c120b0 e1d190b0 e1c130b0 e1c540b0 e1d670b0 e0888007 e0888009 eafffff7'
        )
        image[0x2070:0x2070 + len(code)] = code
        try:
            snapshots = []
            for native in (False, True):
                emu = Emulator(image, serial_panel_inputs=0x2A555,
                               native_observer=native, on_event=None)
                # Release active-low panel reset (bit 3 = 1) and arm via GPIO 26 low->high strobe.
                emu.board.write(0xBD0B1418, 2, 0x8)
                emu.board.write(0xBD0B2000 + 26 * 8 + 4, 2, 1)
                emu.board.write(0xBD0B2000 + 26 * 8 + 2, 2, 0)
                emu.board.write(0xBD0B2000 + 26 * 8 + 2, 2, 1)
                emu.uc.reg_write(UC_ARM_REG_R1, 0xBD0B1418)
                emu.uc.reg_write(UC_ARM_REG_R2, 0x8)
                emu.uc.reg_write(UC_ARM_REG_R3, 0xA)
                emu.uc.reg_write(UC_ARM_REG_R4, 250)
                emu.uc.reg_write(UC_ARM_REG_R5, 0xBD04082E)
                emu.uc.reg_write(UC_ARM_REG_R6, 0xBD0B1410)
                res = emu.run(max_instructions=88, stop='budget')
                snapshots.append({
                    'result': res,
                    'registers': tuple(emu.uc.reg_read(reg) for reg in REGISTERS),
                    'cpsr': emu.uc.reg_read(UC_ARM_REG_CPSR),
                    'ticks': emu.board.ticks,
                    'panel_bits': emu.board.panel.endpoint.bit_count,
                    'panel_writes': list(emu.board.panel.output_writes),
                })
                self._release(emu)
            self.assertEqual(snapshots[0], snapshots[1])
            self.assertEqual(snapshots[1]['panel_bits'], 11)
        finally:
            image[0x2070:0x2070 + len(code)] = b'\x00' * len(code)

    def test_mpu_toggle_and_uart_status_precache_match_reference(self):
        # Exercises cached MPU diff plan across CONTROL bit 0 toggles and
        # UART_STATUS (0xBD0B3C0E) pre-caching after UART_TX writes and frame completions.
        image = self.base_image
        code = bytes.fromhex(
            # 0x2070: MCR p15,0,r4,c1,c0,0 (disable MPU: 0xF8)
            # 0x2074: MCR p15,0,r5,c1,c0,0 (enable MPU: 0xF9)
            # 0x2078: LDRH r7,[r1] (read UART_STATUS 0xBD0B3C0E)
            # 0x207C: TST r7,#0x100
            # 0x2080: BEQ 0x2078 (poll until TX ready)
            # 0x2084: STRH r3,[r2] (write UART_TX 0xBD0B3C06)
            # 0x2088: ADD r3,r3,#1
            # 0x208C: B 0x2070
            'ee014f10 ee015f10 e1d170b0 e3170c01 0afffffc e1c230b0 e2833001 eafffff7'
        )
        image[0x2070:0x2070 + len(code)] = code
        try:
            snapshots = []
            for native in (False, True):
                emu = Emulator(image, native_observer=native, on_event=None)
                emu.board.uart_frame_ticks = 6
                emu.cp15.write((0, 6, 0, 0), 0x3F)
                emu.cp15.write((0, 5, 0, 2), 3)
                emu.cp15.write((0, 5, 0, 3), 3)
                emu.cp15.write(CONTROL, 0xF9)
                emu.uc.reg_write(UC_ARM_REG_R1, 0xBD0B3C0E)
                emu.uc.reg_write(UC_ARM_REG_R2, UART_TX)
                emu.uc.reg_write(UC_ARM_REG_R3, ord('A'))
                emu.uc.reg_write(UC_ARM_REG_R4, 0xF8)
                emu.uc.reg_write(UC_ARM_REG_R5, 0xF9)
                res = emu.run(max_instructions=45, stop='budget')
                snapshots.append({
                    'result': res,
                    'registers': tuple(emu.uc.reg_read(reg) for reg in REGISTERS),
                    'cpsr': emu.uc.reg_read(UC_ARM_REG_CPSR),
                    'ticks': emu.board.ticks,
                    'uart': bytes(emu.uart),
                    'uart_writes': list(emu.uart_writes),
                })
                self._release(emu)
            self.assertEqual(snapshots[0], snapshots[1])
            self.assertEqual(snapshots[1]['uart'], b'ABCD')
        finally:
            image[0x2070:0x2070 + len(code)] = b'\x00' * len(code)

    def test_periodic_timer_ack_and_dtcm_relocation_match_reference(self):
        # Exercises incremental sync_tcm across ITCM/DTCM transitions and
        # deferred ack_802_dirty / cc8_dirty handling across periodic timer ticks.
        image = self.base_image
        code = bytes.fromhex(
            # 0x2070: MCR p15,0,r10,c1,c0,0 (enable ITCM: 0x400F8)
            # 0x2074: MCR p15,0,r1,c9,c1,0 (DTCM base 0x60000000, 4 KiB)
            # 0x2078: MCR p15,0,r2,c1,c0,0 (enable ITCM + DTCM: 0x500F8)
            # 0x207C: STRH r3,[r4] (write 1 to 0xBD040802: ACK timer ch 0)
            # 0x2080: STRH r5,[r6] (write 7 to 0xBD040CC8: raise threshold)
            # 0x2084: STRH r7,[r6] (write 0 to 0xBD040CC8: lower threshold)
            # 0x2088: LDRH r8,[r9] (read 0xBD040CB8: irq_claim)
            # 0x208C: ADD r0,r0,r8
            # 0x2090: B 0x207C
            'ee01af10 ee091f11 ee012f10 e1c430b0 e1c650b0 e1c670b0 e1d980b0 e0800008 eafffff9'
        )
        image[0x2070:0x2070 + len(code)] = code
        try:
            for step in (1, 3):
                snapshots = []
                for native in (False, True):
                    emu = Emulator(image, timer_step=step, us_ticks=1,
                                   native_observer=native, on_event=None)
                    emu.cp15.write((0, 9, 1, 1), 0x00000008)  # ITCM 8 KiB at 0x0
                    # Unmask IRQ 18 (bank 0 bit 18) with priority 2, timer group 0 mask = 1
                    emu.board.write(0xBD040C4C, 4, ~(1 << 18) & 0xFFFFFFFF)
                    emu.board.write(0xBD040CFC - 4 * (18 // 16), 4, 2 << ((18 % 16) * 2))
                    emu.board.write(0xBD040804, 2, 1)
                    emu.board.write(0xBD04081C, 2, 1)         # Periodic ch 0 reload = 1
                    emu.uc.reg_write(UC_ARM_REG_R1, 0x60000006)
                    emu.uc.reg_write(UC_ARM_REG_R2, 0x500F8)
                    emu.uc.reg_write(UC_ARM_REG_R3, 1)
                    emu.uc.reg_write(UC_ARM_REG_R4, 0xBD040802)
                    emu.uc.reg_write(UC_ARM_REG_R5, 7)
                    emu.uc.reg_write(UC_ARM_REG_R6, 0xBD040CC8)
                    emu.uc.reg_write(UC_ARM_REG_R7, 0)
                    emu.uc.reg_write(UC_ARM_REG_R9, 0xBD040CB8)
                    emu.uc.reg_write(REGISTERS[10], 0x400F8)
                    res = emu.run(max_instructions=55, stop='budget')
                    snapshots.append({
                        'result': res,
                        'registers': tuple(emu.uc.reg_read(reg) for reg in REGISTERS),
                        'cpsr': emu.uc.reg_read(UC_ARM_REG_CPSR),
                        'ticks': emu.board.ticks,
                        'timer_pending': emu.board.timer_pending,
                        'irq_raw': emu.board.irq_raw_sources,
                        'irq_claim': emu.board._irq_claim,
                    })
                    self._release(emu)
                self.assertEqual(snapshots[0], snapshots[1])
                self.assertEqual(snapshots[1]['ticks'], 55 * step)
                self.assertGreater(snapshots[1]['registers'][0], 0)
        finally:
            image[0x2070:0x2070 + len(code)] = b'\x00' * len(code)

    def test_c_uart_rx_tx_and_multichannel_timer_fast_paths_match_reference(self):
        # Exercises the C UART0 RX/TX/STATUS fast path (with None, 1-byte, and 2-byte
        # trigger_byte, including mid-stream UART_TX_FLUSH while unread RX bytes remain
        # in board.uart_rx) and 8-channel periodic/one-shot timer expiration
        # (channels 1 and 4 across group 0 and group 1).
        image = self.base_image
        code = bytes.fromhex(
            # 0x2070: LDRH r7,[r1] (read UART_STATUS 0xBD0B3C0E)
            # 0x2074: TST r7,#0x01 (check RX ready)
            # 0x2078: LDRNEH r3,[r6] (if RX ready, read UART_RX 0xBD0B3C0C into r3)
            # 0x207C: TST r7,#0x100 (check TX ready)
            # 0x2080: BEQ 0x2070 (poll until TX ready)
            # 0x2084: STRH r3,[r2] (write r3 to UART_TX 0xBD0B3C06)
            # 0x2088: LDRH r8,[r9] (read 0xBD040800 timer_pending)
            # 0x208C: ADD r0,r0,r8
            # 0x2090: B 0x2070
            'e1d170b0 e3170001 11d630b0 e3170c01 0afffffa e1c230b0 e1d980b0 e0800008 eafffff6'
        )
        image[0x2070:0x2070 + len(code)] = code
        try:
            for trigger_val in (None, ord('!'), 0x10000 | (ord('>') << 8) | ord(' ')):
                for budget in (18, 55):
                    snapshots = []
                    for native in (False, True):
                        seen = []
                        def cb(b, _seen=seen):
                            _seen.append(b)
                        if trigger_val is not None:
                            cb.trigger_byte = trigger_val
                        emu = Emulator(
                            image,
                            native_observer=native,
                            on_event=None,
                            on_uart=cb if trigger_val is not None else None,
                        )
                        emu.board.uart_frame_ticks = 5
                        emu.board.write(0xBD0B4C00, 2, 1)  # Enable UART FIFO
                        emu.board.queue_rx(b'A> B!')
                        # Arm ch 1 periodic (reload=4) and ch 4 one-shot (count=7)
                        emu.board.write(0xBD040804, 2, 0x02)
                        emu.board.write(0xBD040806, 2, 0x10)
                        emu.board.write(0xBD04081E, 2, 4)
                        emu.board.write(0xBD040834, 2, 7)
                        emu.uc.reg_write(UC_ARM_REG_R1, 0xBD0B3C0E)
                        emu.uc.reg_write(UC_ARM_REG_R2, UART_TX)
                        emu.uc.reg_write(UC_ARM_REG_R3, ord('?'))
                        emu.uc.reg_write(UC_ARM_REG_R6, 0xBD0B3C0C)
                        emu.uc.reg_write(UC_ARM_REG_R9, 0xBD040800)
                        res = emu.run(max_instructions=budget, stop='budget')
                        snapshots.append({
                            'result': res,
                            'registers': tuple(emu.uc.reg_read(reg) for reg in REGISTERS),
                            'cpsr': emu.uc.reg_read(UC_ARM_REG_CPSR),
                            'ticks': emu.board.ticks,
                            'uart': bytes(emu.uart),
                            'uart_writes': list(emu.uart_writes),
                            'uart_rx': bytes(emu.board.uart_rx),
                            'pending_uart': emu.board.uart_tx_pending,
                            'timer_pending': emu.board.timer_pending,
                            'seen': list(seen),
                        })
                        self._release(emu)
                    self.assertEqual(snapshots[0], snapshots[1])
        finally:
            image[0x2070:0x2070 + len(code)] = b'\x00' * len(code)


if __name__ == '__main__':
    unittest.main()

