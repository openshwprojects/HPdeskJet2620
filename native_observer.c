/* Optional instruction accounting only. Unicorn still executes every opcode.
 * No firmware addresses, CPU opcode implementation, or device responses live
 * here. The Python bridge owns all architectural checks and device effects.
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

typedef void (*bridge_fn)(uint64_t, uint32_t, uint32_t);
typedef void (*mmio_bridge_fn)(int, uint64_t, int, int64_t);
typedef int (*stop_fn)(void *);
typedef int (*read_reg_fn)(void *, int, void *);
typedef int (*write_reg_fn)(void *, int, const void *);
typedef int (*read_mem_fn)(void *, uint64_t, void *, size_t);

#ifdef __GNUC__
#define UNLIKELY(x) __builtin_expect(!!(x), 0)
#else
#define UNLIKELY(x) (x)
#endif

#define CACHE_SETS 2048
#define CACHE_WAYS 4
#define CACHE_SLOTS (CACHE_SETS * CACHE_WAYS)
#define MAX_BLOCK_BYTES 1024
#define MAX_SPECIALS 32
#define MMIO_CACHE_SLOTS 64
#define EXEC_PAGE_SLOTS 4096

typedef struct {
    uint64_t address;
    uint64_t host_ptr;
    uint32_t generation;
    uint32_t size;
    uint32_t cpsr;
    uint16_t special_count;
    uint8_t cpsr_static;
    uint8_t _pad;
} BlockTag;

typedef struct {
    uint64_t specials[MAX_SPECIALS];
    uint32_t words[MAX_SPECIALS];
    uint8_t types[MAX_SPECIALS];
    uint8_t bytes[MAX_BLOCK_BYTES];
} BlockData;

typedef struct {
    uint64_t address;
    uint64_t mmio_gen;
    uint64_t host_ptr;
    uint32_t size;
    uint32_t value;
} MmioSlot;

typedef struct {
    uint64_t instructions;
    uint64_t completed;
    uint64_t limit;
    uint64_t deadline;
    uint64_t pending;
    uint64_t stopped;
    uint64_t user_access;
    uint64_t irq_mask;
    uint64_t recent_total;
    uint64_t recent[32];
    uint64_t special_count;
    uint64_t specials[MAX_SPECIALS];
    uint32_t words[MAX_SPECIALS];
    uint8_t types[MAX_SPECIALS];
    uint64_t all_special;
    uint64_t current_word, current_kind;
    uint64_t generation, cache_hits, cache_misses;
    uint32_t cpsr_register, spsr_register, lr_register, pc_register;
    uint32_t block_cpsr, cpsr_valid;
    uint64_t maintenance_count;
    uint64_t on_event;
    uint64_t flash_ptr, dram_ptr, itcm_ptr;
    uint64_t tcm_load_mode;
    uint64_t itcm_enabled, itcm_size, itcm_phys_size;
    uint64_t dtcm_enabled, dtcm_base, dtcm_end;
    uint64_t base_ticks, synced_completed, timer_step, timer_offset;
    uint64_t bd040_base_ptr, bd040_read_gen, cc8_write_gen, cc8_dirty;
    uint32_t cc8_value, timer_pending, irq_claim, fiq_claim;
    uint32_t max_irq_claim, max_fiq_claim, timer_mask0, timer_mask1;
    uint32_t timer_mask_union, nt_irq_claim, nt_fiq_claim, ack_802_dirty;
    uint32_t ack_802_value, timer_active_mask, timer_zeroed_mask, us_ticks;
    uint32_t timers_in_c, timer_ch_dirty_mask, timer_expired_mask, timer_val_dirty_mask;
    uint32_t timer_reload_zero_mask, _timer_pad, irq18_claim, fiq18_claim;
    uint32_t irq19_claim, fiq19_claim;
    uint16_t timer_last_val[8];
    uint64_t timer_ch_deadline[8];
    uint64_t timer_ch_period[8];
    uint64_t board_deadline;
    uint64_t next_timer_deadline;
    uint64_t bd0b3_base_ptr, uart0_fast_gen;
    uint32_t uart0_mask, uart0_mask_dirty, uart0_state, uart0_irq_claim;
    uint32_t uart_fifo_enabled, uart_frame_steps, uart_tx_active, uart_tx_bridge_mode;
    uint32_t uart_tx_pending_byte, uart_tx_write_count, uart_tx_out_count, uart_rx_head;
    uint32_t uart_rx_count, uart_tx_trigger_byte;
    uint64_t uart_tx_deadline;
    uint32_t uart_tx_write_pcs[256];
    uint32_t uart_tx_write_insns[256];
    uint32_t uart_tx_out_insns[256];
    uint16_t uart_tx_write_vals[256];
    uint8_t uart_tx_out_bytes[256];
    uint8_t uart_rx_buf[256];
    uint64_t bd0b1_base_ptr, panel_fast_gen, panel_shifted_output;
    uint32_t panel_output_word, panel_armed_strobe, panel_bit_count, panel_frame_bits;
    uint32_t panel_input_width, panel_latched_inputs, panel_signature, panel_reset_level;
    int32_t panel_tail_bit, panel_sampled_bit;
    uint32_t panel_write_count, _panel_pad;
    uint8_t panel_writes_buf[256];
    uint64_t bd0b0_base_ptr, sdio_fast_gen;
    uint32_t sdio_int_status, sdio_status_en, sdio_signal_en, sdio_resp0;
    uint32_t sdio_sdma_addr, sdio_blk_reg, sdio_arg, sdio_card_irq;
    uint32_t sdio_dirty, _sdio_pad;
    uint32_t wdog_enabled, wdog_pet_dirty;
    uint64_t wdog_pet_comp;
    uint64_t mmio_gen;
    bridge_fn bridge;
    mmio_bridge_fn mmio_bridge;
    stop_fn stop;
    read_reg_fn read_reg;
    write_reg_fn write_reg;
    read_mem_fn read_mem;
    MmioSlot mmio_cache[MMIO_CACHE_SLOTS];
    uint64_t exec_page_tag[EXEC_PAGE_SLOTS];
    BlockTag tags[CACHE_SLOTS];
    BlockData data[CACHE_SLOTS];
} Observer;

enum { SYNCHRONIZE = 1, LIMIT = 2, SPECIAL = 3, BLOCK = 4, INTERRUPT = 5, UART_TX_FLUSH = 6 };

EXPORT size_t observer_size(void) { return sizeof(Observer); }
EXPORT uint32_t observer_abi(void) { return 13; }

static inline uint32_t pick_claim(uint32_t c1, uint32_t c2)
{
    if (!c1) return c2;
    if (!c2) return c1;
    uint32_t p1 = c1 >> 8, p2 = c2 >> 8;
    if (p1 != p2) return (p1 > p2) ? c1 : c2;
    return ((c1 & 0xFFu) < (c2 & 0xFFu)) ? c1 : c2;
}

static inline int block_bytes_equal(const uint8_t *a, const uint8_t *b, uint32_t size)
{
    uint32_t i = 0;
    while (i + 8 <= size) {
        uint64_t va, vb;
        memcpy(&va, a + i, 8);
        memcpy(&vb, b + i, 8);
        if (va != vb) {
            return 0;
        }
        i += 8;
    }
    if (i + 4 <= size) {
        uint32_t va, vb;
        memcpy(&va, a + i, 4);
        memcpy(&vb, b + i, 4);
        if (va != vb) {
            return 0;
        }
        i += 4;
    }
    if (i + 2 <= size) {
        uint16_t va, vb;
        memcpy(&va, a + i, 2);
        memcpy(&vb, b + i, 2);
        if (va != vb) {
            return 0;
        }
        i += 2;
    }
    if (i < size && a[i] != b[i]) {
        return 0;
    }
    return 1;
}

static inline void copy_specials(
    uint64_t *dst_count, uint64_t *dst_addrs, uint32_t *dst_words, uint8_t *dst_types,
    uint64_t src_count, const uint64_t *src_addrs, const uint32_t *src_words, const uint8_t *src_types)
{
    size_t count = (size_t)src_count;
    *dst_count = count;
    if (count) {
        memcpy(dst_addrs, src_addrs, count * sizeof(uint64_t));
        memcpy(dst_words, src_words, count * sizeof(uint32_t));
        memcpy(dst_types, src_types, count);
    }
}

static inline const uint8_t *resolve_host_ptr(
    const Observer *s, uint64_t address, uint32_t size)
{
    if (s->tcm_load_mode || size == 0) {
        return NULL;
    }
    uint64_t end = address + size;
    if (s->dtcm_enabled && address < s->dtcm_end && end > s->dtcm_base) {
        return NULL;
    }
    if (s->itcm_enabled && address < s->itcm_size) {
        if (end <= s->itcm_size && s->itcm_phys_size != 0 && s->itcm_ptr != 0) {
            uint64_t off = address & (s->itcm_phys_size - 1);
            if (off + size <= s->itcm_phys_size) {
                return (const uint8_t *)(uintptr_t)s->itcm_ptr + off;
            }
        }
        return NULL;
    }
    if (end <= 0x01000000ULL && s->flash_ptr != 0) {
        return (const uint8_t *)(uintptr_t)s->flash_ptr + address;
    }
    uint64_t top = address >> 28;
    if ((top == 0x2 || top == 0x6 || top == 0xA) && s->dram_ptr != 0) {
        if ((address & 0x0FFFFFFFULL) + size <= 0x08000000ULL) {
            uint64_t dram_off = address & 0x03FFFFFFULL;
            if (dram_off + size <= 0x04000000ULL) {
                return (const uint8_t *)(uintptr_t)s->dram_ptr + dram_off;
            }
        }
    }
    return NULL;
}

static inline uint32_t normalize_cpsr(uint32_t raw_cpsr)
{
    /* All privileged ARM modes share identical MPU and decode semantics;
     * User mode (0x10) is kept distinct so privileged<->user transitions miss. */
    return (raw_cpsr & 0x01000220u) | (((raw_cpsr & 0x1Fu) == 0x10u) ? 0x10u : 0u);
}

