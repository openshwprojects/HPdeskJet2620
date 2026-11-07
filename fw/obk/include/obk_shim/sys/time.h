#ifndef _SHIM_SYS_TIME_H
#define _SHIM_SYS_TIME_H

#include <stdint.h>

struct timeval {
    long tv_sec;
    long tv_usec;
};

#endif
