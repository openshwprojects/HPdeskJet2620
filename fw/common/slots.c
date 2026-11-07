#include "slots.h"
#include "layout.h"
#include "spi_flash.h"
#include "xmodem.h"
#include "uart.h"
#include "cache.h"
#include "libmini.h"
#include "fmt.h"
#include "hw.h"

uint32_t slot_offset(int slot) { return slot == 0 ? FLASH_SLOT_A_OFFSET : FLASH_SLOT_B_OFFSET; }
char slot_letter(int slot) { return slot == 0 ? 'A' : 'B'; }

int slot_from_arg(const char *s)
{
    if (!s) return -1;
    if (!strcmp(s, "a") || !strcmp(s, "A") || !strcmp(s, "0")) return 0;
    if (!strcmp(s, "b") || !strcmp(s, "B") || !strcmp(s, "1")) return 1;
    return -1;
}

void print_version(uint32_t v)
{
    uart_printf("%u.%u.%u", VERSION_MAJOR(v), VERSION_MINOR(v), VERSION_PATCH(v));
}

void slot_inspect(int slot, slot_info_t *info, int check_payload)
{
    info->offset = slot_offset(slot);
    dcache_invalidate_range(info->offset, info->offset + IMAGE_HEADER_SIZE);
    info->hdr = (const image_header_t *)flash_mmap(info->offset);
    if (check_payload && info->hdr->magic == IMAGE_MAGIC && info->hdr->payload_size <= APP_CODE_MAX)
        dcache_invalidate_range(info->offset, info->offset + IMAGE_HEADER_SIZE + info->hdr->payload_size);
    info->status = image_validate(info->hdr, IMAGE_TYPE_APP, APP_LOAD_ADDR, APP_CODE_MAX, check_payload);
}

int slot_select_best(slot_info_t infos[FLASH_SLOT_COUNT])
{
    int best = -1, i;
    for (i = 0; i < FLASH_SLOT_COUNT; i++) {
        if (infos[i].status != IMG_OK) continue;
        if (best < 0 || infos[i].hdr->sequence > infos[best].hdr->sequence) best = i;
    }
    return best;
}

void slot_print_table(slot_info_t infos[FLASH_SLOT_COUNT], int best)
{
    int i;
    uart_puts("slot  flash     status            version   seq        size     name\n");
    for (i = 0; i < FLASH_SLOT_COUNT; i++) {
        const slot_info_t *s = &infos[i];
        uart_printf("  %c   0x%06x  %-17s ", slot_letter(i), s->offset, image_status_str(s->status));
        if (s->status == IMG_OK) {
            char name[IMAGE_NAME_LEN + 1];
            char ver[16];
            memcpy(name, s->hdr->name, IMAGE_NAME_LEN);
            name[IMAGE_NAME_LEN] = 0;
            snprintf(ver, sizeof(ver), "%u.%u.%u", VERSION_MAJOR(s->hdr->version),
                     VERSION_MINOR(s->hdr->version), VERSION_PATCH(s->hdr->version));
            uart_printf("%-9s %-10u %-8u %s%s", ver, s->hdr->sequence, s->hdr->payload_size, name,
                        i == best ? "  <- boot" : "");
        }
        uart_puts("\n");
    }
}

int slot_validate_staged(const uint8_t *staged, uint32_t staged_len)
{
    const image_header_t *hdr = (const image_header_t *)staged;
    if (staged_len < IMAGE_HEADER_SIZE) return IMG_BAD_SIZE;
    int st = image_validate(hdr, IMAGE_TYPE_APP, APP_LOAD_ADDR, APP_CODE_MAX, 0);
    if (st != IMG_OK) return st;
    if (hdr->payload_size + IMAGE_HEADER_SIZE > staged_len) return IMG_BAD_SIZE;
    return image_validate(hdr, IMAGE_TYPE_APP, APP_LOAD_ADDR, APP_CODE_MAX, 1);
}