static inline void apply_cache_hit(Observer *s, const BlockTag *tag, const BlockData *bd)
{
    ++s->cache_hits;
    s->all_special = 0;
    s->cpsr_valid = tag->cpsr_static;
    uint32_t sc = tag->special_count;
    s->special_count = sc;
    if (UNLIKELY(sc != 0)) {
        copy_specials(&s->special_count, s->specials, s->words, s->types,
                      sc, bd->specials, bd->words, bd->types);
    }
}

static inline void write_be16(uint8_t *dst, uint32_t v)
{
    dst[0] = (uint8_t)(v >> 8);
    dst[1] = (uint8_t)v;
}

static inline void write_be32(uint8_t *dst, uint32_t v)
{
    dst[0] = (uint8_t)(v >> 24);
    dst[1] = (uint8_t)(v >> 16);
    dst[2] = (uint8_t)(v >> 8);
    dst[3] = (uint8_t)v;
}

EXPORT void observer_block(void *uc, uint64_t address, uint32_t size, void *opaque)
{
    Observer *s = (Observer *)opaque;
    uint32_t raw_cpsr = 0;
    int cpsr_ok = 0;
    int cpsr_from_cache = 0;
    if (size > 0 && size <= MAX_BLOCK_BYTES) {
        if (s->cpsr_valid == 1u) {
            raw_cpsr = s->block_cpsr;
            cpsr_ok = 1;
            cpsr_from_cache = 1;
        } else if (s->cpsr_valid == 2u && ((address | size) & 2u) != 0u && ((address | size) & 1u) == 0u) {
            raw_cpsr = s->block_cpsr | 0x20u;
            s->block_cpsr = raw_cpsr;
            cpsr_ok = 1;
            cpsr_from_cache = 1;
        } else if (s->read_reg(uc, (int)s->cpsr_register, &raw_cpsr) == 0) {
            s->block_cpsr = raw_cpsr;
            cpsr_ok = 1;
        }
    }
    s->cpsr_valid = 0;
    uint32_t cpsr = normalize_cpsr(raw_cpsr);
    uint32_t gen32 = (uint32_t)s->generation;
    size_t set_base = (size_t)(((((address >> 1) * 2654435761ULL) ^ ((uint64_t)size * 2246822519ULL)) >> 16) & (CACHE_SETS - 1)) * CACHE_WAYS;
    BlockTag *set = &s->tags[set_base];
    if (cpsr_ok) {
        for (size_t way = 0; way < CACHE_WAYS; ++way) {
            BlockTag *tag = &set[way];
            if (tag->generation == gen32 && tag->address == address
                    && tag->size == size && tag->cpsr == cpsr && tag->host_ptr != 0) {
                BlockData *bd = &s->data[set_base + way];
                if (block_bytes_equal(bd->bytes, (const uint8_t *)(uintptr_t)tag->host_ptr, size)) {
                    apply_cache_hit(s, tag, bd);
                    return;
                }
            }
        }
    }
    if (cpsr_from_cache && size > 0 && size <= MAX_BLOCK_BYTES) {
        cpsr_ok = s->read_reg(uc, (int)s->cpsr_register, &raw_cpsr) == 0;
        s->block_cpsr = raw_cpsr;
        cpsr = normalize_cpsr(raw_cpsr);
    }
    uint8_t bytes[MAX_BLOCK_BYTES];
    const uint8_t *host_ptr = cpsr_ok ? resolve_host_ptr(s, address, size) : NULL;
    int readable = 0;
    if (host_ptr != NULL) {
        memcpy(bytes, host_ptr, size);
        readable = 1;
    } else if (cpsr_ok && s->read_mem(uc, address, bytes, size) == 0) {
        readable = 1;
        for (size_t way = 0; way < CACHE_WAYS; ++way) {
            BlockTag *tag = &set[way];
            if (tag->generation == gen32 && tag->address == address
                    && tag->size == size && tag->cpsr == cpsr) {
                BlockData *bd = &s->data[set_base + way];
                if (block_bytes_equal(bd->bytes, bytes, size)) {
                    apply_cache_hit(s, tag, bd);
                    return;
                }
            }
        }
    }
    ++s->cache_misses;
    s->special_count = 0;
    s->all_special = 0;
    int can_change_ifm = !readable || ((raw_cpsr & 0x1Fu) == 0x10u) || ((raw_cpsr & 0x01000200u) != 0x00000200u);
    int can_switch_t = can_change_ifm;
    if (readable && !(raw_cpsr & 0x20u)) {
        if (size & 3u) {
            can_change_ifm = 1;
            can_switch_t = 1;
        }
        int seen_msr = (raw_cpsr & 0x1Fu) == 0x10u;
        for (uint32_t offset = 0; offset + 3 < size; offset += 4) {
            uint32_t word = ((uint32_t)bytes[offset] << 24)
                          | ((uint32_t)bytes[offset + 1] << 16)
                          | ((uint32_t)bytes[offset + 2] << 8)
                          | (uint32_t)bytes[offset + 3];
            uint8_t type = 0;
            if ((word & 0x0F000F10u) == 0x0E000F10u) {
                if (!seen_msr && (word & 0xFFFF0FFFu) == 0xEE070F9Au
                        && ((word >> 12) & 15u) != 15u) {
                    type = 3;
                } else {
                    type = 1;
                }
            } else if ((word & 0x0D200000u) == 0x04200000u) {
                type = 2;
            } else if ((word & 0x0D90F000u) == 0x0100F000u) {
                seen_msr = 1;
            }
            if (!can_change_ifm) {
                if ((word >> 28) == 0xFu) {
                    can_switch_t = 1;
                    if ((word & 0xFE000000u) != 0xFA000000u) {
                        can_change_ifm = 1;
                    }
                } else {
                    uint32_t top2 = (word >> 26) & 3u;
                    if (top2 == 0u) {
                        if ((word & 0x0D900000u) == 0x01000000u
                                && (word & 0x0FC000F0u) != 0x00000090u) {
                            can_switch_t = 1;
                            if ((word & 0x0FFFFFF0u) != 0x012FFF10u
                                    && (word & 0x0FFFFFF0u) != 0x012FFF30u) {
                                can_change_ifm = 1;
                            }
                        } else if (((word >> 12) & 15u) == 15u && (word & (1u << 20)) != 0u) {
                            can_switch_t = 1;
                            can_change_ifm = 1;
                        }
                    } else if (top2 == 1u) {
                        if (((word >> 12) & 15u) == 15u && (word & (1u << 20)) != 0u) {
                            can_switch_t = 1;
                        }
                    } else if (top2 == 2u) {
                        if (((word >> 25) & 1u) == 0u && (word & 0x00408000u) != 0u) {
                            can_switch_t = 1;
                            if ((word & 0x00400000u) != 0u) {
                                can_change_ifm = 1;
                            }
                        }
                    } else {
                        can_switch_t = 1;
                        can_change_ifm = 1;
                    }
                }
            }
            if (type) {
                if (s->special_count < MAX_SPECIALS) {
                    size_t idx = (size_t)s->special_count++;
                    s->specials[idx] = address + offset;
                    s->words[idx] = word;
                    s->types[idx] = type;
                } else {
                    s->all_special = 1;
                }
            }
        }
    } else if (readable) {
        if (size & 1u) {
            can_change_ifm = 1;
            can_switch_t = 1;
        }
        for (uint32_t offset = 0; !can_change_ifm && offset + 1 < size; offset += 2) {
            uint16_t half = (uint16_t)(((uint16_t)bytes[offset] << 8) | bytes[offset + 1]);
            if ((half & 0xFF00u) == 0x4700u
                    || (half & 0xFC87u) == 0x4487u
                    || (half & 0xFF00u) == 0xBD00u
                    || (half & 0xF800u) == 0xE800u) {
                can_switch_t = 1;
            } else if (((half & 0xF000u) == 0xB000u
                        && (half & 0xFF00u) != 0xB000u
                        && (half & 0xFE00u) != 0xB400u
                        && (half & 0xFF00u) != 0xBC00u)
                    || (half & 0xFE00u) == 0xDE00u) {
                can_switch_t = 1;
                can_change_ifm = 1;
            }
        }
    }
    uint64_t page = address >> 12;
    uint64_t expected_page_tag = (s->generation << 32) | (((uint64_t)(cpsr & 0x10u)) << 20) | (page & 0xFFFFFULL);
    if (!(readable && !s->all_special && ((address ^ (address + size - 1)) < 0x1000ULL)
            && s->exec_page_tag[(page ^ (page >> 8)) & (EXEC_PAGE_SLOTS - 1)] == expected_page_tag && !s->stopped)) {
        s->current_word = raw_cpsr;
        s->current_kind = (readable && !s->all_special) ? 1 : 0;
        s->bridge(address, size, BLOCK);
        if (s->stopped) {
            s->stop(uc);
            return;
        }
    }
    if (readable && !s->all_special) {
        uint32_t post_gen = (uint32_t)s->generation;
        size_t target_way = s->cache_misses & (CACHE_WAYS - 1);
        for (size_t way = 0; way < CACHE_WAYS; ++way) {
            if (set[way].generation != post_gen
                    || (set[way].address == address && set[way].size == size && set[way].cpsr == cpsr)) {
                target_way = way;
                break;
            }
        }
        const uint8_t *post_host_ptr = resolve_host_ptr(s, address, size);
        if (post_host_ptr != NULL && memcmp(bytes, post_host_ptr, size) != 0) {
            post_host_ptr = NULL;
        }
        uint8_t cpsr_static = (s->special_count != 0 || can_change_ifm) ? 0u : (can_switch_t ? 2u : 1u);
        s->cpsr_valid = cpsr_static;
        BlockTag *tag = &set[target_way];
        BlockData *bd = &s->data[set_base + target_way];
        tag->address = address;
        tag->host_ptr = (uint64_t)(uintptr_t)post_host_ptr;
        tag->generation = post_gen;
        tag->size = size;
        tag->cpsr = cpsr;
        tag->special_count = (uint16_t)s->special_count;
        tag->cpsr_static = cpsr_static;
        if (s->special_count) {
            uint64_t dummy = 0;
            copy_specials(&dummy, bd->specials, bd->words, bd->types,
                          s->special_count, s->specials, s->words, s->types);
        }
        memcpy(bd->bytes, bytes, size);
    }
}

