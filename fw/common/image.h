/*
 * Image header shared by BL and application images and by fw/image.py.
 * All fields big-endian u32 (native for this CPU). 64 bytes total.
 */
#ifndef FW_IMAGE_H
#define FW_IMAGE_H
#include <stdint.h>
#include "layout.h"

#define IMAGE_MAGIC          0x4850444AU   /* "HPDJ" */
#define IMAGE_HEADER_VERSION 1U
#define IMAGE_TYPE_BL        1U
#define IMAGE_TYPE_APP       2U
#define IMAGE_NAME_LEN       20

typedef struct {
    uint32_t magic;          /* IMAGE_MAGIC */
    uint32_t header_version; /* IMAGE_HEADER_VERSION */
    uint32_t image_type;     /* IMAGE_TYPE_* */
    uint32_t version;        /* major<<24 | minor<<16 | patch<<8 */
    uint32_t sequence;       /* monotonically increasing; the BL boots the highest valid */
    uint32_t payload_size;   /* bytes following the header */
    uint32_t load_address;   /* where the payload is copied (0xA0... alias) */
    uint32_t entry_address;  /* first instruction, ARM state */
    uint32_t payload_crc32;  /* CRC-32/IEEE of the payload */
    uint32_t build_time;     /* unix seconds */
    char     name[IMAGE_NAME_LEN];
    uint32_t header_crc32;   /* CRC-32/IEEE of the 60 bytes above */
} __attribute__((aligned(8))) image_header_t;   /* 8-aligned: GCC may use LDRD on adjacent fields */

_Static_assert(sizeof(image_header_t) == IMAGE_HEADER_SIZE, "image header must be 64 bytes");
_Static_assert((FLASH_BL_OFFSET | FLASH_SLOT_A_OFFSET | FLASH_SLOT_B_OFFSET | STAGING_ADDR) % 8 == 0,
               "image headers must sit at 8-byte aligned addresses");
#define IMAGE_SEQUENCE_MAX 0x7FFFFFFFU   /* leaves room to outrank any accepted image */

#define VERSION_MAJOR(v) (((v) >> 24) & 0xFF)
#define VERSION_MINOR(v) (((v) >> 16) & 0xFF)
#define VERSION_PATCH(v) (((v) >> 8) & 0xFF)

enum image_status {
    IMG_OK = 0,
    IMG_NO_MAGIC,
    IMG_BAD_HEADER_CRC,
    IMG_BAD_TYPE,
    IMG_BAD_SIZE,
    IMG_BAD_ADDRESS,
    IMG_BAD_PAYLOAD_CRC,
    IMG_BAD_SEQUENCE,
    IMG_MISALIGNED,
};

/* Validate a header (and optionally its payload) that sits at `hdr`. The
   payload is expected right after the header. `max_payload` bounds the size
   field; `expect_type`/`expect_load` constrain the image kind. */
int image_validate(const image_header_t *hdr, uint32_t expect_type, uint32_t expect_load,
                   uint32_t max_payload, int check_payload);
const char *image_status_str(int status);

#endif
