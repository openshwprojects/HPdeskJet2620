# Settings EEPROM, original contents (read 2026-11-06)

`eeprom-m24512.bin`: the complete 64 KiB of the printer's ST M24512 settings
EEPROM (I2C `0x50`), read with the custom firmware (`python -m fw.tool nvm`,
`/api/eeprom`). The custom firmware never writes this chip, so these are the
bytes HP's firmware left: board ID, serial number, error log, the NVM3 store
(page counts, pen history, network settings, ...). SHA-256 starts `e78bf355`.

`chip-id.txt`: the four SoC ID words `0xBD040010..1C` of this printer. HP
derives the key for the encrypted NVM3 values from them; with it,
`analysis/nvm_decode.py` decrypts 478 values, and 214 of 220 multi-byte values
equal what HP's own firmware reported (`--crosscheck`), which confirms the key.

```
python -m analysis.nvm_decode references/physical-eeprom/eeprom-m24512.bin --chip-id 00400000,059329f7,822cb810,503dad3e --crosscheck
```

Format: `analysis/nvm.md`. The older 64 KiB RAM mirror of HP's firmware
(`../physical-51-nvm-mirror/`, 2026-10-29) has the same layout; the two differ
where HP's firmware kept writing until it was replaced.