EXPORT void observer_code(void *uc, uint64_t address, uint32_t size, void *opaque)
{
    Observer *s = (Observer *)opaque;
    s->user_access = 0;
    uint64_t comp = s->completed + s->pending;
    s->completed = comp;
    s->pending = 0;
    if (UNLIKELY(comp >= s->deadline)) {
        if (comp < s->board_deadline
                && ((s->timers_in_c && comp >= s->next_timer_deadline)
                    || (s->uart_tx_active == 1u && comp >= s->uart_tx_deadline && s->uart_tx_out_count < 256u))) {
            uint32_t new_pending = s->timer_pending;
            if (s->timers_in_c && comp >= s->next_timer_deadline) {
                uint64_t min_t = 0xFFFFFFFFFFFFFFFFULL;
                for (uint32_t ch = 0; ch < 8u; ++ch) {
                    uint64_t cdl = s->timer_ch_deadline[ch];
                    if (comp >= cdl) {
                        uint32_t bit = 1u << ch;
                        new_pending |= bit;
                        s->timer_expired_mask |= bit;
                        s->timer_ch_dirty_mask |= bit;
                        uint64_t period = s->timer_ch_period[ch];
                        if (period > 0) {
                            cdl += period;
                            s->timer_ch_deadline[ch] = cdl;
                        } else {
                            cdl = 0xFFFFFFFFFFFFFFFFULL;
                            s->timer_ch_deadline[ch] = cdl;
                            s->timer_active_mask &= ~bit;
                        }
                    }
                    if (cdl < min_t) {
                        min_t = cdl;
                    }
                }
                s->next_timer_deadline = min_t;
                s->timer_pending = new_pending;
            }
            int flush_uart = 0;
            if (s->uart_tx_active == 1u && comp >= s->uart_tx_deadline) {
                uint8_t out_b = (uint8_t)s->uart_tx_pending_byte;
                uint32_t prev_b = s->_timer_pad;
                s->_timer_pad = 0x100u | (uint32_t)out_b;
                uint32_t oidx = s->uart_tx_out_count++;
                s->uart_tx_out_bytes[oidx] = out_b;
                s->uart_tx_out_insns[oidx] = (uint32_t)s->instructions;
                s->uart_tx_active = 0u;
                s->uart_tx_deadline = 0xFFFFFFFFFFFFFFFFULL;
                if (s->uart_fifo_enabled) {
                    s->uart0_state |= 0x40u;
                }
                if (s->uart0_fast_gen == s->generation) {
                    uint32_t irq0 = (s->uart0_state & ~s->uart0_mask) != 0u;
                    s->nt_irq_claim = irq0 ? s->uart0_irq_claim : 0u;
                }
                uint32_t trig = s->uart_tx_trigger_byte;
                if (s->uart_tx_bridge_mode == 2u
                        || (s->uart_tx_bridge_mode == 3u
                            && out_b == (uint8_t)(trig & 0xFFu)
                            && (!(trig & 0x10000u)
                                || prev_b == 0u
                                || prev_b == (0x100u | ((trig >> 8) & 0xFFu))))
                        || (s->uart_tx_bridge_mode == 1u && (out_b == 0x0Au || out_b == 0x0Du))
                        || s->uart_tx_out_count >= 240u
                        || s->uart_tx_write_count >= 240u) {
                    flush_uart = 1;
                }
            }
            uint32_t m0 = s->timer_mask0;
            uint32_t m1 = s->timer_mask1;
            uint32_t threshold = s->cc8_value & 7u;
            uint32_t max_irq = s->nt_irq_claim;
            uint32_t max_fiq = s->nt_fiq_claim;
            if (new_pending & m0) {
                max_irq = pick_claim(max_irq, s->irq18_claim);
                max_fiq = pick_claim(max_fiq, s->fiq18_claim);
            }
            if (new_pending & m1) {
                max_irq = pick_claim(max_irq, s->irq19_claim);
                max_fiq = pick_claim(max_fiq, s->fiq19_claim);
            }
            s->max_irq_claim = max_irq;
            s->max_fiq_claim = max_fiq;
            s->irq_claim = ((max_irq >> 8) > threshold) ? max_irq : 0;
            s->fiq_claim = ((max_fiq >> 8) > threshold) ? max_fiq : 0;
            s->irq_mask = (s->irq_claim ? 1u : 0u) | (s->fiq_claim ? 2u : 0u);
            uint64_t next_dl = s->board_deadline;
            if (s->timers_in_c && s->next_timer_deadline < next_dl) {
                next_dl = s->next_timer_deadline;
            }
            if (s->uart_tx_active == 1u && s->uart_tx_deadline < next_dl) {
                next_dl = s->uart_tx_deadline;
            }
            s->deadline = next_dl;
            if (flush_uart) {
                s->cpsr_valid = 0;
                s->bridge(address, size, UART_TX_FLUSH);
            }
        } else {
            s->cpsr_valid = 0;
            s->bridge(address, size, SYNCHRONIZE);
        }
    }
    if (UNLIKELY(s->stopped)) {
        s->stop(uc);
        return;
    }
    uint64_t mask = s->irq_mask;
    if (UNLIKELY(mask != 0)) {
        uint32_t fast_cpsr = s->cpsr_valid ? s->block_cpsr : 0;
        if (((mask & 2) && !(fast_cpsr & 0x40u))
                || ((mask & 1) && !(fast_cpsr & 0x80u))) {
            uint32_t cpsr = 0;
            if (s->read_reg(uc, (int)s->cpsr_register, &cpsr) == 0
                    && (((mask & 2) && !(cpsr & 0x40u))
                        || ((mask & 1) && !(cpsr & 0x80u)))) {
                if (!s->on_event && (cpsr & 0x1Fu) != 0x10u) {
                    uint32_t new_cpsr, vector;
                    if ((mask & 2) && !(cpsr & 0x40u)) {
                        new_cpsr = (cpsr & ~0x3Fu) | 0xD1u;
                        vector = 0x0000001Cu;
                    } else {
                        new_cpsr = (cpsr & ~0x3Fu) | 0x92u;
                        vector = 0x00000018u;
                    }
                    uint32_t lr = (uint32_t)((address & ~1ULL) + 4ULL);
                    s->write_reg(uc, (int)s->cpsr_register, &new_cpsr);
                    s->write_reg(uc, (int)s->spsr_register, &cpsr);
                    s->write_reg(uc, (int)s->lr_register, &lr);
                    s->write_reg(uc, (int)s->pc_register, &vector);
                    s->block_cpsr = new_cpsr;
                    return;
                }
                s->cpsr_valid = 0;
                s->bridge(address, size, INTERRUPT);
                if (s->stopped) {
                    s->stop(uc);
                }
                return;
            }
        }
    }
    uint64_t insns = s->instructions;
    if (UNLIKELY(insns >= s->limit)) {
        s->cpsr_valid = 0;
        s->bridge(address, size, LIMIT);
        s->stop(uc);
        return;
    }
    s->instructions = insns + 1;
    s->pending = 1;
    uint64_t rt = s->recent_total;
    s->recent[rt & 31] = address;
    s->recent_total = rt + 1;
    if (UNLIKELY((s->special_count | s->all_special) != 0)) {
        uint64_t special = s->all_special;
        s->current_kind = 0;
        for (uint64_t i = 0; !special && i < s->special_count; ++i) {
            if (s->specials[i] == address) {
                if (s->types[i] == 3 && !s->on_event) {
                    ++s->maintenance_count;
                    return;
                }
                special = 1;
                s->current_word = s->words[i];
                s->current_kind = (s->types[i] == 2) ? 2 : 1;
            }
        }
        if (special) {
            s->cpsr_valid = 0;
            s->bridge(address, size, SPECIAL);
            if (s->stopped) {
                s->stop(uc);
            }
        }
    }
}

