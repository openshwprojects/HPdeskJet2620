#ifndef _SHIM_UNISTD_H
#define _SHIM_UNISTD_H

#include <stddef.h>
#include <stdint.h>

typedef long ssize_t;

int close(int fd);
void usleep(int r);

#endif
