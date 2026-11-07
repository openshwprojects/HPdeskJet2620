import unittest

from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_ARM, UC_MODE_BIG_ENDIAN
from unicorn.arm_const import (
    UC_CPU_ARM_946, UC_ARM_REG_CPSR, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
)

from cpu import CP15, CONTROL, DTCM_REGION, CpuModelError
from memory import PhysicalMemory, FLASH_SIZE


class PhysicalMemoryTests(unittest.TestCase):
    _BASE_DUMP = bytes.fromhex('11223344') + bytes(FLASH_SIZE - 4)

    def setUp(self):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_BIG_ENDIAN)
        self.uc.ctl_set_cpu_model(UC_CPU_ARM_946)
        self.uc.reg_write(UC_ARM_REG_CPSR, 0x2D3)
        self.cpu = CP15()
        self.dump = self._BASE_DUMP
        self.memory = PhysicalMemory(self.uc, self.dump, self.cpu)

    def test_dram_apertures_and_capacity_wrap_share_storage(self):
        self.uc.mem_write(0xA0001234, b'physical')
        for base in (0x20000000, 0x60000000, 0xA0000000):
            for repeat in (0, 0x04000000):
                self.assertEqual(bytes(self.uc.mem_read(base + repeat + 0x1234, 8)), b'physical')

    def test_dtcm_relocation_preserves_bank_and_underlying_dram(self):
        self.uc.mem_write(0x60000000, b'DDR!')
        self.cpu.write(DTCM_REGION, 0x10000006)
        self.cpu.write(CONTROL, self.cpu.control | 0x10000)
        self.assertTrue(self.memory.sync_tcm())
        self.uc.mem_write(0x10000000, b'TCM!')
        self.cpu.write(DTCM_REGION, 0x60000006)
        self.memory.sync_tcm()
        self.assertEqual(bytes(self.uc.mem_read(0x60000000, 4)), b'TCM!')
        self.assertEqual(bytes(self.uc.mem_read(0xA0000000, 4)), b'DDR!')
        self.cpu.write(CONTROL, self.cpu.control & ~0x10000)
        self.memory.sync_tcm()
        self.assertEqual(bytes(self.uc.mem_read(0x60000000, 4)), b'DDR!')
        self.cpu.write(CONTROL, self.cpu.control | 0x10000)
        self.memory.sync_tcm()
        self.assertEqual(bytes(self.uc.mem_read(0x60000000, 4)), b'TCM!')

    def test_load_mode_cpu_store_updates_itcm_but_next_load_reads_flash(self):
        self.cpu.write(CONTROL, self.cpu.control | 0xC0000)
        self.memory.sync_tcm()
        # STR r0,[r1]; LDR r2,[r1]. Load mode must separate their targets.
        self.uc.mem_write(0xA0001000, bytes.fromhex('e5810000e5912000'))
        self.uc.reg_write(UC_ARM_REG_R0, 0xDEADBEEF)
        self.uc.reg_write(UC_ARM_REG_R1, 0)
        self.uc.emu_start(0xA0001000, 0xA0001008)
        self.assertEqual(self.uc.reg_read(UC_ARM_REG_R2), 0x11223344)
        self.assertTrue(self.memory.check_flash())
        self.cpu.write(CONTROL, self.cpu.control & ~0x80000)
        self.memory.sync_tcm()
        self.assertEqual(bytes(self.uc.mem_read(0, 4)), bytes.fromhex('deadbeef'))
        self.cpu.write(CONTROL, self.cpu.control & ~0x40000)
        self.memory.sync_tcm()
        self.assertEqual(bytes(self.uc.mem_read(0, 4)), self.dump[:4])
        self.assertTrue(self.memory.check_flash())

    def test_dtcm_instruction_bus_limitation_is_an_explicit_fault(self):
        self.cpu.write(DTCM_REGION, 0x60000006)
        self.cpu.write(CONTROL, self.cpu.control | 0x10000)
        self.memory.sync_tcm()
        with self.assertRaises(UcError):
            self.uc.emu_start(0x60000000, 0x60000004, count=1)
        self.assertIn('separate instruction/data bus', self.memory.error)

    def test_flash_rejects_normal_guest_store(self):
        self.uc.mem_write(0xA0001000, bytes.fromhex('e5810000'))
        self.uc.reg_write(UC_ARM_REG_R0, 0xDEADBEEF)
        self.uc.reg_write(UC_ARM_REG_R1, 0)
        with self.assertRaises(UcError):
            self.uc.emu_start(0xA0001000, 0xA0001004)
        self.assertTrue(self.memory.check_flash())

    def test_explicit_absent_flash_window_is_read_only_and_keeps_other_holes(self):
        from unicorn import UC_PROT_READ
        with self.assertRaises(UcError):
            self.uc.mem_read(0x03f02000, 1)
        uc = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_BIG_ENDIAN)
        uc.ctl_set_cpu_model(UC_CPU_ARM_946)
        uc.reg_write(UC_ARM_REG_CPSR, 0x2D3)
        memory = PhysicalMemory(uc, self.dump, CP15(), absent_flash_window=True)
        self.assertEqual(bytes(uc.mem_read(0x03f02000, 0xe000)), b'\xff' * 0xe000)
        self.assertEqual(memory.max_permissions(0x03f02000), UC_PROT_READ)
        with self.assertRaises(UcError):
            uc.mem_read(0x02000000, 1)
        # Native guest stores cannot change an unconnected bus input.
        uc.mem_write(0xA0001000, bytes.fromhex('e5810000'))
        uc.reg_write(UC_ARM_REG_R0, 0)
        uc.reg_write(UC_ARM_REG_R1, 0x03f02000)
        with self.assertRaises(UcError):
            uc.emu_start(0xA0001000, 0xA0001004)
        self.assertEqual(bytes(uc.mem_read(0x03f02000, 4)), b'\xff' * 4)
        self.assertTrue(memory.check_flash())

    def test_tcm_aperture_aliases_one_physical_bank(self):
        self.cpu.write(DTCM_REGION, 0x10000008)  # 8 KiB aperture, 4 KiB bank.
        self.cpu.write(CONTROL, self.cpu.control | 0x10000)
        self.memory.sync_tcm()
        self.uc.mem_write(0x10001004, b'bank')
        self.assertEqual(bytes(self.uc.mem_read(0x10000004, 4)), b'bank')

    def test_denied_load_mode_store_does_not_mutate_tcm(self):
        self.cpu.write((0, 6, 0, 0), 0x3F)
        self.cpu.write((0, 5, 0, 3), 3)  # Executable background, no data access.
        self.cpu.write(CONTROL, self.cpu.control | 0xC0001)
        self.memory.sync_tcm()
        self.uc.mem_write(0xA0001000, bytes.fromhex('e5810000'))
        self.uc.reg_write(UC_ARM_REG_R0, 0xDEADBEEF)
        self.uc.reg_write(UC_ARM_REG_R1, 0)
        with self.assertRaisesRegex(CpuModelError, 'MPU denied write'):
            self.uc.emu_start(0xA0001000, 0xA0001004)
        self.assertTrue(self.memory.check_flash())
        self.cpu.write(CONTROL, self.cpu.control & ~0x80000)
        self.memory.sync_tcm()
        self.assertEqual(bytes(self.uc.mem_read(0, 4)), bytes(4))


if __name__ == '__main__':
    unittest.main()
