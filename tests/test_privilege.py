import unittest

from unicorn.arm_const import (
    UC_ARM_REG_CPSR, UC_ARM_REG_R0, UC_ARM_REG_R1,
)

from cpu import CONTROL
from emulator import Emulator, FLASH_SIZE


class PrivilegedInstructionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Only the required image signature is needed for these CPU stubs.
        dump = bytearray(FLASH_SIZE)
        dump[:4] = bytes.fromhex('e59ff018')
        dump[0x20:0x24] = bytes.fromhex('00002070')
        cls.dump = bytes(dump)

    def make_emulator(self, instruction, *, data_ap=1, user=False):
        emu = Emulator(self.dump)
        emu._limit, emu._stop = 8, 'budget'
        emu.uc.mem_write(0xA0001000, bytes.fromhex(instruction))
        emu.cp15.write((0, 6, 0, 0), 0x3F)  # 4 GiB background.
        emu.cp15.write((0, 5, 0, 2), data_ap)
        emu.cp15.write((0, 5, 0, 3), 3)
        emu.cp15.write(CONTROL, emu.cp15.control | 1)
        emu.uc.reg_write(UC_ARM_REG_CPSR, 0x2D0 if user else 0x2D3)
        emu.uc.reg_write(UC_ARM_REG_R0, 0xDEADBEEF)
        emu.uc.reg_write(UC_ARM_REG_R1, 0xA0002000)
        return emu

    @staticmethod
    def execute(emu):
        emu.uc.emu_start(0xA0001000, 0xA0001004, count=1)

    def test_user_cp15_cannot_disable_mpu(self):
        emu = self.make_emulator('ee010f10', user=True)  # MCR p15,0,r0,c1,c0,0.
        emu.uc.reg_write(UC_ARM_REG_R0, 0xF8)
        self.execute(emu)
        self.assertTrue(emu.cp15.mpu_enabled)
        self.assertEqual(emu.reason, 'fault')
        self.assertIn('Undefined Instruction', emu.error)

    def test_failed_condition_does_not_trap_user_cp15(self):
        emu = self.make_emulator('0e010f10', user=True)  # MCREQ with Z clear.
        self.execute(emu)
        self.assertTrue(emu.cp15.mpu_enabled)
        self.assertIsNone(emu.error)

    def test_strt_checks_user_permissions_before_store_or_writeback(self):
        emu = self.make_emulator('e4a10004')  # STRT r0,[r1],#4, from SVC mode.
        self.execute(emu)
        self.assertIn('MPU denied write', emu.error)
        self.assertEqual(bytes(emu.uc.mem_read(0xA0002000, 4)), bytes(4))
        self.assertEqual(emu.uc.reg_read(UC_ARM_REG_R1), 0xA0002000)

    def test_allowed_strt_executes_natively(self):
        emu = self.make_emulator('e4a10004', data_ap=3)
        self.execute(emu)
        self.assertIsNone(emu.error)
        self.assertEqual(bytes(emu.uc.mem_read(0xA0002000, 4)), bytes.fromhex('deadbeef'))
        self.assertEqual(emu.uc.reg_read(UC_ARM_REG_R1), 0xA0002004)

    def test_ldrt_accepts_user_read_only_permission(self):
        emu = self.make_emulator('e4b10004', data_ap=2)  # LDRT r0,[r1],#4.
        emu.uc.mem_write(0xA0002000, bytes.fromhex('12345678'))
        self.execute(emu)
        self.assertIsNone(emu.error)
        self.assertEqual(emu.uc.reg_read(UC_ARM_REG_R0), 0x12345678)
        self.assertEqual(emu.uc.reg_read(UC_ARM_REG_R1), 0xA0002004)

    def test_unaligned_unprivileged_word_transfer_stops_explicitly(self):
        emu = self.make_emulator('e4a10004', data_ap=3)
        emu.uc.reg_write(UC_ARM_REG_R1, 0xA0002001)
        self.execute(emu)
        self.assertIn('unaligned ARM946 unprivileged word transfer', emu.error)
        self.assertEqual(bytes(emu.uc.mem_read(0xA0002000, 8)), bytes(8))


if __name__ == '__main__':
    unittest.main()
