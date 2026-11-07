# I2C bus (2026-11-06)

One controller at `0xBD065800` (halfword registers, details and HP's driver
in `analysis/boot_hardware.md`; custom driver `fw/common/periph.c`). HP
initialises it from a 180 MHz reference for 200 kHz (`+0xC` = 449). A device
that does not answer leaves the controller busy until the driver times out and
resets it (HP checks exactly this with a dummy read of address `0x7F`).

`i2c scan` on the printer finds two devices:

| Address (7-bit) | Device | What it holds | Access by the custom firmware |
| --- | --- | --- | --- |
| `0x50` | ST M24512 EEPROM, 64 KiB, 16-bit word addresses | HP's settings ("NVM"): boot region with board ID `0x68a4`, serial number and hardware error log, then the NVM3 store (page counts, pen history, stall/jam counters, network settings; values wider than a byte encrypted with a key from the SoC ID). Format: `analysis/nvm.md`; original contents: `references/physical-eeprom/` | read only (`eeprom`, `/api/eeprom`, `fw.tool nvm`); never written |
| `0x1F` | "Undercroft" companion chip (HP's name, driver `undercroft.c` `0xA0468E3E..0xA0469CB8`, chip table `0xA0622C68`) | its own 256-byte NVM in 23 partitions; HP reports revision 2 (`references/README.md`). Command interface: command bytes written to sub-address `0x00`, response header at `0x80` (status) and `0x81` (payload length), payload from `0x82`. Opcodes from HP's driver: `0x62`/`0x20` echo self-test, `0x41` read NVM (with CRC), `0x42` write NVM, `0x00` device info / counter (20 bytes), `0xC9` MAC/challenge (18 bytes) | header read only: `i2c read 0x1f 0x80 8` gives `02 00 00 ff ...` (status 2, no payload) on the printer; no command has been sent |

The undercroft's MAC/challenge opcode and its NVM partitions suggest an
authentication or secure-storage role (for example cartridge or platform
authentication); that is an inference from the driver, not established.
The emulator models both devices (`i2c.py`: `EEPROM`, `Undercroft`).