EXPORT void observer_mmio(
    void *uc, int access, uint64_t address, int size, int64_t value, void *opaque)
{
    Observer *s = (Observer *)opaque;
    if (!s->user_access && !s->on_event && !s->stopped) {
        if (access == 16) {
            if ((address & ~0xFFFULL) == 0xBD040000ULL && s->bd040_read_gen == s->generation) {
                uint8_t *base = (uint8_t *)(uintptr_t)s->bd040_base_ptr;
                if (address == 0xBD040814ULL && size == 4) {
                    uint64_t ticks = s->base_ticks + (s->completed - s->synced_completed) * s->timer_step;
                    write_be32(base + 0x814, (uint32_t)(ticks + s->timer_offset));
                    return;
                }
                if (size == 2) {
                    uint32_t v = 0xFFFFFFFFu;
                    if (address == 0xBD040CC8ULL) {
                        v = s->cc8_value;
                    } else if (address == 0xBD040800ULL) {
                        v = s->timer_pending;
                    } else if (address == 0xBD040CB8ULL) {
                        v = s->irq_claim;
                    } else if (address == 0xBD040804ULL) {
                        v = s->timer_mask0;
                    } else if (address == 0xBD040CC0ULL) {
                        v = s->fiq_claim;
                    } else if (address == 0xBD040806ULL) {
                        v = s->timer_mask1;
                    } else if (address == 0xBD040400ULL) {
                        v = s->_sdio_pad;
                    }
                    if (v != 0xFFFFFFFFu) {
                        write_be16(base + (size_t)(address & 0xFFFu), v);
                        return;
                    }
                }
            } else if ((address == 0xBD0B1410ULL || address == 0xBD0B1418ULL)
                       && size == 2 && s->panel_fast_gen == s->generation) {
                if (address == 0xBD0B1418ULL) {
                    write_be16((uint8_t *)(uintptr_t)s->bd0b1_base_ptr + 0x418, s->panel_output_word);
                    return;
                }
                if (s->panel_armed_strobe && s->panel_bit_count > 0 && s->panel_sampled_bit >= 0
                        && ((s->panel_output_word >> 3) & 1u) != s->panel_reset_level) {
                    write_be16((uint8_t *)(uintptr_t)s->bd0b1_base_ptr + 0x410, (uint32_t)(s->panel_sampled_bit << 8));
                    return;
                }
                if (UNLIKELY(s->on_event || s->user_access)) {
                    s->cpsr_valid = 0;
                }
                s->mmio_bridge(access, address, size, value);
                if (s->stopped) {
                    s->stop(uc);
                }
                return;
            } else if ((address == 0xBD0B3C04ULL || address == 0xBD0B3C0EULL || address == 0xBD0B3C0CULL)
                       && size == 2 && s->uart0_fast_gen == s->generation) {
                uint8_t *base = (uint8_t *)(uintptr_t)s->bd0b3_base_ptr;
                if (address == 0xBD0B3C04ULL) {
                    write_be16(base + 0xC04, s->uart0_mask);
                    return;
                }
                if (address == 0xBD0B3C0EULL) {
                    uint32_t v;
                    if (s->uart_fifo_enabled) {
                        uint32_t st = s->uart0_state;
                        v = ((st & 0x40u) ? 0x0180u : 0u) | ((st & 0x01u) ? 0x0004u : 0u);
                    } else {
                        v = (s->uart_tx_active == 0u) ? 0x0100u : 0u;
                    }
                    write_be16(base + 0xC0E, v);
                    return;
                }
                if (s->uart_rx_count <= 256u) {
                    uint32_t v = 0;
                    if (s->uart_rx_head < s->uart_rx_count) {
                        v = s->uart_rx_buf[s->uart_rx_head++];
                        if (s->uart_rx_head >= s->uart_rx_count) {
                            uint32_t st = s->uart0_state & ~0x01u;
                            s->uart0_state = st;
                            uint32_t irq0 = (st & ~s->uart0_mask) != 0u;
                            uint32_t nt_claim = irq0 ? s->uart0_irq_claim : 0u;
                            s->nt_irq_claim = nt_claim;
                            uint32_t max_irq = nt_claim;
                            if (s->timer_pending & s->timer_mask0) {
                                max_irq = pick_claim(max_irq, s->irq18_claim);
                            }
                            if (s->timer_pending & s->timer_mask1) {
                                max_irq = pick_claim(max_irq, s->irq19_claim);
                            }
                            uint32_t threshold = s->cc8_value & 7u;
                            s->max_irq_claim = max_irq;
                            s->irq_claim = ((max_irq >> 8) > threshold) ? max_irq : 0u;
                            s->irq_mask = (s->irq_claim ? 1u : 0u) | (s->fiq_claim ? 2u : 0u);
                        }
                    }
                    s->mmio_cache[((0xBD0B3C0EULL >> 1) ^ (0xBD0B3C0EULL >> 7) ^ 2ULL) & (MMIO_CACHE_SLOTS - 1)].mmio_gen = 0;
                    write_be16(base + 0xC0C, v);
                    return;
                }
            } else if ((address & ~0xFFFULL) == 0xBD0B0000ULL && size == 4 && s->sdio_fast_gen == s->generation) {
                uint32_t off = (uint32_t)(address & 0xFFFu);
                if (off < 0x100u && (off & 3u) == 0) {
                    int matched = 1;
                    uint32_t v = 0;
                    if (off == 0x30u) {
                        v = (s->sdio_int_status & ~0x100u) | ((s->sdio_card_irq && (s->sdio_status_en & 0x100u)) ? 0x100u : 0u);
                        s->sdio_int_status = v;
                    } else if (off == 0x34u) {
                        v = s->sdio_status_en;
                    } else if (off == 0x38u) {
                        v = s->sdio_signal_en;
                    } else if (off == 0x10u) {
                        v = s->sdio_resp0;
                    } else if (off == 0x24u) {
                        v = 0x01FF0000u;
                    } else if (off == 0xFCu) {
                        uint32_t slot_int = (s->sdio_int_status & s->sdio_status_en & s->sdio_signal_en) ? 1u : 0u;
                        v = 0x02000000u | slot_int;
                    } else if (off == 0x00u) {
                        v = s->sdio_sdma_addr;
                    } else if (off == 0x04u) {
                        v = (s->sdio_blk_reg & 0xFFFF0FFFu) | 0x7000u;
                    } else if (off == 0x08u) {
                        v = s->sdio_arg;
                    } else {
                        matched = 0;
                    }
                    if (matched) {
                        write_be32((uint8_t *)(uintptr_t)s->bd0b0_base_ptr + off, v);
                        return;
                    }
                }
            }
            size_t slot_idx = (size_t)(((address >> 1) ^ (address >> 7) ^ (uint64_t)size) & (MMIO_CACHE_SLOTS - 1));
            MmioSlot *slot = &s->mmio_cache[slot_idx];
            if (slot->mmio_gen == s->mmio_gen && slot->address == address && slot->size == (uint32_t)size) {
                uint8_t *dst = (uint8_t *)(uintptr_t)slot->host_ptr;
                uint32_t v = slot->value;
                if (size == 2) {
                    write_be16(dst, v);
                } else if (size == 4) {
                    write_be32(dst, v);
                } else {
                    dst[0] = (uint8_t)v;
                }
                return;
            }
        } else if (size == 2) {
            if (address == 0xBD0B1418ULL && s->panel_fast_gen == s->generation) {
                uint32_t v = (uint32_t)value & 0xFFFFu;
                if ((v & ~0xFu) == 0 && ((v >> 3) & 1u) != s->panel_reset_level
                        && s->panel_write_count < 256u) {
                    uint32_t prev = s->panel_output_word;
                    uint32_t rising = (!(prev & 2u)) && (v & 2u);
                    if (rising && s->panel_armed_strobe) {
                        uint32_t idx = s->panel_bit_count;
                        if (idx < s->panel_frame_bits) {
                            uint64_t shifted = s->panel_shifted_output | (((uint64_t)((v >> 2) & 1u)) << idx);
                            int32_t sampled;
                            if (idx < s->panel_input_width) {
                                sampled = (int32_t)((s->panel_latched_inputs >> idx) & 1u);
                            } else if (idx < s->panel_input_width + 8u) {
                                sampled = (int32_t)((s->panel_signature >> (idx - s->panel_input_width)) & 1u);
                            } else if (idx < s->panel_input_width + 16u) {
                                sampled = (int32_t)((shifted >> (idx - s->panel_input_width - 8u)) & 1u);
                            } else {
                                sampled = s->panel_tail_bit;
                            }
                            s->panel_shifted_output = shifted;
                            s->panel_bit_count = idx + 1u;
                            s->panel_sampled_bit = sampled;
                            s->panel_output_word = v;
                            s->panel_writes_buf[s->panel_write_count++] = (uint8_t)v;
                            return;
                        }
                    } else {
                        s->panel_output_word = v;
                        s->panel_writes_buf[s->panel_write_count++] = (uint8_t)v;
                        return;
                    }
                }
            } else if ((address == 0xBD0B3C04ULL || address == 0xBD0B3C06ULL) && s->uart0_fast_gen == s->generation) {
                uint32_t v = (uint32_t)value & 0xFFFFu;
                if (address == 0xBD0B3C04ULL) {
                    s->uart0_mask = v;
                    s->uart0_mask_dirty = 1;
                    write_be16((uint8_t *)(uintptr_t)s->bd0b3_base_ptr + 0xC04, v);
                    uint32_t irq0 = (s->uart0_state & ~v) != 0;
                    uint32_t nt_claim = irq0 ? s->uart0_irq_claim : 0;
                    s->nt_irq_claim = nt_claim;
                    uint32_t max_irq = nt_claim;
                    if (s->timer_pending & s->timer_mask0) {
                        max_irq = pick_claim(max_irq, s->irq18_claim);
                    }
                    if (s->timer_pending & s->timer_mask1) {
                        max_irq = pick_claim(max_irq, s->irq19_claim);
                    }
                    uint32_t threshold = s->cc8_value & 7u;
                    s->max_irq_claim = max_irq;
                    s->irq_claim = ((max_irq >> 8) > threshold) ? max_irq : 0;
                    s->irq_mask = (s->irq_claim ? 1u : 0u) | (s->fiq_claim ? 2u : 0u);
                    return;
                }
                if (s->uart_tx_active == 0u && s->uart_tx_write_count < 256u && s->uart_tx_out_count < 256u) {
                    uint32_t pc = 0;
                    s->read_reg(uc, (int)s->pc_register, &pc);
                    uint32_t widx = s->uart_tx_write_count++;
                    s->uart_tx_write_pcs[widx] = pc;
                    s->uart_tx_write_insns[widx] = (uint32_t)s->instructions;
                    s->uart_tx_write_vals[widx] = (uint16_t)v;
                    write_be16((uint8_t *)(uintptr_t)s->bd0b3_base_ptr + 0xC06, v);
                    s->mmio_cache[((0xBD0B3C0EULL >> 1) ^ (0xBD0B3C0EULL >> 7) ^ 2ULL) & (MMIO_CACHE_SLOTS - 1)].mmio_gen = 0;
                    s->uart_tx_active = 1u;
                    s->uart_tx_pending_byte = v & 0xFFu;
                    uint64_t tx_dl = s->completed + (uint64_t)s->uart_frame_steps;
                    s->uart_tx_deadline = tx_dl;
                    if (tx_dl < s->deadline) {
                        s->deadline = tx_dl;
                    }
                    uint32_t st = s->uart0_state & ~0x40u;
                    s->uart0_state = st;
                    uint32_t irq0 = (st & ~s->uart0_mask) != 0u;
                    uint32_t nt_claim = irq0 ? s->uart0_irq_claim : 0u;
                    s->nt_irq_claim = nt_claim;
                    uint32_t max_irq = nt_claim;
                    if (s->timer_pending & s->timer_mask0) {
                        max_irq = pick_claim(max_irq, s->irq18_claim);
                    }
                    if (s->timer_pending & s->timer_mask1) {
                        max_irq = pick_claim(max_irq, s->irq19_claim);
                    }
                    uint32_t threshold = s->cc8_value & 7u;
                    s->max_irq_claim = max_irq;
                    s->irq_claim = ((max_irq >> 8) > threshold) ? max_irq : 0u;
                    s->irq_mask = (s->irq_claim ? 1u : 0u) | (s->fiq_claim ? 2u : 0u);
                    return;
                }
            } else if (s->cc8_write_gen == s->generation) {
                if (address == 0xBD040CC8ULL) {
                    uint32_t v = (uint32_t)value & 0xFFFFu;
                    uint32_t threshold = v & 7u;
                    s->cc8_value = v;
                    s->cc8_dirty = 1;
                    s->irq_claim = ((s->max_irq_claim >> 8) > threshold) ? s->max_irq_claim : 0;
                    s->fiq_claim = ((s->max_fiq_claim >> 8) > threshold) ? s->max_fiq_claim : 0;
                    s->irq_mask = (s->irq_claim ? 1u : 0u) | (s->fiq_claim ? 2u : 0u);
                    return;
                }
                if (address == 0xBD040400ULL && s->wdog_enabled && s->timers_in_c) {
                    uint32_t v = (uint32_t)value & 0xFFFFu;
                    if (v == 0x000Cu) {
                        s->_sdio_pad = 0x000Cu;
                        s->wdog_pet_dirty = 1u;
                        s->wdog_pet_comp = s->completed;
                        write_be16((uint8_t *)(uintptr_t)s->bd040_base_ptr + 0x400, 0x000Cu);
                        MmioSlot *wslot = &s->mmio_cache[((0xBD040400ULL >> 1) ^ (0xBD040400ULL >> 7) ^ 2ULL) & (MMIO_CACHE_SLOTS - 1)];
                        if (wslot->address == 0xBD040400ULL) {
                            wslot->value = 0x000Cu;
                        }
                        return;
                    }
                    if (v == 0x0003u || v == 0x000Fu) {
                        uint32_t prev = s->_sdio_pad;
                        s->_sdio_pad = v;
                        if (v == 0x000Fu || (prev & 3u) == 0u) {
                            s->wdog_pet_dirty = 2u;
                            s->wdog_pet_comp = s->completed;
                        } else if (s->wdog_pet_dirty == 0u) {
                            s->wdog_pet_dirty = 3u;
                        }
                        write_be16((uint8_t *)(uintptr_t)s->bd040_base_ptr + 0x400, v);
                        MmioSlot *wslot = &s->mmio_cache[((0xBD040400ULL >> 1) ^ (0xBD040400ULL >> 7) ^ 2ULL) & (MMIO_CACHE_SLOTS - 1)];
                        if (wslot->address == 0xBD040400ULL) {
                            wslot->value = v;
                        }
                        return;
                    }
                }
                if (address == 0xBD040802ULL) {
                    uint32_t v = (uint32_t)value & 0xFFFFu;
                    uint32_t old_pending = s->timer_pending;
                    uint32_t new_pending = old_pending & ~v;
                    uint32_t m0 = s->timer_mask0;
                    uint32_t m1 = s->timer_mask1;
                    uint32_t m_other = s->timer_mask_union & ~(m0 | m1);
                    if ((!(old_pending & m0) == !(new_pending & m0))
                            && (!(old_pending & m1) == !(new_pending & m1))
                            && (((old_pending ^ new_pending) & m_other) == 0)) {
                        s->timer_pending = new_pending;
                        s->ack_802_value = v;
                        s->ack_802_dirty = 1;
                        return;
                    }
                    if ((new_pending & m_other) == 0) {
                        uint32_t threshold = s->cc8_value & 7u;
                        uint32_t max_irq = s->nt_irq_claim;
                        uint32_t max_fiq = s->nt_fiq_claim;
                        if (new_pending & m0) {
                            max_irq = pick_claim(max_irq, s->irq18_claim);
                            max_fiq = pick_claim(max_fiq, s->fiq18_claim);
                        }
                        if (new_pending & m1) {
                            max_irq = pick_claim(max_irq, s->irq19_claim);
                            max_fiq = pick_claim(max_fiq, s->fiq19_claim);
                        }
                        s->timer_pending = new_pending;
                        s->ack_802_value = v;
                        s->ack_802_dirty = 1;
                        s->max_irq_claim = max_irq;
                        s->max_fiq_claim = max_fiq;
                        s->irq_claim = ((max_irq >> 8) > threshold) ? max_irq : 0;
                        s->fiq_claim = ((max_fiq >> 8) > threshold) ? max_fiq : 0;
                        s->irq_mask = (s->irq_claim ? 1u : 0u) | (s->fiq_claim ? 2u : 0u);
                        return;
                    }
                }
                if (address >= 0xBD04081EULL && address < 0xBD04083CULL
                        && ((address - 0xBD04081EULL) & 3ULL) == 0) {
                    uint32_t v = (uint32_t)value & 0xFFFFu;
                    uint32_t ch = (uint32_t)((address - 0xBD04081EULL) >> 2);
                    uint32_t bit = 1u << ch;
                    if (s->timers_in_c && s->us_ticks > 0) {
                        s->timer_last_val[ch] = (uint16_t)v;
                        s->timer_val_dirty_mask |= bit;
                        s->timer_ch_dirty_mask |= bit;
                        if (v == 0) {
                            s->timer_zeroed_mask |= bit;
                            s->timer_active_mask &= ~bit;
                            s->timer_ch_deadline[ch] = 0xFFFFFFFFFFFFFFFFULL;
                        } else {
                            s->timer_zeroed_mask &= ~bit;
                            s->timer_active_mask |= bit;
                            s->timer_ch_deadline[ch] = s->completed + (uint64_t)v * (uint64_t)s->us_ticks;
                        }
                        uint64_t min_t = 0xFFFFFFFFFFFFFFFFULL;
                        for (uint32_t i = 0; i < 8u; ++i) {
                            if (s->timer_ch_deadline[i] < min_t) {
                                min_t = s->timer_ch_deadline[i];
                            }
                        }
                        s->next_timer_deadline = min_t;
                        uint64_t next_dl = s->board_deadline;
                        if (min_t < next_dl) {
                            next_dl = min_t;
                        }
                        if (s->uart_tx_active == 1u && s->uart_tx_deadline < next_dl) {
                            next_dl = s->uart_tx_deadline;
                        }
                        s->deadline = next_dl;
                        return;
                    }
                    if (v == 0 && !(s->timer_active_mask & bit) && (s->timer_reload_zero_mask & bit)) {
                        s->timer_zeroed_mask |= bit;
                        return;
                    }
                }
            }
        } else if (size == 4 && (address & ~0xFFFULL) == 0xBD0B0000ULL && s->sdio_fast_gen == s->generation) {
            uint32_t off = (uint32_t)(address & 0xFFFu);
            uint32_t v = (uint32_t)value;
            if (off == 0x00u) {
                s->sdio_sdma_addr = v;
                s->sdio_dirty |= 4u;
                return;
            }
            if (off == 0x04u) {
                s->sdio_blk_reg = v;
                s->sdio_dirty |= 8u;
                return;
            }
            if (off == 0x08u) {
                s->sdio_arg = v;
                s->sdio_dirty |= 16u;
                return;
            }
            if (off == 0x30u || off == 0x34u || off == 0x38u) {
                uint32_t old_ist = (s->sdio_int_status & ~0x100u) | ((s->sdio_card_irq && (s->sdio_status_en & 0x100u)) ? 0x100u : 0u);
                uint32_t old_active = (old_ist & s->sdio_status_en & s->sdio_signal_en) != 0;
                uint32_t new_ist = old_ist;
                uint32_t new_sten = s->sdio_status_en;
                uint32_t new_sigen = s->sdio_signal_en;
                if (off == 0x30u) {
                    new_ist = (old_ist & ~(v & ~0x100u)) | ((s->sdio_card_irq && (new_sten & 0x100u)) ? 0x100u : 0u);
                } else if (off == 0x34u) {
                    new_sten = v;
                    new_ist = (old_ist & ~0x100u) | ((s->sdio_card_irq && (new_sten & 0x100u)) ? 0x100u : 0u);
                } else {
                    new_sigen = v;
                }
                uint32_t new_active = (new_ist & new_sten & new_sigen) != 0;
                if (new_active == old_active) {
                    s->sdio_int_status = new_ist;
                    s->sdio_status_en = new_sten;
                    s->sdio_signal_en = new_sigen;
                    s->sdio_dirty |= (off == 0x34u) ? 1u : ((off == 0x38u) ? 2u : 32u);
                    return;
                }
            }
        } else if (address == 0xBD3008B0ULL && size == 4
                   && (s->_panel_pad & 0x10000u) != 0u && ((uint64_t)value & ~0xFFFFULL) == 0ULL) {
            uint32_t v16 = (uint32_t)value & 0xFFFFu;
            s->_panel_pad = 0x30000u | v16;
            MmioSlot *mslot = &s->mmio_cache[((0xBD3008B0ULL >> 1) ^ (0xBD3008B0ULL >> 7) ^ 4ULL) & (MMIO_CACHE_SLOTS - 1)];
            if (mslot->address == 0xBD3008B0ULL && mslot->host_ptr != 0) {
                mslot->value = v16;
                write_be32((uint8_t *)(uintptr_t)mslot->host_ptr, v16);
            }
            return;
        }
    }
    if (UNLIKELY(s->on_event || s->user_access)) {
        s->cpsr_valid = 0;
    }
    s->mmio_bridge(access, address, size, value);
    if (s->stopped) {
        s->stop(uc);
    }
}

