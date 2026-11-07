/*
 * W25Q128JV driver on the AXON SPI controller (0xBD063800).
 *
 * The controller serializer is only partly decoded (analysis/spi_review.md).
 * Two hypotheses are consistent with every sequence HP firmware issues:
 *   SPI_MODE_BYTE_FINAL: every TX write clocks one byte; clearing the CS-hold
 *                        bit on the final byte releases CS after it.
 *   SPI_MODE_WIDE_FINAL: a TX write with the CS-hold bit clear clocks nothing
 *                        and only releases CS, so an explicit release write
 *                        follows the last data byte.
 * The mode can be switched at run time ("flash mode") so reads can be tried
 * against real hardware before any write is attempted. flash_write_enable()
 * verifies that WEL latched, which fails loudly if the hypothesis is wrong.
 *
 * Every erase/program below FLASH_PROTECT_END is refused: HP stage-1 and the
 * bootloader can only be changed with an external programmer.
 */
#ifndef FW_SPI_FLASH_H
#define FW_SPI_FLASH_H
#include <stdint.h>

enum { SPI_MODE_BYTE_FINAL = 0, SPI_MODE_WIDE_FINAL = 1 };

enum flash_err {
    FLASH_OK = 0,
    FLASH_ERR_PROTECTED = -1,
    FLASH_ERR_ALIGN = -2,
    FLASH_ERR_RANGE = -3,
    FLASH_ERR_WEL = -4,        /* write enable did not latch */
    FLASH_ERR_TIMEOUT = -5,    /* WIP never cleared */
    FLASH_ERR_VERIFY = -6,     /* read-back mismatch */
    FLASH_ERR_BUS = -7,        /* controller status never reached the expected state */
    FLASH_ERR_UNVERIFIED = -8, /* serializer mode neither detected nor forced */
};

#define FLASH_JEDEC_W25Q128 0xEF4018U

void     flash_init(void);
void     flash_set_mode(int mode);
int      flash_get_mode(void);
int      flash_mode_was_detected(void);
const char *flash_mode_name(int mode);
uint32_t flash_bus_errors(void);   /* count of bounded status waits that gave up */
/* Sectors whose memory-mapped window lagged behind the chip after a write but
   whose SPI read-back matched (window staleness, not a write failure). */
uint32_t flash_window_stale_sectors(void);
/* Read-only calibration: known JEDEC id through HP's receive pattern, framing
   of the CS-releasing transfer, and command-path reads that must match the
   memory-mapped window. Returns the framing adopted, or -1 (writes disabled). */
int      flash_autodetect_mode(void);
/* Print raw controller behaviour under several transfer patterns (read-only). */
void     flash_diag(void);
uint32_t flash_read_jedec_id(void);
uint8_t  flash_read_status1(void);
void     flash_read(uint32_t addr, void *buf, uint32_t len);       /* via SPI command 0x03 */
int      flash_write_enable(void);
int      flash_wait_ready(uint32_t timeout_ms);
int      flash_erase_sector(uint32_t addr);                         /* 4 KiB, verifies 0xFF */
int      flash_program_page(uint32_t addr, const void *data, uint32_t len);  /* <=256, in-page */
/* Erase + program + verify an arbitrary byte range (sector-aligned start). Calls
   progress(done, total) after every sector when non-NULL. */
int      flash_write_region(uint32_t addr, const void *data, uint32_t len,
                            void (*progress)(uint32_t done, uint32_t total));
const char *flash_err_str(int err);

/* The flash is also visible read-only at CPU address 0..16 MiB. */
static inline const uint8_t *flash_mmap(uint32_t addr) { return (const uint8_t *)(uintptr_t)addr; }

#endif
