
# HP DeskJet 2620 Stock Firmware — Update / Flash-Writer Path: Definitive Integrated Analysis

**Scope:** static reverse engineering of the owner's own 16 MiB SPI flash image (`dump.bin`, SHA-256 `b791436c…4c496`, confirmed this session). Synthesis of five subsystem reports, each adversarially verified; every claim below is graded, and anything a verifier refuted or could not reproduce has been dropped or downgraded.

**What I re-verified independently this session (from the bytes, not from the reports):**
- `dump.bin` hash matches the stated SHA-256; image is exactly 16 MiB.
- Magic census over the full 16 MiB: container `3CA55A3C` at `0x20034` (once), footer `3CA55A7D` at `0xFFFF94` (once), GHS `7F474853` at `0xC93E98` (once) — exactly the reported singletons.
- The resident SPI transfer helper **A000CEBA** disassembles as: `r5 = 0xBD063800`; control at `[r5,#8]`; opcode byte written to TX via `lsls r0,#0x18; str r0,[r5,#0x14]`; status polled at `[r5,#0]` (extract bits 15:11). CS helper **A000CEFA** uses `[r5,#8]` mask `0xFFFFF3FF`.
- The identify routine **A000CF12** feeds the helper **only** opcodes `0x85`, `0x9F`, `0xAB` (plus zero address/dummy bytes). No `0x06`/`0x02`/`0x20`/`0xD8`/`0xC7`/`0x05` anywhere in it.
- Extracted base-image entry = **`0xA04E2E05`** (confirms the running static image is the *base*, pre-GHS, image).
- `spi.py` already contains an opt-in, datasheet-faithful W25Q128JV program/erase model (default immutable), with `tests/test_spi_write.py` exercising it — relevant to Section 7.

A general note that the verifiers landed on and that I confirm: **the absolute negative ("no writer in the image") is conditional on the premise that any SPI programming must go through controller `0xBD063800`.** NOR flash read happens through a memory-mapped window aperture that physically cannot program the part. Both reports' and both verifiers' scans found the controller base at exactly seven literal-pool sites, all inside the boot loader's read driver, and nowhere else in the extracted runtime image. The premise holds, so the negative holds — but it is a premise, stated here explicitly.

---

## 1. The update pipeline, end to end

The stock "firmware update" is architecturally split into an **in-session orchestrator/receiver** (main application, runs from RAM) and a **boot-time committer** (boot loader). The main app never writes flash; commits happen at the next boot. Here is the pipeline with a grade per stage.

| # | Stage | Where (address / command) | Grade |
|---|-------|---------------------------|-------|
| 1 | **Delivery transport** | Two genuine firmware-ingress paths exist: **(a) boot-loader USB recovery S-record loader** `nos_loader_srec.c`, parser **A000B87C**, reader **A000B7CA** over USB device-controller MMIO `0xBD09FC00/FC04/FC28`, `0xBD09C408`; and **(b) network/web pull** via `rest_fw_update.setUpdateAction` (**A037F7D5**) kicking a WFWU download worker. The UART `udw.srec_download` command is **not** a transport (see §4). | (a) **proven** it is an S-record ingress; (b) **strong** |
| 2 | **Receive / buffer** | Web path: START (`A046F4AA`) queues download worker **A046F21C**; sink **A046F88B** buffers 0x2000-byte chunks into a RAM file-record store (table `0x20E02494`) under path `/ram/system/web_fw_update/manifest`. USB path: S-record bytes staged into DDR. | **strong** (web), **strong** (USB staging) |
| 3 | **Package validation** | Web/orchestrator path: **no on-device cryptographic verification located** (see §3). Boot GHS/footer path: magic + length + complement + board-match in validator **A000A60E**, then rolling checksum. USB S-record path: an **AuthHdr/AuthUpdate/AuthFinal** sequence with an `is_secure_boot` / `unicast_challenge` handshake exists in boot code — whether it *enforces* on this unit is unknown. | boot checksum/complement **proven**; USB auth **present but enforcement unproven**; web crypto **absent (proven negative for direct calls)** |
| 4 | **Flash stage / write** | **No resident SPI program/erase driver exists** (see §2). Web INSTALL (`A046DDBA`) only sets control-block flag `0x40`; it does not write flash. The physical bytes-to-flash writer is **not in this dump** — it is attributed to a downloaded, RAM-resident "hp reflash" agent executed by the boot recovery monitor (jump `blx A000FC80`). | writer **not found** (proven absent from image); downloaded-agent hypothesis **strong** |
| 5 | **NVM gate set** | Web INSTALL arms persistent bits: engine `vt[0]` (**A046FC12**) read-modify-writes nvm2 datastore key **0x125CE** (`|0x30|1`). Boot external-footer path sets the per-slot **odd** bit (**A000A5A6**) before apply. These are **I2C EEPROM** writes, not SPI-flash writes. | **strong** |
| 6 | **Reboot** | Commit is deferred to next boot by design (orchestrator defers everything). | **strong** |
| 7 | **Boot-time apply (GHS/RFIX)** | Loader `A000A9D0` scatter-loads base (`A000A85C`), runs RFIX callback (`0x00C93BD1`, informational), then applies GHS stream at `0x00C93E98` via applier **A000B42A**. **This is a RAM overlay, not a flash rewrite.** | **proven** |
| 8 | **Clear gate** | After `A000B42A` returns success, odd bit cleared (`A000A5A6`, A000ADE0), even bit set sticky (`A000A5E8`, A000ADF2). | **proven** |

