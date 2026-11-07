from pathlib import Path
import pickle
import struct
import unittest

from usb import USBError, UsbDeviceController


class UsbDeviceControllerUnitTests(unittest.TestCase):
    def test_attach_bus_reset_and_setup_helpers(self):
        irqs: dict[int, bool] = {}
        ram = bytearray(0x1000)

        def dma_read(addr: int, size: int) -> bytes:
            return bytes(ram[addr : addr + size])

        def dma_write(addr: int, data: bytes) -> None:
            ram[addr : addr + len(data)] = data

        usb = UsbDeviceController(
            set_irq=lambda src, lvl: irqs.__setitem__(src, lvl),
            dma_read=dma_read,
            dma_write=dma_write,
        )
        usb.write(0xBD09FC2A, 2, UsbDeviceController.EVENT_BUS_RESET | UsbDeviceController.EVENT_EP0_SETUP)
        usb.bus_reset(high_speed=True)
        self.assertTrue(usb.attached)
        self.assertEqual(usb.read(0xBD09FC04, 4), 0x00000401)
        self.assertEqual(usb.read(0xBD09FC28, 2) & UsbDeviceController.EVENT_BUS_RESET, UsbDeviceController.EVENT_BUS_RESET)
        self.assertTrue(irqs[23])

        # W1C bus reset interrupt
        usb.write(0xBD09FC28, 2, UsbDeviceController.EVENT_BUS_RESET)
        self.assertFalse(irqs[23])

        # Standard GET_DESCRIPTOR (Device)
        ram[0x100 : 0x112] = b"\x12\x01\x00\x02\x00\x00\x00\x40\xf0\x03\x53\x00\x00\x01\x01\x02\x03\x01"
        usb.setup_get_descriptor(1, 0, w_length=18)
        self.assertEqual(usb.read(0xBD09FC10, 4), 0x80060001)
        self.assertEqual(usb.read(0xBD09FC14, 4), 0x00001200)
        usb.write(0xBD09BC24, 4, 0x100)
        usb.write(0xBD09BC28, 4, 18)
        usb.write(0xBD09BC2E, 2, 1)
        usb.write(0xBD09FC1A, 2, 1)
        self.assertEqual(len(usb.control_transfers), 1)
        self.assertEqual(usb.control_transfers[-1].response, bytes(ram[0x100:0x112]))
        self.assertFalse(usb.control_transfers[-1].stalled)

        # Standard SET_ADDRESS handled by SIE when EVENT_EP0_SETUP is enabled
        usb.setup_set_address(7)
        self.assertEqual(usb.device_address, 7)
        self.assertEqual(usb.control_transfers[-1].device_address, 7)

        # Bulk OUT DMA transfer on EP4 (in two chunks to verify remaining RX count update)
        usb.write(0xBD09FC1E, 2, 1 << 4)
        usb.write(0xBD09FC26, 2, 1 << 4)
        usb.write(0xBD09BC00 + 0x400 * 4 + 0x00, 4, 0x0000A200)
        payload = b"GET /DevMgmt/ProductConfigDyn.xml HTTP/1.1\r\n\r\n"
        usb.send_bulk_out(4, payload)
        self.assertTrue(irqs[24])
        usb.write(0xBD09BC00 + 0x400 * 4 + 0x24, 4, 0x200)
        usb.write(0xBD09BC00 + 0x400 * 4 + 0x28, 4, 16)
        usb.write(0xBD09BC00 + 0x400 * 4 + 0x2E, 2, 1)
        self.assertEqual(bytes(ram[0x200 : 0x200 + 16]), payload[:16])
        self.assertEqual(usb.read(0xBD09BC00 + 0x400 * 4 + 0x10, 2), (len(payload) - 16) << 2)
        self.assertFalse(bool(usb.read(0xBD09FC1C, 2) & (1 << 4)))
        # Re-enabling 0xBD09FC1E re-asserts 0xBD09FC1C for the remaining RX bytes (short packet vs 512-byte DMA)
        usb.write(0xBD09FC1E, 2, 1 << 4)
        self.assertTrue(bool(usb.read(0xBD09FC1C, 2) & (1 << 4)))
        usb.write(0xBD09BC00 + 0x400 * 4 + 0x24, 4, 0x210)
        usb.write(0xBD09BC00 + 0x400 * 4 + 0x28, 4, 512)
        usb.write(0xBD09BC00 + 0x400 * 4 + 0x2E, 2, 1)
        self.assertEqual(bytes(ram[0x200 : 0x200 + len(payload)]), payload)
        self.assertEqual(usb.read(0xBD09BC00 + 0x400 * 4 + 0x28, 4), 512 - (len(payload) - 16))
        self.assertEqual(usb.read(0xBD09BC00 + 0x400 * 4 + 0x10, 2), 0)
        self.assertFalse(bool(usb.read(0xBD09FC1C, 2) & (1 << 4)))
        # Clear EP4 DMA completion interrupt
        usb.write(0xBD09BC00 + 0x400 * 4 + 0x20, 2, 1)
        self.assertFalse(irqs[24])

        # Bulk IN (device-to-host) DMA transfer on EP9 signals DMA completion on IRQ 24
        tx_resp = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        ram[0x300 : 0x300 + len(tx_resp)] = tx_resp
        usb.write(0xBD09FC26, 2, 1 << 9)
        usb.write(0xBD09BC00 + 0x400 * 9 + 0x00, 4, 0x00002200)  # Bulk IN (bit 15 == 0)
        usb.write(0xBD09BC00 + 0x400 * 9 + 0x24, 4, 0x300)
        usb.write(0xBD09BC00 + 0x400 * 9 + 0x28, 4, len(tx_resp))
        usb.write(0xBD09BC00 + 0x400 * 9 + 0x2E, 2, 1)
        self.assertEqual(usb.endpoint_transfers[-1], (9, 0, tx_resp))
        self.assertEqual(usb.read_bulk_in(9, 10), tx_resp[:10])
        self.assertEqual(usb.drain_bulk_in(9), tx_resp[10:])
        self.assertEqual(usb.drain_bulk_in(9), b"")
        self.assertEqual(usb.read(0xBD09BC00 + 0x400 * 9 + 0x28, 4), 0)
        self.assertEqual(usb.read(0xBD09BC00 + 0x400 * 9 + 0x2C, 2), 0xFFFF)
        self.assertTrue(bool(usb.read(0xBD09FC24, 2) & (1 << 9)))
        self.assertTrue(irqs[24])

    def test_invalid_inputs_and_widths_raise(self):
        usb = UsbDeviceController()
        with self.assertRaises(ValueError):
            usb.send_setup(b"\x00" * 7)
        with self.assertRaises(ValueError):
            usb.setup_set_address(128)
        with self.assertRaises(ValueError):
            usb.send_bulk_out(0, b"x")
        with self.assertRaises(USBError):
            usb.read(0xBD09FC01, 2)

    def test_halfword_setup_read_soft_reset_and_dma_ack_chaining(self):
        ram = bytearray(0x1000)
        usb = UsbDeviceController(
            dma_read=lambda addr, size: bytes(ram[addr : addr + size]),
            dma_write=lambda addr, data: ram.__setitem__(slice(addr, addr + len(data)), data),
        )
        usb.setup_get_descriptor(1, 0, w_length=18)
        self.assertTrue(bool(usb.read(0xBD09FC28, 2) & UsbDeviceController.EVENT_EP0_SETUP))
        # Reading 0xBD09FC14 (size=2) does not clear EVENT_EP0_SETUP until 0xBD09FC16 (size=2) is read
        self.assertEqual(usb.read(0xBD09FC14, 2), 0x0000)
        self.assertTrue(bool(usb.read(0xBD09FC28, 2) & UsbDeviceController.EVENT_EP0_SETUP))
        self.assertEqual(usb.read(0xBD09FC16, 2), 0x1200)
        self.assertFalse(bool(usb.read(0xBD09FC28, 2) & UsbDeviceController.EVENT_EP0_SETUP))

        # Multi-packet Bulk-OUT chaining when 0xBD09FC1E bit ep stays enabled across DMA ack
        usb.write(0xBD09FC1E, 2, 1 << 4)
        usb.write(0xBD09BC00 + 0x400 * 4 + 0x00, 4, 0x0000A200)
        usb.send_bulk_out(4, b"ABCDEFGH")
        usb.write(0xBD09BC00 + 0x400 * 4 + 0x24, 4, 0x100)
        usb.write(0xBD09BC00 + 0x400 * 4 + 0x28, 4, 4)
        usb.write(0xBD09BC00 + 0x400 * 4 + 0x2E, 2, 1)
        self.assertFalse(bool(usb.read(0xBD09FC1C, 2) & (1 << 4)))
        # Acknowledging DMA completion at eb + 0x20 while 0xBD09FC1E has bit 4 set re-asserts 0xBD09FC1C
        usb.write(0xBD09BC00 + 0x400 * 4 + 0x20, 2, 1)
        self.assertTrue(bool(usb.read(0xBD09FC1C, 2) & (1 << 4)))

        # Soft reset (bit 19 of 0xBD09FC00) clears pending status and device address
        usb._device_address = 9
        usb.write(0xBD09FC00, 4, 0x00090000)
        self.assertEqual(usb.read(0xBD09FC00, 4), 0x00010000)
        self.assertEqual(usb.device_address, 0)
        self.assertEqual(usb.read(0xBD09FC1C, 2), 0)

        usb.reset()
        self.assertFalse(usb.attached)
        self.assertEqual(usb.endpoint_transfers, [])


class UsbLiveFirmwareEnumerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from analysis.uart_selftest import load_default_dump_and_snapshot

        loaded = load_default_dump_and_snapshot()
        if loaded is None:
            raise unittest.SkipTest("dump.bin or Stage-22 snapshot not available")
        cls.dump, cls.snap = loaded

    def test_live_usb20_enumeration_and_ieee1284_device_id(self):
        from analysis.uart_selftest import build_and_restore, step_restored

        emu = build_and_restore(self.dump, self.snap)
        usb = emu.board.usb

        def step(insns: int) -> None:
            step_restored(emu, insns)

        def _stop_after(extra: int = 5_000) -> None:
            cur = emu._native_observer.state.instructions if emu._native_observer is not None else emu.instructions
            target = cur + max(0, extra)
            emu._limit = target
            if emu._native_observer is not None:
                emu._native_observer.state.limit = target

        def step_ctrl(max_insns: int = 150_000, extra: int = 5_000) -> None:
            if usb.control_transfers and usb.control_transfers[-1].setup == usb.last_setup:
                step(extra)
                return
            usb.on_control_transfer = lambda _xfer: _stop_after(extra)
            try:
                step(max_insns)
            finally:
                usb.on_control_transfer = None

        # Let restored IRQ complete before asserting USB bus reset
        step(15_000)

        usb.bus_reset(high_speed=True)
        step(25_000)

        # 1. GET_DESCRIPTOR (Device)
        usb.setup_get_descriptor(1, 0, w_length=18)
        step_ctrl()
        dev_desc = usb.control_transfers[-1].response
        self.assertEqual(len(dev_desc), 18)
        vid, pid = struct.unpack_from("<HH", dev_desc, 8)
        self.assertEqual((vid, pid), (0x03F0, 0x0053))

        # 2. SET_ADDRESS (5)
        usb.setup_set_address(5)
        step_ctrl()
        self.assertEqual(usb.device_address, 5)

        # 3. GET_DESCRIPTOR (Configuration)
        usb.setup_get_descriptor(2, 0, w_length=255)
        step_ctrl()
        cfg_desc = usb.control_transfers[-1].response
        self.assertEqual(len(cfg_desc), 177)
        self.assertEqual(cfg_desc[4], 4)  # 4 interfaces

        # 4. GET_DESCRIPTOR (String 2 - Product)
        usb.setup_get_descriptor(3, 2, w_index=0x0409, w_length=255)
        step_ctrl()
        prod_str = usb.control_transfers[-1].response[2:].decode("utf-16le", errors="ignore")
        self.assertEqual(prod_str, "DeskJet 2600 series")

        # 5. SET_CONFIGURATION (1) -> configures endpoints and enables Bulk-OUT IRQs
        usb.setup_set_configuration(1)
        step_ctrl(extra=25_000)
        self.assertEqual(usb.read(0xBD09FC1E, 2) & 0x0512, 0x0512)

        # 6. IEEE 1284 GET_DEVICE_ID on Interface 1
        usb.setup_get_device_id(interface=1, w_length=512)
        step_ctrl()
        dev_id_raw = usb.control_transfers[-1].response
        self.assertGreater(len(dev_id_raw), 200)
        dev_id = dev_id_raw[2:].decode("latin1")
        self.assertIn("MFG:HP;MDL:DeskJet 2600 series;", dev_id)
        self.assertIn("CMD:PCL3GUI,PJL,", dev_id)

        # 7. Bulk-OUT DMA transfer on EP8 (Printer class 07/01/02)
        pjl_cmd = b"\x1b%-12345X@PJL INFO ID\r\n\x1b%-12345X"
        usb.on_endpoint_transfer = lambda xfer: _stop_after(25_000) if xfer[0] == 8 and not usb.rx_queues[8] else None
        usb.send_bulk_out(8, pjl_cmd)
        step(600_000)
        usb.on_endpoint_transfer = None
        ep8_rx = b"".join(data for ep, _ch, data in usb.endpoint_transfers if ep == 8)
        self.assertEqual(ep8_rx, pjl_cmd)

        # 8. Short Bulk-OUT DMA transfer on EP4 (LEDM REST HTTP interface)
        ledm_req = b"GET /DevMgmt/ProductConfigDyn.xml HTTP/1.1\r\nHost: localhost\r\n\r\n"
        usb.on_endpoint_transfer = lambda xfer: _stop_after(80_000) if xfer[0] == 4 and not usb.rx_queues[4] else None
        usb.send_bulk_out(4, ledm_req)
        step(600_000)
        usb.on_endpoint_transfer = None
        ep4_rx = b"".join(data for ep, _ch, data in usb.endpoint_transfers if ep == 4)
        self.assertEqual(ep4_rx, ledm_req)
        self.assertEqual(usb.read(0xBD09BC00 + 0x400 * 4 + 0x28, 4), 0x804 - len(ledm_req))
        self.assertEqual(bytes(emu.uc.mem_read(0x20AF5444, len(ledm_req))), ledm_req)

    def test_differential_python_vs_native_usb_enumeration(self):
        from analysis.uart_selftest import (
            REGISTERS,
            UC_ARM_REG_CPSR,
            build_and_restore,
            step_restored,
        )

        outcomes = []
        for native in (False, True):
            emu = None
            emu = build_and_restore(self.dump, self.snap, native_observer=native)
            emu.board.usb.bus_reset(high_speed=True)
            step_restored(emu, 25_000)
            emu.board.usb.setup_get_descriptor(1, 0, w_length=18)
            step_restored(emu, 25_000)
            outcomes.append(
                (
                    emu.instructions,
                    emu.board.ticks,
                    tuple(emu.uc.reg_read(r) for r in REGISTERS),
                    emu.uc.reg_read(UC_ARM_REG_CPSR),
                    list(emu.board.usb.control_transfers),
                )
            )
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(len(outcomes[1][4][-1].response), 18)


if __name__ == "__main__":
    unittest.main()

