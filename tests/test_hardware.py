"""Behavioral checks for clocked peripheral state independent of firmware PCs."""
import struct
import unittest

from micci import CarriageAsic, RegisterDevice
from sdio import SDIO_BASE

from hardware import (
    BootHardware, HardwareError, PLL_CONTROL, PLL_STATUS,
    SECOND_PLL_RESET, SECOND_PLL_CONTROL, SECOND_PLL_CONFIG, SECOND_PLL_LOCK,
    SDRAM_CONTROL, SDRAM_STATUS, TIMER, UART_TX, UART_RX, UART_STATUS,
    BUS_MONITOR_ENABLE, BUS_MONITOR_STATUS, BUS_MONITOR_ADDRESS,
    RESET_RETENTION, WATCHDOG_CONTROL, WATCHDOG_COUNT, WATCHDOG_RELOAD,
)


class ClockedHardwareTests(unittest.TestCase):
    def test_timer_reads_are_observational_and_time_is_explicit(self):
        board = BootHardware(timer_step=3)
        self.assertEqual(board.read(TIMER, 4), 0)
        self.assertEqual(board.read(TIMER, 4), 0)
        board.advance(7)
        self.assertEqual(board.read(TIMER, 4), 21)
        self.assertEqual(board.read(TIMER, 4), 21)

    def test_timer_preload_wraps_without_rescheduling_devices(self):
        board = BootHardware(pll_lock_ticks=10)
        board.write(PLL_CONTROL, 4, 0x10002)
        board.write(PLL_CONTROL, 4, 2)
        board.advance(3)
        board.write(TIMER, 4, 0xFFFFFFFC)
        self.assertEqual(board.read(TIMER, 4), 0xFFFFFFFC)
        board.advance(4)
        self.assertEqual(board.read(TIMER, 4), 0)
        self.assertEqual(board.read(PLL_STATUS, 4), 0)
        board.advance(3)
        self.assertEqual(board.read(TIMER, 4), 3)
        self.assertEqual(board.read(PLL_STATUS, 4), 0x10000)

    def test_polling_does_not_make_pll_lock_early(self):
        board = BootHardware(pll_lock_ticks=10)
        board.write(PLL_CONTROL, 4, 0x10002)
        board.write(PLL_CONTROL, 4, 2)
        for _ in range(100):
            self.assertEqual(board.read(PLL_STATUS, 4), 0)
        board.advance(9)
        self.assertEqual(board.read(PLL_STATUS, 4), 0)
        board.advance()
        self.assertEqual(board.read(PLL_STATUS, 4), 0x10000)

    def test_reasserting_reset_cancels_inflight_lock(self):
        board = BootHardware(pll_lock_ticks=10)
        board.write(PLL_CONTROL, 4, 0x10002)
        board.write(PLL_CONTROL, 4, 2)
        board.advance(5)
        board.write(PLL_CONTROL, 4, 0x10002)
        board.advance(5)
        self.assertEqual(board.read(PLL_STATUS, 4), 0)
        board.write(PLL_CONTROL, 4, 2)
        board.advance(9)
        self.assertEqual(board.read(PLL_STATUS, 4), 0)
        board.advance()
        self.assertEqual(board.read(PLL_STATUS, 4), 0x10000)

    def test_second_pll_requires_reset_release_and_both_config_words(self):
        board = BootHardware(pll_lock_ticks=10)
        board.write(SECOND_PLL_CONTROL, 4, 0xCA21A3C3)
        board.write(SECOND_PLL_CONFIG, 4, 0x322)
        board.advance(100)
        self.assertFalse(board.read(SECOND_PLL_CONTROL, 4) & SECOND_PLL_LOCK)
        board.write(SECOND_PLL_RESET, 4, 0x1000)
        board.advance(100)
        self.assertFalse(board.read(SECOND_PLL_CONTROL, 4) & SECOND_PLL_LOCK)
        board.write(SECOND_PLL_RESET, 4, 0)
        board.advance(9)
        self.assertFalse(board.read(SECOND_PLL_CONTROL, 4) & SECOND_PLL_LOCK)
        board.advance()
        self.assertEqual(board.read(SECOND_PLL_CONTROL, 4), 0xCAA1A3C3)
        board.write(SECOND_PLL_RESET, 4, 0x1000)
        self.assertFalse(board.read(SECOND_PLL_CONTROL, 4) & SECOND_PLL_LOCK)

    def test_second_pll_status_cannot_be_written_or_obtained_without_config(self):
        board = BootHardware(pll_lock_ticks=10)
        board.write(SECOND_PLL_RESET, 4, 0x1000)
        board.write(SECOND_PLL_RESET, 4, 0)
        board.write(SECOND_PLL_CONTROL, 4, SECOND_PLL_LOCK | 1)
        board.advance(100)
        self.assertEqual(board.read(SECOND_PLL_CONTROL, 4), 1)
        board.write(SECOND_PLL_CONFIG, 4, 2)
        board.advance(5)
        board.write(SECOND_PLL_CONTROL, 4, 0)
        board.advance(5)
        self.assertEqual(board.read(SECOND_PLL_CONTROL, 4), 0)

    def test_sdram_calibration_is_delayed_and_cancellable(self):
        board = BootHardware(sdram_calibration_ticks=10)
        board.write(SDRAM_CONTROL, 2, 1)
        board.advance(5)
        self.assertEqual(board.read(SDRAM_STATUS, 2), 0)
        board.write(SDRAM_CONTROL, 2, 0)
        board.advance(5)
        self.assertEqual(board.read(SDRAM_STATUS, 2), 0)
        board.write(SDRAM_CONTROL, 2, 1)
        board.advance(10)
        self.assertEqual(board.read(SDRAM_STATUS, 2), 0x4000)

    def test_uart_has_backpressure_and_emits_completed_frames_once(self):
        board = BootHardware(uart_frame_ticks=10)
        board.write(UART_TX, 2, 0x141)
        self.assertEqual(board.read(UART_STATUS, 2), 0)
        self.assertEqual(board.drain_uart(), b'')
        with self.assertRaises(HardwareError):
            board.write(UART_TX, 2, 66)
        board.advance(9)
        self.assertEqual(board.drain_uart(), b'')
        board.advance()
        self.assertEqual(board.drain_uart(), b'A')
        self.assertEqual(board.drain_uart(), b'')
        self.assertEqual(board.read(UART_STATUS, 2), 0x100)

    def test_rx_data_is_consumed_without_guessing_status_bits(self):
        board = BootHardware()
        board.queue_rx(b'ab')
        self.assertEqual(board.read(UART_STATUS, 2), 0x100)
        self.assertEqual(board.read(UART_RX, 2), ord('a'))
        self.assertEqual(board.read(UART_RX, 2), ord('b'))
        self.assertEqual(board.read(UART_RX, 2), 0)

    def test_overlapping_register_views_are_big_endian(self):
        board = BootHardware()
        board.write(SECOND_PLL_RESET, 4, 0x12345678)
        self.assertEqual(board.read(SECOND_PLL_RESET, 2), 0x1234)
        board.write(SECOND_PLL_RESET, 2, 0xABCD)
        self.assertEqual(board.read(SECOND_PLL_RESET, 4), 0xABCD5678)

    def test_irq_configuration_storage_does_not_enable_unknown_registers(self):
        board = BootHardware()
        for address, size, value in (
            (0xBD040C00, 4, 0), (0xBD040C4C, 4, 0xFFFFFFFF),
            (0xBD040CFC, 4, 0x12345678), (0xBD040CB0, 2, 0xFF),
        ):
            board.write(address, size, value)
            self.assertEqual(board.read(address, size), value)
        with self.assertRaises(HardwareError):
            board.read(0xBD040C10, 4)
        with self.assertRaises(HardwareError):
            board.write(UART_STATUS, 2, 0)

    def test_bus_monitor_starts_without_a_fault(self):
        board = BootHardware()
        board.write(BUS_MONITOR_ENABLE, 2, 1)
        self.assertEqual(board.read(BUS_MONITOR_ENABLE, 2), 1)
        self.assertEqual(board.read(BUS_MONITOR_STATUS, 2), 0)
        self.assertEqual(board.read(BUS_MONITOR_ADDRESS, 4), 0)
        with self.assertRaises(HardwareError):
            board.write(BUS_MONITOR_STATUS, 2, 1)

    def test_reset_retention_starts_cold_and_retains_writes(self):
        board = BootHardware()
        self.assertEqual(board.read(RESET_RETENTION, 2), 0)
        board.write(RESET_RETENTION, 2, 0x1234)
        board.advance(100)
        self.assertEqual(board.read(RESET_RETENTION, 2), 0x1234)

    def test_watchdog_config_is_retained_but_unsupported_enable_stops(self):
        board = BootHardware()
        board.write(WATCHDOG_CONTROL, 2, 0xC)
        board.write(WATCHDOG_COUNT, 4, 0xFFFFFFFF)
        board.write(WATCHDOG_RELOAD, 4, 12000000)
        self.assertEqual(board.read(WATCHDOG_COUNT, 4), 0xFFFFFFFF)
        self.assertEqual(board.read(WATCHDOG_RELOAD, 4), 12000000)
        with self.assertRaisesRegex(HardwareError, 'watchdog enable'):
            board.write(WATCHDOG_CONTROL, 2, 3)
        self.assertEqual(board.read(WATCHDOG_CONTROL, 2), 0xC)

    def test_micci_routes_to_explicit_endpoint_and_completes_on_board_time(self):
        base = 0xBD300800
        board = BootHardware(micci_devices={base: {(0, 6): RegisterDevice(0x8014)}})
        board.write(base, 2, 0x2400)
        board.write(base + 0xAC, 2, (6 << 13) | 0x20 | 0xF)
        self.assertTrue(board.read(base + 0xB0, 4) & 0x80000000)
        board.advance(31)
        self.assertTrue(board.read(base + 0xB0, 4) & 0x80000000)
        board.advance()
        self.assertEqual(board.read(base + 0xB0, 4), 0x8014)
        self.assertIn(base & ~0xFFF, board.pages)

    def test_micci_default_profile_does_not_invent_external_identity(self):
        base = 0xBD300800
        board = BootHardware()
        board.write(base, 2, 0x2400)
        with self.assertRaisesRegex(HardwareError, 'unmodeled MICCI endpoint'):
            board.write(base + 0xAC, 2, (6 << 13) | 0x20 | 0xF)
        self.assertEqual(board.read(base + 0xB0, 4), 0)

    def test_gpio_routes_latches_and_requires_known_input_levels(self):
        board = BootHardware(gpio_inputs={5: 1})
        base = 0xBD0B2000
        self.assertIn(base, board.pages)
        self.assertEqual(board.read(base + 5 * 8, 2), 1)
        with self.assertRaisesRegex(HardwareError, 'input level is unknown'):
            board.read(base + 6 * 8, 2)
        board.write(base + 7 * 8 + 2, 2, 1)
        board.write(base + 7 * 8 + 4, 2, 1)
        self.assertEqual(board.read(base + 7 * 8, 2), 1)
        with self.assertRaisesRegex(HardwareError, 'read-only'):
            board.write(base + 7 * 8, 2, 0)

    def test_carriage_asic_and_micci_broadcast_mode(self):
        base = 0xBD300C00
        carriage = CarriageAsic(0x0801)
        board = BootHardware(micci_devices={base: {(1, 4): carriage}})
        # Broadcast mode (config[0] bit 14 = 0x4000) ignores writes to absent endpoints
        board.write(base, 2, 0x6400)
        board.write(base + 0x92, 2, 0x0004)
        board.write(base + 0x94, 2, (0 << 13) | 0x0000)
        board.advance(32)
        # Switch to normal mode (0x2400) and read CarriageAsic ID (bus=1, dev=4, reg=0x7D)
        board.write(base, 2, 0x2400)
        board.write(base + 0xAC, 2, (4 << 13) | (0x7D << 6) | 0x20 | (1 << 4) | 0xF)
        board.advance(32)
        self.assertEqual(board.read(base + 0xB0, 4), 0x0801)
        # Read Neo status register 0x00 (bus=1, dev=4, reg=0x00)
        board.write(base + 0xAC, 2, (4 << 13) | (0x00 << 6) | 0x20 | (1 << 4) | 0xF)
        board.advance(32)
        self.assertEqual(board.read(base + 0xB0, 4), 0x800E)
        # Write Pen ADC config and verify ready bit on reg 0x09
        carriage.write(0x09, 0x0200)
        self.assertEqual(carriage.read(0x09), 0x8300)

    def test_usb_device_controller_enumeration_and_dma(self):
        dma_ram = bytearray(256)
        dma_ram[0x20:0x32] = b'\x12\x01\x00\x02\x00\x00\x00\x40\xf0\x03\x54\x05\x00\x01\x01\x02\x03\x01'
        board = BootHardware(
            usb_dma_read=lambda addr, size: bytes(dma_ram[addr:addr + size]),
            usb_dma_write=lambda addr, data: dma_ram.__setitem__(slice(addr, addr + len(data)), data),
        )
        # Enable USB global event interrupts (Bus Reset + EP0 SETUP)
        board.write(0xBD09FC2A, 2, 0x0108)
        board.usb.bus_reset(high_speed=True)
        self.assertTrue(board.usb.attached)
        self.assertEqual(board.read(0xBD09FC04, 4), 0x00000401)
        self.assertTrue(board.read(0xBD09FC28, 2) & 0x0008)
        # Clear Bus Reset W1C
        board.write(0xBD09FC28, 2, 0x0008)
        self.assertEqual(board.read(0xBD09FC28, 2) & 0x0008, 0)

        # Deliver GET_DESCRIPTOR (Device, 18 bytes) SETUP packet
        setup = bytes([0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 0x12, 0x00])
        board.usb.send_setup(setup)
        self.assertTrue(board.read(0xBD09FC28, 2) & 0x0100)
        self.assertEqual(board.read(0xBD09FC10, 4), 0x80060001)
        self.assertEqual(board.read(0xBD09FC14, 4), 0x00001200)
        # Program EP0 DMA channel 0 and ACK via 0xBD09FC1A
        board.write(0xBD09BC24, 4, 0x20)
        board.write(0xBD09BC28, 4, 18)
        board.write(0xBD09BC2E, 2, 1)
        board.write(0xBD09FC1A, 2, 1)
        self.assertEqual(len(board.usb.control_transfers), 1)
        self.assertEqual(board.usb.control_transfers[0].response, bytes(dma_ram[0x20:0x32]))
        self.assertFalse(board.usb.control_transfers[0].stalled)

        # Deliver SET_ADDRESS (address = 7) in SIE interrupt mode
        board.usb.send_setup(bytes([0x00, 0x05, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00]))
        self.assertEqual(board.usb.device_address, 7)

    def test_sdio_host_and_bcm43143_backplane_and_wifi_ioctl(self):
        dma_ram = bytearray(4096)
        board = BootHardware(
            sdio_dma_read=lambda addr, size: bytes(dma_ram[addr:addr + size]),
            sdio_dma_write=lambda addr, data: dma_ram.__setitem__(slice(addr, addr + len(data)), data),
        )
        # Enable SDHCI command & transfer complete status bits
        board.write(SDIO_BASE + 0x34, 4, 0x01FF01FF)
        board.write(SDIO_BASE + 0x38, 4, 0x01FF01FF)

        def send_cmd(cmd_idx: int, arg: int, flags: int = 0x1A, mode: int = 0) -> int:
            board.write(SDIO_BASE + 0x08, 4, arg)
            board.write(SDIO_BASE + 0x0C, 4, (((cmd_idx << 8) | flags) << 16) | mode)
            board.write(SDIO_BASE + 0x30, 4, 0x00000003)
            return board.read(SDIO_BASE + 0x10, 4)

        # CMD5 (IO_SEND_OP_COND) -> 2 functions ready, 3.3V OCR
        resp5 = send_cmd(5, 0x00300000, flags=0x02)
        self.assertEqual(resp5, 0xA0FFFF00)
        # CMD3 (SEND_RELATIVE_ADDR) -> RCA 0x0001
        resp3 = send_cmd(3, 0)
        self.assertEqual(resp3 >> 16, 0x0001)
        # CMD52: Enable Function 1 & Function 2 in CCCR IO_EN (0x02 = 0x06)
        send_cmd(52, (1 << 31) | (0 << 28) | (1 << 27) | (0x02 << 9) | 0x06)
        self.assertEqual(board.sdio.card.f0[0x02], 0x06)
        # CMD52: Read CCCR IO_READY (0x03) -> 0x06
        r_ready = send_cmd(52, (0 << 31) | (0 << 28) | (0x03 << 9))
        self.assertEqual(r_ready & 0xFF, 0x06)

        # CMD53: Read first 4 bytes of EROM at backplane window 0x18001000 via Function 1
        board.write(SDIO_BASE + 0x00, 4, 0x100)
        board.write(SDIO_BASE + 0x04, 4, (1 << 16) | 4)
        send_cmd(53, (0 << 31) | (1 << 28) | (1 << 26) | ((0x08000 | 0x1000) << 9) | 4, mode=0x0013)
        self.assertEqual(struct.unpack('<I', dma_ram[0x100:0x104])[0], 0x4BF80001)

        # CMD53: Send Function 2 SDPCM + BCDC WLC_GET_VAR ("cur_etheraddr")
        payload = b'cur_etheraddr\x00\x00\x00'
        bcdc = struct.pack('<IIII', 262, len(payload), 0x00020000, 0) + payload
        total_len = 12 + len(bcdc)
        sdpcm = struct.pack('<HHBBBBHBB', total_len, (~total_len) & 0xFFFF, 1, 0, 0, 12, 0, 0, 0)
        frame = sdpcm + bcdc
        dma_ram[0x200:0x200 + len(frame)] = frame
        board.write(SDIO_BASE + 0x00, 4, 0x200)
        board.write(SDIO_BASE + 0x04, 4, (1 << 16) | len(frame))
        send_cmd(53, (1 << 31) | (2 << 28) | (1 << 26) | len(frame), mode=0x0003)

        # Read back Function 2 SDPCM + BCDC response
        board.write(SDIO_BASE + 0x00, 4, 0x400)
        board.write(SDIO_BASE + 0x04, 4, (1 << 16) | 64)
        send_cmd(53, (0 << 31) | (2 << 28) | (1 << 26) | 64, mode=0x0013)
        rx_len, rx_ck = struct.unpack_from('<HH', dma_ram, 0x400)
        self.assertEqual(rx_len ^ rx_ck, 0xFFFF)
        data_off = dma_ram[0x400 + 7]
        self.assertEqual(bytes(dma_ram[0x400 + data_off + 16:0x400 + data_off + 22]), bytes(board.sdio.card.mac_address))

    def test_dma_checksum_offload_and_sdio_data_packets(self):
        dma_ram = bytearray(512)
        # IGMPv2 Membership Report for 224.0.1.60 (type=0x16, code=0x00, cksum=0x0000, group=0xE000013C)
        igmp = bytes([0x16, 0x00, 0x00, 0x00, 0xE0, 0x00, 0x01, 0x3C])
        dma_ram[0x40:0x48] = igmp
        board = BootHardware(
            sdio_dma_read=lambda addr, size: bytes(dma_ram[addr:addr + size]),
            sdio_dma_write=lambda addr, data: dma_ram.__setitem__(slice(addr, addr + len(data)), data),
        )
        # Reset & seed checksum accumulator at 0xBD0B1000..0xBD0B1010
        board.write(0xBD0B1002, 2, 0x0001)
        board.write(0xBD0B1002, 2, 0x0010)
        board.write(0xBD0B1010, 4, 0)
        # Accumulate 8-byte buffer at 0x40
        board.write(0xBD0B1008, 4, 0x40)
        board.write(0xBD0B100C, 2, 8)
        board.write(0xBD0B1002, 2, 0x0006)
        self.assertEqual(board.read(0xBD0B1004, 2) & 1, 1)
        board.write(0xBD0B1002, 2, 0x0010)
        # Finalize checksum and verify (~sum & 0xFFFF == 0x08C3)
        board.write(0xBD0B1002, 2, 0x0024)
        self.assertEqual(board.read(0xBD0B1004, 2) & 1, 1)
        folded = board.read(0xBD0B1010, 4)
        self.assertEqual((~folded) & 0xFFFF, 0x08C3)

        # Transmit Ethernet II frame on SDIO Function 2 (SDPCM channel 2 + 4-byte BDC header)
        eth_frame = b'\xff\xff\xff\xff\xff\xff\x00\x04\xea\x82\x05\x0b\x08\x06' + bytes(46)
        bdc_hdr = bytes([0x20, 0x00, 0x00, 0x00])
        total_len = 12 + len(bdc_hdr) + len(eth_frame)
        sdpcm = struct.pack('<HHBBBBHBB', total_len, (~total_len) & 0xFFFF, 2, 2, 0, 12, 0, 0, 0)
        pkt = sdpcm + bdc_hdr + eth_frame
        dma_ram[0x80:0x80 + len(pkt)] = pkt
        board.write(SDIO_BASE + 0x00, 4, 0x80)
        board.write(SDIO_BASE + 0x04, 4, (1 << 16) | len(pkt))
        board.write(SDIO_BASE + 0x08, 4, (1 << 31) | (2 << 28) | (1 << 26) | len(pkt))
        board.write(SDIO_BASE + 0x0C, 4, (((53 << 8) | 0x1A) << 16) | 0x0003)
        self.assertEqual(len(board.sdio.card.tx_packets), 1)
        self.assertEqual(board.sdio.card.tx_packets[0], eth_frame)

    def test_lhcfifo_and_tierdone_diagnostic_registers(self):
        board = BootHardware()
        lhcfifo_regs = (
            0xBD066402, 0xBD066404, 0xBD066406, 0xBD066408,
            0xBD071C00, 0xBD071C18, 0xBD071C1A,
            0xBD072000, 0xBD072002, 0xBD072004,
            0xBD073000, 0xBD073002,
            0xBD073412, 0xBD073414, 0xBD073416,
        )
        for idx, addr in enumerate(lhcfifo_regs, start=1):
            self.assertEqual(board.read(addr, 2), 0)
            board.write(addr, 2, 0x1000 + idx)
            self.assertEqual(board.read(addr, 2), 0x1000 + idx)

    def test_watchdog_reload_secondary_timer_and_cancelled_event_compaction(self):
        board = BootHardware(timer_step=1, us_ticks=4)
        # 1. Watchdog service bit (0x04) reschedules an already-enabled stage 0 timer
        board.write(0xBD060002, 2, 1)
        board.write(WATCHDOG_COUNT, 4, 1800)   # 10 us = 40 ticks
        board.write(WATCHDOG_RELOAD, 4, 900)
        board.write(WATCHDOG_CONTROL, 2, 0x01)
        board.advance(25)
        self.assertFalse(bool(board.irq_raw_sources & (1 << 5)))
        # Service stage 0 (bit 2 = 0x04) while keeping stage 0 enabled (bit 0 = 0x01)
        board.write(WATCHDOG_CONTROL, 2, 0x05)
        board.advance(25)
        # Without reload rescheduling, stage 0 would have expired at 40 ticks (or never)
        self.assertFalse(bool(board.irq_raw_sources & (1 << 5)))
        board.advance(15)
        self.assertTrue(bool(board.irq_raw_sources & (1 << 5)))

        # 2. Secondary timer pending register (0xBD301010) read + W1C and enable (0xBD30100E)
        board._sec_timer_pending = 0x07
        board.write(0xBD30100E, 2, 0x07)
        self.assertEqual(board.read(0xBD301010, 2), 0x07)
        self.assertEqual((board.irq_raw_sources >> 10) & 7, 7)
        board.write(0xBD301010, 2, 0x03)
        self.assertEqual(board.read(0xBD301010, 2), 0x04)
        self.assertEqual((board.irq_raw_sources >> 10) & 7, 1)  # Only ch 2 -> IRQ 10 remains

        # 3. Repeatedly rescheduling a far-future timer channel behind an earlier channel compacts _events
        board.write(0xBD04081E, 2, 10)        # ch 0 at 40 ticks
        for delay in range(100, 200):
            board.write(0xBD040822, 2, delay) # ch 1 rescheduled 100 times
        board._prune_cancelled_events()
        self.assertLessEqual(len(board._events), 32)


if __name__ == '__main__':
    unittest.main()


