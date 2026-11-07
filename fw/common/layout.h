/*
 * Flash and RAM layout shared by the bootloader (BL), the application and the
 * Python image builder (fw/image.py parses the numeric #defines in this file,
 * so keep them plain).
 *
 * Flash (W25Q128JV, 16 MiB, 4 KiB sectors, 256 B pages)
 *
 *   0x000000  HP stage-1 boot ROM  (PLL/DDR/UART/SPI init)      kept, never written
 *   0x020030  HP container header (patched: scatter-copies BL)   kept, never written
 *   0x022000  BL image  = 64 B header + payload (<= 120 KiB)     written by CH341 only
 *   0x040000  APP slot A = 64 B header + payload (<= 512 KiB)    updatable
 *   0x0C0000  APP slot B                                         updatable
 *   0x140000  unused (original dump content is preserved)
 *   0xC93000  (GHS overlay header sector; blanked by the builder)
 *   0xFFF000  (HP footer sector; blanked by the builder)
 *
 * Everything below FLASH_PROTECT_END is refused by the flash driver, which is
 * what makes the base bootloader "not updatable" from software.
 *
 * DRAM (64 MiB, visible through three apertures of the same physical memory):
 *   0xA0xxxxxx  read-only for data, instruction fetch allowed, cached write-through -> code
 *   0x20xxxxxx  read/write, cached write-back, write-buffered                      -> data/stack
 *   0x60xxxxxx  read/write, uncached, write-buffered                               -> (unused)
 *   0x60000000..0x60000FFF is NOT DRAM: HP stage-1 relocates the 4 KiB DTCM there
 *   (HP keeps .srdata in it); HP's stage-1 data also lives below 0x2000 in DRAM.
 * The MPU is left exactly as HP stage-1 programmed it; this is why code is
 * linked at 0xA0... and copied through the 0x20... alias (RW_ALIAS()).
 * Memory below physical offset DRAM_FREE_OFFSET is reserved for the above and
 * for the BL/app/staging regions; "memtest" refuses it.
 */
#ifndef FW_LAYOUT_H
#define FW_LAYOUT_H

#define FLASH_TOTAL_SIZE     0x01000000
#define FLASH_SECTOR_SIZE    0x00001000
#define FLASH_PAGE_SIZE      0x00000100

#define FLASH_STAGE1_SIZE    0x00022000   /* HP stage-1 + container header, preserved */
#define FLASH_BL_OFFSET      0x00022000
#define FLASH_BL_SIZE        0x0001E000   /* header + payload, 120 KiB */
#define FLASH_PROTECT_END    0x00040000   /* flash driver refuses writes below this */
#define FLASH_SLOT_A_OFFSET  0x00040000
#define FLASH_SLOT_B_OFFSET  0x000C0000
#define FLASH_SLOT_SIZE      0x00080000   /* 512 KiB per application slot */
#define FLASH_SLOT_COUNT     2

#define IMAGE_HEADER_SIZE    64

/* Bootloader RAM */
#define BL_LOAD_ADDR         0xA0020000   /* code + rodata + .data load image */
#define BL_CODE_MAX          (FLASH_BL_SIZE - IMAGE_HEADER_SIZE)
#define BL_DATA_ADDR         0x20040000   /* .data/.bss VMA */
#define BL_DATA_SIZE         0x00020000
#define BL_STACK_TOP         0x20080000

/* Application RAM */
#define APP_LOAD_ADDR        0xA0100000
#define APP_CODE_MAX         (FLASH_SLOT_SIZE - IMAGE_HEADER_SIZE)
#define APP_DATA_ADDR        0x20180000
#define APP_DATA_SIZE        0x00040000
#define APP_STACK_TOP        0x20200000

/* Shared scratch: XMODEM staging buffer for a whole slot image */
#define STAGING_ADDR         0x20400000
#define STAGING_SIZE         FLASH_SLOT_SIZE

/* Boot information block (BL -> app, app -> BL) */
#define BOOTINFO_ADDR        0x2001F000

/* First physical DRAM offset free for user experiments (memtest etc.) */
#define DRAM_FREE_OFFSET     0x00800000
#define DRAM_ALIAS_SIZE      0x04000000

/* Convert any DRAM address to its writable (0x20...) alias */
#define RW_ALIAS(a)          ((((unsigned long)(a)) & 0x03FFFFFFUL) | 0x20000000UL)
/* Convert any DRAM address to its executable (0xA0...) alias */
#define EXEC_ALIAS(a)        ((((unsigned long)(a)) & 0x03FFFFFFUL) | 0xA0000000UL)

#endif /* FW_LAYOUT_H */
