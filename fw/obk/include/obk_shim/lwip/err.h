#ifndef _SHIM_LWIP_ERR_H
#define _SHIM_LWIP_ERR_H

#include <stdint.h>

typedef int8_t err_t;
typedef uint8_t u8_t;
typedef int8_t s8_t;
typedef uint16_t u16_t;
typedef int16_t s16_t;
typedef uint32_t u32_t;
typedef int32_t s32_t;

#define ERR_OK          0
#define ERR_MEM        (-1)
#define ERR_BUF        (-2)
#define ERR_TIMEOUT    (-3)
#define ERR_RTE        (-4)
#define ERR_INPROGRESS (-5)
#define ERR_VAL        (-6)
#define ERR_WOULDBLOCK (-7)
#define ERR_USE        (-8)
#define ERR_ALREADY    (-9)
#define ERR_ISCONN     (-10)
#define ERR_CONN       (-11)
#define ERR_IF         (-12)
#define ERR_ABRT       (-13)
#define ERR_RST        (-14)
#define ERR_CLSD       (-15)
#define ERR_ARG        (-16)

#define LWIP_UNUSED_ARG(x) (void)(x)
#define LWIP_CONST_CAST(target_type, val) ((target_type)(uintptr_t)(val))

#endif