EXPORT int64_t native_unpack_lzss(
    const uint8_t *packed,
    size_t packed_len,
    uint8_t *out,
    size_t expected_size
) {
    size_t cursor = 0;
    size_t out_len = 0;
    uint32_t flags = 0;
    while (cursor < packed_len) {
        flags >>= 1;
        if (!(flags & 0x100u)) {
            if (out_len > expected_size) {
                return -1;
            }
            flags = 0xFF00u | (uint32_t)packed[cursor++];
            if (cursor >= packed_len) {
                return -2;
            }
        }
        if (flags & 1u) {
            if (cursor >= packed_len) {
                return -2;
            }
            if (out_len < expected_size) {
                out[out_len] = packed[cursor];
            }
            ++out_len;
            ++cursor;
        } else {
            if (cursor >= packed_len) {
                return -2;
            }
            if (cursor + 1 >= packed_len) {
                return -3;
            }
            uint32_t first = packed[cursor];
            uint32_t second = packed[cursor + 1];
            cursor += 2;
            uint32_t offset = first | ((second & 0xF0u) << 4);
            size_t length = (size_t)(second & 0x0Fu) + 3u;
            int64_t dist = (int64_t)(((out_len + 0xFEDu - (size_t)offset) & 0xFFFu) + 1u);
            int64_t source = (int64_t)out_len - dist;
            if (out_len + length > expected_size) {
                return -1;
            }
            if (source >= 0) {
                for (size_t i = 0; i < length; ++i) {
                    out[out_len + i] = out[(size_t)source + i];
                }
            } else {
                for (size_t i = 0; i < length; ++i) {
                    int64_t s = source + (int64_t)i;
                    out[out_len + i] = (s >= 0) ? out[(size_t)s] : 0;
                }
            }
            out_len += length;
        }
    }
    if (out_len > expected_size) {
        return -1;
    }
    if (out_len != expected_size) {
        return (int64_t)out_len;
    }
    return -5;
}

