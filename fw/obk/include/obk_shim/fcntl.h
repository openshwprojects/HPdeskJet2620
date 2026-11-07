#ifndef _SHIM_FCNTL_H
#define _SHIM_FCNTL_H

#define F_GETFL    3
#define F_SETFL    4
#define O_NONBLOCK 0x0004
#define O_NDELAY   O_NONBLOCK

int fcntl(int s, int cmd, int val);

#endif
