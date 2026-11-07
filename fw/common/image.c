#include "image.h"
#include "crc.h"

int image_validate(const image_header_t *hdr, uint32_t expect_type, uint32_t expect_load,
                   uint32_t max_payload, int check_payload)
{
    if (((uintptr_t)hdr & 7U) != 0) return IMG_MISALIGNED;
    if (hdr->magic != IMAGE_MAGIC) return IMG_NO_MAGIC;
    if (crc32_compute(hdr, IMAGE_HEADER_SIZE - 4) != hdr->header_crc32) return IMG_BAD_HEADER_CRC;
    if (hdr->header_version != IMAGE_HEADER_VERSION || hdr->image_type != expect_type) return IMG_BAD_TYPE;
    if (hdr->sequence > IMAGE_SEQUENCE_MAX) return IMG_BAD_SEQUENCE;
    if (hdr->payload_size == 0 || hdr->payload_size > max_payload || (hdr->payload_size & 3U)) return IMG_BAD_SIZE;
    if (hdr->load_address != expect_load) return IMG_BAD_ADDRESS;
    if (hdr->entry_address < hdr->load_address ||
        hdr->entry_address >= hdr->load_address + hdr->payload_size ||
        (hdr->entry_address & 3U)) return IMG_BAD_ADDRESS;
    if (check_payload) {
        const uint8_t *payload = (const uint8_t *)hdr + IMAGE_HEADER_SIZE;
        if (crc32_compute(payload, hdr->payload_size) != hdr->payload_crc32) return IMG_BAD_PAYLOAD_CRC;
    }
    return IMG_OK;
}

const char *image_status_str(int status)
{
    switch (status) {
    case IMG_OK: return "ok";
    case IMG_NO_MAGIC: return "empty/no magic";
    case IMG_BAD_HEADER_CRC: return "bad header crc";
    case IMG_BAD_TYPE: return "wrong image type";
    case IMG_BAD_SIZE: return "bad size";
    case IMG_BAD_ADDRESS: return "bad load/entry address";
    case IMG_BAD_PAYLOAD_CRC: return "bad payload crc";
    case IMG_BAD_SEQUENCE: return "sequence too large";
    case IMG_MISALIGNED: return "header not 8-byte aligned";
    default: return "?";
    }
}