EXPORT void native_zero_dram(void *ptr, size_t byte_len) {
    uint64_t *p = (uint64_t *)ptr;
    size_t n = byte_len >> 3;
    for (size_t i = 0; i < n; ++i) {
        if (p[i] != 0) {
            p[i] = 0;
        }
    }
    uint8_t *tail = (uint8_t *)(p + n);
    for (size_t i = 0; i < (byte_len & 7u); ++i) {
        if (tail[i] != 0) {
            tail[i] = 0;
        }
    }
}

#ifdef _WIN32
__declspec(dllimport) void * __stdcall VirtualAlloc(void *, size_t, uint32_t, uint32_t);
__declspec(dllimport) uint32_t __stdcall GetWriteWatch(uint32_t, void *, size_t, void **, uintptr_t *, uint32_t *);
__declspec(dllimport) uint32_t __stdcall ResetWriteWatch(void *, size_t);
#endif

EXPORT void *native_alloc_dram(size_t byte_len) {
#ifdef _WIN32
    void *p = VirtualAlloc(NULL, byte_len, 0x00203000u, 0x04u);
    if (p != NULL) {
        ResetWriteWatch(p, byte_len);
    }
    return p;
#else
    (void)byte_len;
    return NULL;
#endif
}

EXPORT uint64_t native_prepare_dram(
    uint8_t *dst,
    size_t total_bytes,
    const uint8_t *init_src,
    size_t init_len,
    int same_init_src,
    size_t prev_init_len,
    const uint8_t *exec_pages
) {
#ifdef _WIN32
    static void *dirty_pages[16384];
    uintptr_t count = 16384;
    uint32_t gran = 4096;
    if (GetWriteWatch(0x01u, dst, total_bytes, dirty_pages, &count, &gran) == 0) {
        if (init_src != NULL && init_len > 0 && same_init_src) {
            uint64_t exec_dirty = 0;
            for (uintptr_t i = 0; i < count; ++i) {
                uint8_t *p = (uint8_t *)dirty_pages[i];
                size_t off = (size_t)(p - dst);
                if (exec_pages != NULL && (off >> 12) < 16384 && exec_pages[off >> 12]) {
                    exec_dirty = 1;
                }
                if (off + gran <= init_len) {
                    memcpy(p, init_src + off, gran);
                } else if (off < init_len) {
                    size_t copy_n = init_len - off;
                    memcpy(p, init_src + off, copy_n);
                    memset(p + copy_n, 0, gran - copy_n);
                } else if (off + gran <= total_bytes) {
                    memset(p, 0, gran);
                }
            }
            ResetWriteWatch(dst, total_bytes);
            return exec_dirty;
        }
        uint64_t exec_dirty = (init_len == 0 && prev_init_len == 0) ? 0 : 1;
        for (uintptr_t i = 0; i < count; ++i) {
            uint8_t *p = (uint8_t *)dirty_pages[i];
            size_t off = (size_t)(p - dst);
            if (!exec_dirty && exec_pages != NULL && (off >> 12) < 16384 && exec_pages[off >> 12]) {
                exec_dirty = 1;
            }
            if (off + gran > init_len && off + gran <= total_bytes) {
                memset(p, 0, gran);
            }
        }
        if (init_src != NULL && init_len > 0) {
            memcpy(dst, init_src, init_len);
        }
        if (prev_init_len > init_len) {
            memset(dst + init_len, 0, prev_init_len - init_len);
        }
        ResetWriteWatch(dst, total_bytes);
        return exec_dirty;
    }
#else
    (void)same_init_src;
    (void)prev_init_len;
    (void)exec_pages;
#endif
    if (init_src != NULL && init_len > 0) {
        memcpy(dst, init_src, init_len);
        if (init_len < total_bytes) {
            native_zero_dram(dst + init_len, total_bytes - init_len);
        }
    } else {
        native_zero_dram(dst, total_bytes);
    }
    return 1;
}

