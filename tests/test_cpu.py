import unittest

from cpu import CP15, CONTROL, DTCM_REGION, ITCM_REGION, CpuModelError


class SystemControlTests(unittest.TestCase):
    def test_reset_byte_order_is_explicit_and_cannot_change(self):
        cpu = CP15()
        self.assertEqual(cpu.control, 0xF8)
        with self.assertRaisesRegex(CpuModelError, 'byte order'):
            cpu.write(CONTROL, cpu.control & ~0x80)
        self.assertEqual(cpu.control, 0xF8)
        little = CP15(big_endian=False)
        self.assertEqual(little.control, 0x78)
        with self.assertRaisesRegex(CpuModelError, 'byte order'):
            little.write(CONTROL, little.control | 0x80)

    def test_arm946_has_no_sctlr_alignment_check_bit(self):
        cpu = CP15()
        with self.assertRaisesRegex(CpuModelError, 'bit 1 is reserved'):
            cpu.write(CONTROL, cpu.control | 2)
        self.assertEqual(cpu.control & 2, 0)

    def test_tcm_discovery_relocation_and_aliases(self):
        changes = []
        cpu = CP15(on_change=lambda *event: changes.append(event))
        descriptor = cpu.read((0, 0, 0, 2))
        self.assertEqual((descriptor >> 6) & 15, 4)  # 8 KiB ITCM.
        self.assertEqual((descriptor >> 18) & 15, 3)  # 4 KiB DTCM.
        self.assertEqual(descriptor & 0x4004, 0)
        cpu.write(DTCM_REGION, 0x10000006)
        cpu.write(CONTROL, cpu.control | 0x10000)
        dtcm = cpu.tcm_regions()[1]
        self.assertTrue(dtcm.enabled)
        self.assertEqual((dtcm.address, dtcm.size), (0x10000000, 4096))
        cpu.write(DTCM_REGION, 0x60000008)  # 8 KiB aperture, 4 KiB RAM.
        dtcm = cpu.tcm_regions()[1]
        self.assertEqual(dtcm.physical_offset(0x60001004), 4)
        self.assertEqual(changes[-1], (DTCM_REGION, 0x10000006, 0x60000008))

    def test_itcm_load_mode_is_explicit_and_base_is_fixed(self):
        cpu = CP15()
        cpu.write(CONTROL, cpu.control | 0xC0000)
        self.assertTrue(cpu.tcm_regions()[0].load_mode)
        cpu.write(CONTROL, cpu.control & ~0x80000)
        self.assertTrue(cpu.tcm_regions()[0].enabled)
        self.assertFalse(cpu.tcm_regions()[0].load_mode)
        with self.assertRaises(CpuModelError):
            cpu.write(ITCM_REGION, 0x2008)

    def test_highest_mpu_region_wins_and_user_permissions_apply(self):
        cpu = CP15()
        cpu.write((0, 6, 0, 0), 0x3F)  # 4 GiB background.
        cpu.write((0, 6, 7, 0), 0x1017)  # 4 KiB at 0x1000.
        cpu.write((0, 5, 0, 2), 0x20000003)  # Background RW; region 7 user RO.
        cpu.write((0, 5, 0, 3), 0x00000003)  # Region 7 cannot execute.
        cpu.write(CONTROL, cpu.control | 1)
        cpu.check_access(0x1000, 4, user=True)
        cpu.check_access(0x1000, 4, write=True)
        cpu.check_access(0x8000, 4, write=True, user=True)
        with self.assertRaises(CpuModelError):
            cpu.check_access(0x1000, 4, write=True, user=True)
        with self.assertRaises(CpuModelError):
            cpu.check_access(0x1000, 4, instruction=True)
        with self.assertRaises(CpuModelError):
            cpu.check_access(0x0FFE, 4, write=True, user=True)
        with self.assertRaises(CpuModelError):
            cpu.check_access(0, 0x3000, instruction=True)

    def test_permission_register_standard_and_extended_views_share_state(self):
        cpu = CP15()
        cpu.write((0, 5, 0, 2), 0x12305631)
        self.assertEqual(cpu.read((0, 5, 0, 0)), 0x6C6D)
        cpu.write((0, 5, 0, 0), 0xE4E4)
        self.assertEqual(cpu.read((0, 5, 0, 2)), 0x32103210)

    def test_invalid_or_unsupported_configuration_is_rejected(self):
        cpu = CP15()
        with self.assertRaises(CpuModelError):
            cpu.write(CONTROL, cpu.control | 1)
        with self.assertRaises(CpuModelError):
            cpu.write((0, 6, 0, 0), 0x1019)  # Misaligned 8 KiB.
        with self.assertRaises(CpuModelError):
            cpu.read((0, 15, 0, 0))
        with self.assertRaises(CpuModelError):
            cpu.write((0, 0, 0, 0), 123)


if __name__ == '__main__':
    unittest.main()
