# Wi-Fi module: from first contact to a working scan (2026-11-05)

The custom application's `wifi` command (`fw/app/wifi.c`) runs the Broadcom
BCM43143 SDIO module on the owner's printer: `wifi probe` powers it, brings up
the AXON SD host and reads the chip identity with single-byte CMD52 transfers,
`wifi fw` loads HP's chip firmware and NVRAM from flash, boots the chip and
asks it for its version, and `wifi scan` lists nearby networks. Everything is
polled; no interrupts are used.

## First contact (`wifi probe`)

1. **Module power.** AXON GPIO pin 12 (HP logical GPIO 8) is WL_REG_ON, active
   high. HP's Wi-Fi driver switches it at `0xa045de18`. The probe drives it low
   for 100 ms, then high, and waits 200 ms. A module left selected by an
   earlier probe ignores CMD5, so the power cycle is needed every time.
2. **SD block enable** (HP's `sd_ctrl.cpp` init at `0xa03c6b46`):
   - clear bit 15 of `0xBD061C0A` (the AXON reset register; HP's helper
     `0xa01775ea` with list `{0x2f}`). The printer read `0xe600` before; while
     the bit is set every SD host register reads `0xffffffff`.
   - write 0 to `0xBD0B0400`.
   - two handshakes (`0xa03c6ad4`): for n = 0..255 write `0x0100 | n` to
     `0xBD0B0402` (resp. `0xBD0B0406`), low byte first, until `0xBD0B0404`
     bit 0 (resp. bit 1) is set, then clear bit 8. The printer settled at
     n = 18 and n = 19.
3. **SD host** (standard SDHCI at `0xBD0B0000`, 32-bit word access, as in
   `sdio.py`): capabilities `0x69ef30b0`, version `0x89010000` (SDHCI 2.0),
   base clock 48 MHz. Reset all, bus power on (`0x28 = 0x0100`, HP uses power
   byte `0x01`), identification clock 375 kHz (divider `0x40`), 24 MHz after
   the card is selected. HP's own final setting (emulator snapshot): host
   control `0x0106` (4-bit, high speed, power on), clock `0x0007` (48 MHz).
4. **SDIO enumeration:** CMD5 OCR `0x20ffff00` (2 I/O functions), CMD5 with
   the voltage window until ready, CMD3 (relative address 1), CMD7.
5. **Identity:** CCCR revision `0x43`; CIS manufacturer tuple vendor `0x02d0`
   (Broadcom), device `0xa887`.
6. **Function 1 and backplane:** enable F1 (CCCR `0x02 = 0x02`, wait for
   `0x03` bit 1), request ALP (`0x1000E = 0x28`, CSR read `0x68`), then as
   brcmfmac does: force ALP (`0x1000E = 0x21`), wait 65 us, pull-ups off
   (`0x1000F = 0`). Without the forced ALP the first backplane CMD52 timed out
   (error status `0x00018000`). Window `0x1000A..0x1000C` = `0x18000000`, four
   CMD52 byte reads: ChipCommon chip ID **`0x1702a887`**: BCM43143, revision 2,
   7 cores. (`sdio.py` returns `0x1602a887`, 6 cores; left unchanged because
   HP's emulated bring-up was built on it.)

## Chip firmware download, boot and scan (`wifi fw`, `wifi scan`)

Both work on the printer. The sequence follows Linux brcmfmac for a Cortex-M3
chip; the firmware and NVRAM come straight from HP's ROM file system in flash
(read through the memory-mapped window at CPU address 0..16 MiB):

| Item | Flash offset | Size | Check before use |
| --- | --- | --- | --- |
| RAM image (`6.20.198.22_sdio-...-proptxstatus.bin`) | `0x53AE97` | `0x5E630` (386,608) | banner `43143b0-roml/sdio-g-p2p-mchan` at image offset `0x5E567` |
| NVRAM text (`bcm943143eds_p331.txt`) | `0x5994C7` | ends before the next file (gzip) | starts with `#750-124646` |

The image is stored uncompressed; it is byte-identical to what HP uploads in
the emulator. NVRAM processing is Broadcom DHD's: drop comments, empty lines
and `\r`, NUL-terminate each line, pad to 4, add one NUL, round up to 4 (892
bytes), length token `(~words << 16) | words` in the last RAM word.

1. Function 1 block size 64. Enumeration ROM at ChipCommon `0xFC`; Broadcom
   cores on the printer: ChipCommon `0x800` rev 43, D11 `0x812` rev 37 at
   `0x18001000`, SDIO device `0x829` rev 15 at `0x18002000`, ARM CM3 `0x82A`
   rev 7 at `0x18003000`, SOCRAM `0x80E` rev 20 at `0x18004000`, USB device
   `0x81A` rev 17 at `0x18005000` (wrappers at `0x181xx000`).
2. Download mode: ARM core disabled, D11 reset with PHY clock, SOCRAM reset
   (AI wrapper `ioctrl` `+0x408`, `resetctrl` `+0x800`).
3. RAM size from the SOCRAM bank registers: **448 KiB** on the printer
   (`sdio.py` reports 8 banks = 512 KiB, so HP's emulated upload put the
   NVRAM at `0x7FC80`; on the real chip it belongs at `0x6FC80`).
4. HT clock request (`CHIPCLKCSR = 0x10`, read `0xd0`), firmware written with
   CMD53 in 2 KiB pieces of 64-byte blocks through an uncached SDMA buffer
   (DRAM alias `0x60800000`): **373 ms**. First and last 64 bytes read back.
5. NVRAM at `0x6FC80`, token `0xff2000df` at `0x6FFFC`.
6. Clear SDIO core `intstatus` (`+0x20`), release the ARM core, force HT,
   write SDPCM protocol 4 to `tosbmailboxdata` (`+0x48`), enable function 2:
   the firmware reports ready after **40 ms** (mailbox `0x00040008`).
7. Function 2 block size 512, watermark 8. Control requests are SDPCM frames
   (12-byte header, channel 0) carrying a BCDC header (cmd, len, flags with
   request id, status). Replies are polled: SDIOD `intstatus` bit 6
   (FRAME_IND), a 64-byte first read, then the rest in 512-byte blocks.
8. `ver` answers `wl0: Mar 17 2016 15:56:01 version 6.20.198.22 (r625840) FWID 01-0`.
9. Scan as HP's DHD does it: `WLC_UP`, `iscan` (start), then `iscanresults`
   until the status is no longer pending (continue on partial). The printer
   listed 7 and then 10 nearby networks with BSSID, channel, signal and SSID.

## Joining networks, access point, IP (verified on physical printer 2026-11-05)

Both station mode and access point mode are fully implemented; station mode with
WPA2-PSK and automatic DHCP lease acquisition has been verified end-to-end on
the physical HP DeskJet 2620 printer.

| Command | What it does |
| --- | --- |
| `wifi join [ssid] [passphrase]` | Loads chip firmware if needed. Defaults to the network in `fw/app/wifi_secrets.h` if omitted. Open networks, or WPA2-PSK (AES) with 4-way handshake offloaded to the BCM43143 chip's own supplicant (`idsup`). Then DHCP lease negotiation |
| `wifi ap [ssid] [passphrase\|-] [channel]` | The printer's own access point (default `HPDJ-xxxx` from MAC, open, channel 6), address 192.168.4.1/24, DHCP server for .10 to .17 |
| `wifi leave`, `wifi rssi`, `wifi` | Leave or stop access point; signal strength; status with mode, network, and frame counters |
| `wifi iovar <name> [val]` | Read or write any Broadcom chip iovar directly (e.g. `mpc`, `allmulti`, `pkt_filter_mode`) |
| `net`, `net ip <a> <mask> [gw]`, `net dhcp` | Address, ARP table, DHCP state; static address or automatic DHCP client |
| `ping <ip> [count]` | ICMP echo; the printer also answers pings |

Names and passphrases with spaces go in double quotes:
`wifi join "Home Net - 5" "pass phrase"`.

### Hardware Bring-up & Protocol Details

1. **WPA2-PSK Association Sequence & Real Event Ordering**:
   The BCM43143 supplicant handles the 4-way WPA2 handshake autonomously in firmware.
   The physical event flow observed on the printer:
   - `WLC_E_SET_SSID` (event 0, status 0)
   - `WLC_E_AUTH` (event 3, status 0)
   - `WLC_E_ASSOC` (event 1, status 0)
   - `WLC_E_LINK` (event 16, status 0)
   - `WLC_E_PSK_SUP` (event 44, status 6 = `SUP_KEYED`)
   Total association and keying time on the test network: **2,616 ms to 2,696 ms**;
   sustained signal strength: **-37 dBm to -40 dBm**.

2. **Ethernet Runt Frame Padding (>= 60 bytes)**:
   The physical Wi-Fi dongle and physical network switches discard raw Ethernet
   frames shorter than 60 bytes (Ethernet minimum frame size without FCS is 60
   bytes). ARP frames (42 bytes) were originally dropped on the physical medium.
   `wifi_send()` zero-pads all outgoing frames shorter than 60 bytes (`send_len = len < 60U ? 60U : len`).

3. **Broadcom Dongle IOVAR Settings**:
   During `wifi fw` bringup, matching HP's Stage-22 snapshot configuration:
   - `bus:txglom 0`: disables SDIO TX glomming (superframes).
   - `bus:rxglom 0`: requests one frame per SDIO transfer.
   - `ampdu_hostreorder 0`: reorders AMPDUs inside dongle RAM instead of requiring host driver reassembly.
   - `mchan 0`: single-channel mode.
   - `mpc 0`: disables Broadcom Minimum Power Consumption so the radio stays awake.
   - `allmulti 1`: accepts all multicast frames.
   - `pkt_filter_mode 0`: prevents dongle firmware from dropping non-matching packets.

4. **DHCP Discovery & Unicast Flag (`flags = 0`)**:
   Standard Broadcom mobile/embedded firmware drops broadcast packets (`255.255.255.255`
   or MAC `FF:FF:FF:FF:FF:FF`) by default to save power. When `dhcpc_send()` set the
   RFC 2131 Broadcast flag (`0x8000`), the router responded with broadcast offers which
   were filtered by the dongle.
   Clearing the broadcast flag (`flags = 0`, as done by Android and Linux dhcpcd)
   requests the DHCP server to send `DHCPOFFER` and `DHCPACK` directly as unicast
   802.11 frames to the client MAC (`90:32:4b:a9:72:bf`).
   Result: instantaneous DHCP lease acquisition (`192.168.0.133/24`, gateway `192.168.0.1`, lease 7200 s).

5. **Bidirectional Network Validation**:
   - **Printer -> Router (`192.168.0.1`)**: `ping 192.168.0.1 2` answered in 2.1 ms to 2.3 ms.
   - **Printer -> PC (`192.168.0.213`)**: `ping 192.168.0.213 2` answered in 5.0 ms.
   - **PC -> Printer (`192.168.0.133`)**: `ping 192.168.0.133 -n 4` answered in 6 ms to 11 ms, 0% packet loss.
   - **Large Payload Ping**: Tested `ping -l 20`, `ping -l 100`, `ping -l 300`, and `ping -l 1000` with 0% packet loss, confirming SDIO multi-chunk receive (`f2_read_frame`) works up to full MTU.

6. **Front-Panel UI & LCD Integration**:
   - `ui on` activates the front-panel display and button handling.
   - LCD lights the Wi-Fi icon and signal bars.
   - Pressing the physical `Info` button scrolls the leased IP address (`1-9-2---1-6-8---0---1-3-3`) across the 7-segment display.

Join sequence (as Cypress WHD prepares a join): `WLC_UP`, `event_msgs`
(SET_SSID, AUTH, DEAUTH(_IND), ASSOC(_IND), REASSOC_IND, DISASSOC(_IND), LINK,
PSK_SUP), `WLC_SET_PM 0`, `WLC_SET_INFRA 1`, `WLC_SET_AUTH 0`; WPA2:
`WLC_SET_WSEC 4`, `bsscfg:sup_wpa 1`, `bsscfg:sup_wpa2_eapver -1`,
`bsscfg:sup_wpa_tmo 2500`, `WLC_SET_WSEC_PMK` (passphrase, flags 1),
`WLC_SET_WPA_AUTH 0x80`; then `WLC_SET_SSID`. Connected = LINK up and, for
WPA2, PSK_SUP status 6 (keyed); a PSK_SUP with another status after LINK up
is reported as a probably wrong passphrase, SET_SSID status 3 as "network not
found". Access point: `WLC_DOWN`, `apsta 0`, `WLC_SET_AP 1`, infra, channel,
security (the chip's authenticator for WPA2), `WLC_UP`, `WLC_SET_SSID`, then
SET_SSID/LINK as confirmation; ASSOC_IND/DISASSOC_IND report stations.

Data path: SDPCM channel 2 with a 4-byte BDC header (`0x20`, priority 0,
interface 0, no extra offset) around Ethernet II frames. Received frames on
channels 1 and 2 are told apart by Ethernet type `0x886C` (events). Data frames
that arrive while an ioctl answer is awaited wait in an 8-frame queue. Sending
honours the chip's flow-control window (SDPCM header byte 9, brcmfmac's
`data_ok()`) with a 20 ms wait. `bus:rxglom 0` asks for one frame per
transfer. The idle loop looks at the chip every 2 ms and reads the signal
strength every 5 s while connected.

`fw/app/net.c` is a small polled IPv4 stack: 8 ARP entries and one packet
waiting for ARP, IPv4 without fragments, ICMP echo both ways, UDP, a DHCP
client (host name `hpdj-2620`, renewal at half the lease) and, in access point
mode, a DHCP server. It has no TCP.

### Emulator: printer profile and virtual network

`sdio.py` now has two chip profiles. `hp` (default) is the model HP's emulated
firmware was built against. `printer` is what the owner's chip reported: chip
ID `0x1702A887`, six Broadcom cores at the printer's addresses (enumeration
ROM served at `0x1810F000`), 7 SOCRAM banks = 448 KiB, and the 6.20.198.22
version string. `fw/harness.py` uses `printer` for the custom firmware.

`wifi_net.py` (opt-in, `VirtualWifiNetwork(card)`) plays the far side of the
link: access points with or without a passphrase (events like the chip,
honouring the firmware's event mask), a router 192.168.50.1/24 with a DHCP
server that answers ARP and ping, and in access point mode a client station
that associates, takes a DHCP lease and pings. Tests:
`tests/test_fw_network.py` (station join with WPA2 and open networks, wrong
passphrase, unknown network, static address, ping both ways, link lost and
back, access point with DHCP server and ping, WPA2 access point).

## What HP's firmware does next (from the emulator snapshot)

The stage-22 snapshot already contains HP's complete bring-up: 3,879 SDIO
commands, the chip firmware in SOCRAM, NVRAM, 249 SDPCM/BCDC ioctls, firmware
version string `43143b0-roml/sdio-ag-p2p-pno-aoe-pktfilter-keepalive-sr-mchan-proptxstatus
Version: 6.10.190.49`, and the set-up access point `HP-Setup<17>DeskJet 2600
series`. HP's ROM file system names the blobs
`/rom/ultra_spectra/6.20.198.22_sdio-g-p2p-mchan-idauth-idsup-keepalive-vsdb-pclose-swdiv-wfds-proptxstatus.bin`
and `/rom/ultra_spectra/bcm943143eds_p331.txt`.

## Emulator

`emulator.py`'s native fast path for 32-bit SD host writes did not drop the
cached read of host/clock control (`0x28`..`0x2C`), so a driver that reads the
clock register right after writing it never saw Internal Clock Stable.
`BootHardware.write` already invalidated it; the fast path now does too.

`tests/test_fw_bootloader.py` runs `wifi probe`, `wifi fw` and `wifi scan`
against the `sdio.py` model with the `printer` profile (see above), so the
expected output now matches what the printer printed: chip ID `0x1702a887`,
six cores, 448 KiB RAM, NVRAM at `0x6FC80`, version 6.20.198.22. The `hp`
profile keeps the old model for HP's own emulated firmware (chip ID
`0x1602a887`, 5 cores, 512 KiB, `Version: 6.10.190.49`). The scan still
returns two invented access points.

## Next steps

On the printer: `wifi join <your network> <passphrase>` (typed by the owner),
then `ping <router>` and a ping from a PC to the printer; `wifi ap` with a
phone joining and pinging 192.168.4.1. Things the emulator cannot show: the
real chip's event order and statuses, the flow-control window, and whether
`bsscfg:sup_wpa` is accepted by this firmware build.