EXPORT int64_t native_dirty_dram_offsets(
    const uint8_t *dst,
    size_t total_bytes,
    uint32_t *out_offsets,
    size_t max_offsets
) {
#ifdef _WIN32
    static void *dirty_pages[16384];
    uintptr_t count = 16384;
    uint32_t gran = 4096;
    if (GetWriteWatch(0u, (void *)dst, total_bytes, dirty_pages, &count, &gran) == 0 && gran == 4096) {
        size_t out_n = 0;
        for (uintptr_t i = 0; i < count; ++i) {
            const uint8_t *p = (const uint8_t *)dirty_pages[i];
            size_t off = (size_t)(p - dst);
            if (off + 4096 <= total_bytes) {
                const uint64_t *w = (const uint64_t *)p;
                int nz = 0;
                for (size_t k = 0; k < 512; ++k) {
                    if (w[k] != 0) {
                        nz = 1;
                        break;
                    }
                }
                if (nz) {
                    if (out_n >= max_offsets) {
                        return -1;
                    }
                    out_offsets[out_n++] = (uint32_t)off;
                }
            }
        }
        return (int64_t)out_n;
    }
#else
    (void)dst;
    (void)total_bytes;
    (void)out_offsets;
    (void)max_offsets;
#endif
    return -1;
}
