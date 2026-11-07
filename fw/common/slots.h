/* Application slot management shared by BL (boot selection, recovery update)
   and the application (self-update into the inactive slot). */
#ifndef FW_SLOTS_H
#define FW_SLOTS_H
#include <stdint.h>
#include "image.h"

typedef struct {
    uint32_t offset;                /* flash offset of the header */
    int status;                     /* enum image_status */
    const image_header_t *hdr;      /* memory-mapped header (valid if status == IMG_OK) */
} slot_info_t;

uint32_t slot_offset(int slot);
char     slot_letter(int slot);
int      slot_from_arg(const char *s);           /* "a"/"A"/"0" -> 0, "b"/"B"/"1" -> 1, else -1 */
/* Inspect one slot, optionally verifying the payload CRC. */
void     slot_inspect(int slot, slot_info_t *info, int check_payload);
/* Highest-sequence valid slot, or -1. */
int      slot_select_best(slot_info_t infos[FLASH_SLOT_COUNT]);
void     slot_print_table(slot_info_t infos[FLASH_SLOT_COUNT], int best);
/* Validate a staged image (header+payload in RAM) for an application slot. */
int      slot_validate_staged(const uint8_t *staged, uint32_t staged_len);
/* Erase + program + verify `image` (header+payload) into `slot`. */
int      slot_write(int slot, const uint8_t *image, uint32_t image_len,
                    void (*progress)(uint32_t done, uint32_t total));
int      slot_erase(int slot, void (*progress)(uint32_t done, uint32_t total));
/* Receive an image over XMODEM into the staging buffer, validate it and write it. */
int      slot_update_over_xmodem(int slot, int current_best_slot);
void     print_version(uint32_t v);

#endif