int slot_write(int slot, const uint8_t *image, uint32_t image_len,
               void (*progress)(uint32_t done, uint32_t total))
{
    if (slot < 0 || slot >= FLASH_SLOT_COUNT) return FLASH_ERR_RANGE;
    if (image_len > FLASH_SLOT_SIZE) return FLASH_ERR_RANGE;
    return flash_write_region(slot_offset(slot), image, image_len, progress);
}

int slot_erase(int slot, void (*progress)(uint32_t done, uint32_t total))
{
    uint32_t base, off;
    if (slot < 0 || slot >= FLASH_SLOT_COUNT) return FLASH_ERR_RANGE;
    base = slot_offset(slot);
    /* Erasing the header sector alone already invalidates the slot; erase it
       first so an interrupted erase can never leave a bootable torso. */
    for (off = 0; off < FLASH_SLOT_SIZE; off += FLASH_SECTOR_SIZE) {
        int rc = flash_erase_sector(base + off);
        if (rc != FLASH_OK) return rc;
        if (progress) progress(off + FLASH_SECTOR_SIZE, FLASH_SLOT_SIZE);
    }
    return FLASH_OK;
}

static void update_progress(uint32_t done, uint32_t total)
{
    (void)total;
    if ((done & 0x7FFF) == 0 || done == total) uart_putc('.');
}

int slot_update_over_xmodem(int slot, int current_best_slot)
{
    uint8_t *staging = (uint8_t *)STAGING_ADDR;
    if (slot < 0) {
        slot = current_best_slot == 0 ? 1 : 0;   /* inactive slot by default */
    }
    if (slot == current_best_slot) {
        uart_printf("warning: slot %c holds the currently selected image; it will be replaced\n",
                    slot_letter(slot));
    }
    uart_printf("send the application image (XMODEM-CRC/1K) now, target slot %c ...\n", slot_letter(slot));
    int32_t n = xmodem_receive(staging, STAGING_SIZE);
    uart_puts("\n");
    if (n < 0) {
        uart_printf("xmodem failed: %s\n", xmodem_err_str(n));
        return -1;
    }
    int st = slot_validate_staged(staging, (uint32_t)n);
    if (st != IMG_OK) {
        uart_printf("received %d bytes but the image is invalid: %s\n", (int)n, image_status_str(st));
        return -2;
    }
    const image_header_t *hdr = (const image_header_t *)staging;
    uint32_t image_len = IMAGE_HEADER_SIZE + hdr->payload_size;
    uart_printf("received %d bytes: app version ", (int)n);
    print_version(hdr->version);
    uart_printf(" seq %u, %u payload bytes, crc ok\n", hdr->sequence, hdr->payload_size);
    {
        /* The bootloader boots the highest sequence: tell the developer now if
           this image would lose against the slot that stays in place. */
        int other = slot == 0 ? 1 : 0;
        slot_info_t info;
        slot_inspect(other, &info, 0);
        if (info.status == IMG_OK && info.hdr->sequence >= hdr->sequence) {
            uart_printf("warning: seq %u is not higher than slot %c (seq %u); the bootloader will keep "
                        "booting slot %c unless you 'boot %c' or 'erase %c'\n",
                        hdr->sequence, slot_letter(other), info.hdr->sequence, slot_letter(other),
                        slot_letter(slot), slot_letter(other));
        }
    }
    uart_printf("writing slot %c at 0x%06x ", slot_letter(slot), slot_offset(slot));
    int rc = slot_write(slot, staging, image_len, update_progress);
    uart_puts("\n");
    if (rc != FLASH_OK) {
        uart_printf("flash write failed: %s\n", flash_err_str(rc));
        return -3;
    }
    slot_info_t info;
    slot_inspect(slot, &info, 1);
    if (info.status != IMG_OK) {
        uart_printf("post-write check failed: %s\n", image_status_str(info.status));
        return -4;
    }
    uart_printf("slot %c updated and verified\n", slot_letter(slot));
    return 0;
}