The end-to-end story is coherent and matches the physical device: item-3 = `0xa9` → slot-0 even=1/odd=0 → the 2018 GHS overlay is applied every boot → device boots the updated entry `A04E3DD9`; the emulator's `0xAA` → slot-0 even=0/odd=1 → slot skipped → base entry `A04E2E05`.

**Critical caveat on stages 4–7:** the orchestrator "defers the real flash write to the boot GHS applier" narrative, which the first report leans on, is **partly a category error that the other reports correct.** The boot GHS applier **A000B42A is itself a RAM patcher that never writes flash.** So the chain "web INSTALL → reboot → boot applier writes 16 MiB flash" is **not** substantiated. What is substantiated: web INSTALL arms EEPROM bits, and the boot applier re-overlays RAM each boot. **Where a received web/USB blob actually gets programmed into SPI flash is the central unresolved gap** (see §2, §6).

---

## 2. Flash-write capability — the crux

**Verdict: no SPI program/erase driver exists anywhere in this image. The 16 MiB flash is effectively read-only to all resident code. (proven, independently reproduced three ways.)**

Evidence:
- The AXON SPI controller base `0xBD063800` (and `+4`/`+8`/`+0x14`/`+0x18`) appears as a literal at **exactly seven sites, all inside the boot loader read driver** (`A000D0B8/D0D0/D0DC/D0E4/D25C/D260/D2A0`), and nowhere in the 5 MiB main `.text`, `.rodata`, `.data`, or (per the reports' snapshot scan) anywhere in live DRAM except those loader literals. I reproduced the seven-site result on the extracted sections.
- The **only** code that drives the controller is the read/identify driver (**A000CEBA/CEFA/CE98/CF12/D07A**). Across its 19 transfer calls it issues **only** `0x85` (legacy READ-ID), `0x9F` (JEDEC), `0xAB` (release power-down) and zero bytes — **never** WREN `0x06`, Page-Program `0x02`, Sector/Block erase `0x20`/`0x52`/`0xD8`, Chip-erase `0xC7`/`0x60`, or WRSR `0x01`. I verified this opcode set directly from the disassembly.
- Flash **reads** are memory-mapped through the window aperture (window regs `0xBD063818 + slot*2`), not through read opcodes. A read aperture cannot program NOR flash.
- A full BL/BLX decode of the main `.text` finds **zero** calls into the SPI routines; they are called only from the loader's own discovery code.

**Granularity:** none present, because no writer is present. The resident **low-level primitives are callable** — `A000CEFA` (assert CS) + `A000CEBA` (shift one byte) demonstrate the exact TX/control/status protocol a writer would extend — but the write opcodes themselves are absent.

**Where the writer actually lives (strong inference, not in this dump):** the boot recovery "download monitor" (packet processor **A000C0BE**; load path **A000C440**; jump **`blx A000FC80`** with a descriptor carrying `reflash_io`/`image_size`/`flash_start_addr`/`image_location` and signature bytes `0xBE/0x41`) loads an **executable image into DDR (~0x60200000) and branches to it**. The presence of a `flash_start_addr` field strongly implies that downloaded "hp reflash" agent carries the real program/erase driver. That agent is delivered during an update and is **not baked into this image**. (Verifier corrections: `A000A316` is the enclosing function, not a loop — the infinite loop is at `A000A324/A000A328`, and the `0xA/0xC/3` command dispatch is at `A000A32A`; the substance is confirmed.)

---

## 3. Validation / crypto verdict

This is the single most important honesty point, and it is where the first orchestrator report overreached. Graded carefully:

**The field/web-update path performs NO on-device cryptographic signature verification that could be located. (proven as a negative for direct calls; strong overall.)**
- A BL/BLX decode of the WFU core (`0xA0469000–0xA0472000`), `rest_fw_update` (`0xA037E000–0xA0380400`) and the download store (`0xA046A000–0xA046C000`) makes **zero** calls into the crypto region (`0xA0288000–0xA0295000`). The SHA-256 implementation genuinely exists (constants `0x6a09e667/0xbb67ae85` at `A028E944`, confirmed by the verifier) but is **uncalled** by the updater — it serves TLS and ePrint device-identity signing.
- The manifest parser (**A046E9B6**) is plain line-by-line text parsing.
- The orchestrator status enum (`0xA0630000`) has no "verifying/validating/authenticating" state — only idle/checking/…/install-succeeded.

**The `manifest-not-auth` / `blob-not-auth` strings do NOT prove local signature checking. (downgraded from the first report's "implying signature/authorization checks.")** They are entries in the WFU error enum at `0xA06302E8`. Both the first report's own verifier and the package-validation report flag this as exactly the "auth-is-asserted-but-never-shown" trap. The honest reading: these are **protocol/authorization statuses whose authenticity derives from the TLS channel to HP's server**, not from a local image signature. No code backing a local check was found.

**The boot GHS/footer path is integrity-only — checksum + complement + board match, NOT cryptography. (proven.)**
- Validator **A000A60E**: magic `0x3CA55A7D`, length `0x6C`, complement `[+8] == ~[+0xC]` (verified `0x064501FF == ~0xF9BAFE00`), plus board-support match `M=0x06`/`F=0x45` and a board-number range via `A0009E10`.
- GHS stream integrity = a **rotate-left-7 rolling accumulator** at **A000AE88** (`ror #25` ≡ `rol #7`, then add byte) compared to stored word `0x8F9F727E` at GHS-header+0x1C, which a verifier recomputed and matched; plus opcode-4 per-range checksums (**A000B0CC**). The applier and its opcode table contain no hash/verify call.
- The RFIX footer's 128-bit "Sigs" words are **printed, never verified**.

**The one place authentication genuinely appears in code is the boot-loader USB S-record recovery path.** `nos_loader_srec.c` carries an `AuthHdr`/`AuthUpdate`/`AuthFinal` phase sequence and an `is_secure_boot` / `unicast_challenge` query-record handshake, with strings distinguishing `current code is secure` vs `unsecure` and `auth architecture is not enabled or unit is unsecure`. **Whether this gate is enabled and enforcing on this specific unit is unknown and is the pivotal open question** (§6).

**Net crypto verdict:** a custom image delivered through the **web/field** updater faces no local cryptographic gate (only TLS-channel trust and server-side authorization). A custom image delivered through the **USB S-record boot recovery** path faces a real auth architecture whose enforcement state is undetermined. The **GHS boot overlay** path is checksum/complement/board-match only — defeatable by construction, but it only patches RAM and presupposes you can already write the stream to flash.

---

## 4. Most promising native delivery route

Ranked by plausibility for the owner (who has UART + USB + network to their own device):

1. **HIGHEST — USB recovery-mode Motorola S-record loader in the boot loader** (`nos_loader_srec.c`, reader **A000B7CA**, parser **A000B87C**, over USB MMIO `0xBD09FC00…`). This is the only transport **proven to be an authenticated firmware ingress** that feeds the flash-committing boot stage, and it is the device's own recovery/un-brick mechanism. **Viability hinges entirely on whether this unit reports `unsecure` (auth architecture not enabled).** If unsecure, it is the realistic route; if secure, it needs valid HP signatures. Resolve non-destructively by sending the `is_secure_boot` query S-record over USB and reading the handshake reply.

2. **MEDIUM — crafted GHS overlay at the embedded staging slot `0x00B40000`** (from `container_header[0x20034]+0x68 = [0x2009C]`; fallback `0x00C40000`; both currently `0xFFFFFFFF`/erased). A valid GHS stream here would be applied to the RAM image every boot (raw/LZSS/byte-patch/fill/memmove/reloc opcodes can redirect the entry pointer) — **checksum-gated only, no signature.** But this path **presupposes the writer**: you must already be able to program those bytes into SPI flash and arm the NVM item-2/item-3 gate. It is the "apply + safety" half, not an ingress.

3. **LOW — web WFU** (`rest_fw_update.setUpdateAction` → WFWU worker). Server-pull of a presumed-signed package; only viable if the update server can be redirected **and** the package is not server-authenticated — both unlikely, and authenticity rests on TLS.

4. **DEAD END — `udw.srec_download` over the UART UDW shell** (`A03D90FC`). Despite the name it is an **inert stub**: it builds a 60-byte descriptor `{op=3, 0xFFFFFFFF, 0}`, hands it to worker `A02BE888` (which stages it at `0x20B2F09C` + flag `0x20B2F098=1`), and returns 0. It reads no bytes and writes no flash. The commit companion `A02BE8B4` acts only on staged op `0xa`/`0xc`; **op=3 is ignored.** Matches the physical probe (returns 0, no output).

5. **NOT FIRMWARE — `dat.download_*` / `im.download_*`** stage auxiliary data/imaging file-sets in RAM/heap; **DEAD END — `fm.flow_download` / `fm.lua_download`** load interpreter scripts to RAM and hang the UART shell (consistent with the MEMORY.md note that `fm.*` commands hang the shell; the "never to SPI" sub-claim is reasonable inference, not byte-proven — the weakest-evidenced item in the set).

**`smgr_init.force_reflash` (A03D9162, base image, confirmed)** is a trigger, not a transport: it reads smgr attribute `0x10022` via object `[0x20DB32A4]`, ORs in bit `0x20`, writes it back. It most plausibly **arms a reboot-into-recovery/reflash path** — the consumer of that bit was not traced and is an open question. (Note: the `A03DA0CB` address some reports gave for this was resolved against the GHS-*updated* image and is refuted; the base-image handler is `A03D9162`.)

---

## 5. Safety & recovery

**In-place vs staged:** every resident path is **non-destructive to SPI flash.**
- The boot GHS/RFIX update is a **RAM overlay re-applied each boot**; the base flash image, the baked GHS stream, and the footer are never rewritten.
- The web updater stages to a **RAM download store**; INSTALL only arms EEPROM bits.
- The crash guard is real and strong: the external-footer apply sets the per-slot **odd** bit *before* calling `A000B42A` and clears it only *after* success; the boot gate (`A000AC68…AC7A`) skips a slot iff **even=0 AND odd=1**. A half-applied/interrupted update therefore leaves even=0/odd=1 → that slot is **permanently skipped → clean fall-back to the untouched base image `A04E2E05`.** (proven mechanism.)

**Brick risk:**
- Through **native software paths: essentially nil**, precisely because no resident code can erase or program the boot flash. The device always re-reads the pristine image at reset.
- Recovery from a bad **overlay** is trivial: clear the gate by writing NVM item-3 = `0xAA` (or boot in load-mode 3), which touches only the **I2C EEPROM**, never the SPI flash.
- Brick risk appears **only if you supply your own writer** (RAM-resident agent orchestrating WREN/PP/erase against the W25Q128) and program the base image or footer. That bypasses the checksum-only boot validation and is unguarded.

**External backstop:** the chip is a standard Winbond **W25Q128** on a discrete SPI bus (the boot loader's own ID/geometry table at `A0011A98`, W25Q128BV entry `A0011F60`, confirms the part). The owner already holds a full dump. A bad flash is recoverable with an **external SPI programmer on the flash chip (the U5-class part)** — provided the custom image does not corrupt the small resident boot loader at flash `0x0`, which the update need not touch. The USB S-record recovery path is itself the device's intended un-brick mechanism: an owner who can enter it can also recover.

---

## 6. Open questions, ranked (blocking a confident physical attempt)

1. **Is this unit `secure` or `unsecure`?** The boot S-record loader's `is_secure_boot` handshake decides whether the authenticated USB ingress (route #1) accepts unsigned images. **The single most important unknown.** Resolvable non-destructively by sending the `is_secure_boot` query S-record over USB and reading the reply — recommend doing this first.
2. **How is USB recovery mode entered on this hardware?** Button chord at power-on, a `0xF001/0xF002` retained-scratch value (`0xBD04083C`, seen near `A000A2E8`), or a runtime command that reboots into the loader? Not determined statically.
3. **Where does a received web/USB blob physically get programmed into SPI flash?** The resident GHS applier is RAM-only; the runtime op-`0xa`/`0xc` commit cluster (`A024FF90` / `A02501A8` / `A02502B0`) was **not traced to the controller** and carries no `0xBD063800` literal. The writer is attributed to the downloaded "hp reflash" agent, which is absent from the dump. This is the gap that makes the pipeline not yet end-to-end.
4. **Does the downloaded reflash agent get authenticated before `blx A000FC80`?** The `[Boot] No AuthHdr:` string implies an optional AuthHdr, but whether a missing/invalid header blocks the jump or is merely logged was not confirmed.
5. **Does `force_reflash`'s bit-`0x20` (smgr attr `0x10022`) drive a reboot-into-RAM-updater path, and is it persisted to an NVM item?** The consumer of that flag was not traced.
6. **nvm2 key ↔ boot "item 3" identity.** The `0xAA → need-to-check` arithmetic lines up, and item-3 is proven to be the I2C-EEPROM `NOS_NVM_FWUPDATE_STATUS`, but the datastore key `0x125CE → record → physical-item-3 → EEPROM offset 0x4A` mapping was not byte-traced (0x4A comes from the `i2c.py` model, not the ROM descriptor table at `A0011924`, which sits outside the decompressed loader block).
7. **Full AXON controller register map / any hardware program mode.** Only `+0/+4/+8/+0x14/+0x18` are exercised by resident code; no complete register map was recovered.

**Dropped/corrected from the source reports (do not carry forward):**
- `get_nvm_state @ A0470F6D` and `force_reflash @ A03DA0CB` — **refuted**; both addresses land mid-instruction in the base image and the quoted disassembly is not reproducible (they were resolved against the GHS-updated `ghs/dram.bin`). Use the base-image addresses `A046FFCD` and `A03D9162`.
- `update_type` method is **`A046F5F4`** (vt[6]), not `A046F5E0`; it reads the **RAM control block (`0x20E0D850`+0x54)** primarily and only falls back to NVM `0x125CE` bits[9:8] via helper `A046F5E0`. "update_type is an NVM read" is both mis-addressed and oversimplified.
- "Stock web firmware update is a **signed** design" — **downgraded to unproven inference** (see §3).
- Any citation that "`spi.py` models read-only" — **stale** (see §7).

---

## 7. Emulation test plan (zero hardware risk)

The goal is to exercise the real write path in the emulator with no physical device. Good news: the endpoint half is **already built**.

**What already exists (verified this session):**
- `spi.py` `W25Q128JV` has an **opt-in `writable=True`** program/erase model faithful to W25Q128JV §8.2.12–8.2.19: Page-Program `0x02` (NOR AND semantics within a 256-byte page), Sector/Block erase `0x20`/`0x52`/`0xD8`, Chip-erase `0xC7`/`0x60`, each requiring prior WREN `0x06`, with a WEL latch that self-clears, and a `wip_read_latency` knob so a firmware busy-poll on RDSR `0x05` can be driven. Default stays **immutable** (`image` is `bytes`, every mutating opcode faults) preserving the read-only boundary.
- `tests/test_spi_write.py` already covers this endpoint and re-asserts default immutability.

**What to add / wire:**
1. **Emulator wiring.** `emulator.py:194` currently constructs `W25Q128JV(dump)` (immutable). Add a gated path to construct it with `writable=True` (and a chosen `wip_read_latency`) **only** under an explicit test/emulation flag, so the SPI controller model (`SPIController`, base `0xBD063800`, and `FlashWindow` at `window()`) routes program/erase to `_apply_write`. Keep the default immutable everywhere else.
2. **Controller-level coverage.** Add tests that drive program/erase **through the controller register interface** (`+8` control, `+0x14` TX, `+0` status, `+4` RX) rather than only the endpoint API, so the test mirrors how firmware would actually talk to `0xBD063800`. Reuse the resident protocol demonstrated by `A000CEBA`/`A000CEFA` as the reference sequence.
3. **Drive a real resident routine against the model, end to end (read side).** The only resident controller client is the identify/discovery path — invoke **`A000CF12`** (identify) and **`A000D07A`** (discovery) in the emulator and assert they read JEDEC `EF 40 18` / geometry correctly against the writable-but-read-only-driven model. This proves the controller model is faithful to what firmware expects.
4. **Exercise the staged-apply + gate handshake (no SPI write needed).** Drive the boot path in the emulator: set NVM item-3 to arming values and confirm the gate logic — `get_even` **A000A5CE**, `get_odd` **A000A58C**, `set_odd` **A000A5A6**, `set_even` **A000A5E8**, gate **A000AC68…AC7A**, apply lifecycle **A000AD9C…ADF6**, applier **A000B42A**. Verify the crash-guard invariants (odd-before-apply, even-after-success, even=0/odd=1 → skip). This path is pure RAM+EEPROM and carries no hardware risk.
5. **Synthesize the missing writer to prove the model accepts it.** Since no resident WREN/PP/erase exists, write a small **RAM-resident test stub** that performs `CS → WREN(0x06) → PP(0x02)+3 addr+data → poll RDSR(0x05) WIP → sector-erase(0x20)` against the controller model with `writable=True`, and assert the 16 MiB image mutates with correct NOR/erase semantics and WIP timing. This is the emulation stand-in for the absent downloaded "hp reflash" agent (§2) and validates that the controller+endpoint model would faithfully host such an agent.

**Entry addresses / commands a test should invoke:** `A000CF12` (identify), `A000D07A` (discovery), `A000B42A` (GHS apply), `A000A5CE`/`A000A58C`/`A000A5A6`/`A000A5E8` (gate bits), and the orchestrator chain `A037F7D5` (setUpdateAction) → `A037F132` (dispatcher) → `A046F4AA` (START) / `A046DDBA` (INSTALL) / `A046FC12` (arm NVM `0x125CE`). Driving `fm.*` routines is contraindicated (MEMORY.md: they hang the shell).

**What the emulator cannot prove:** because the actual flash-writer is not in the dump, no test can exercise HP's real program sequence — only a synthesized stand-in against a faithful controller model. Closing that gap requires either capturing the downloaded reflash agent during a live USB update, or resolving open question #3.

---

### Bottom line

The stock DeskJet 2620 firmware's "update" is a **receive-and-stage orchestrator plus a boot-time RAM-overlay applier**, not a self-contained flash writer. There is **no SPI program/erase code anywhere the running firmware can reach** (proven), so the native software paths **cannot brick the device and cannot by themselves persist custom firmware**. The field/web path has **no local cryptographic gate** (proven negative for direct calls); the boot GHS/footer path is **checksum/complement/board-match only** (proven). The one real authentication gate lives in the **boot-loader USB S-record recovery path**, and the single question that determines whether a custom image can be delivered natively is **whether this unit's secure-boot/auth architecture is enabled** — resolvable non-destructively via the `is_secure_boot` USB query. The actual bytes-to-flash writer is **not in this dump**; it is the downloaded "hp reflash" agent, and locating or capturing it (open question #3) is what still blocks an end-to-end native attempt. External recovery via an SPI programmer on the W25Q128 remains the reliable backstop throughout.