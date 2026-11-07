#ifndef _SHIM_ERRNO_H
#define _SHIM_ERRNO_H

extern int errno;

#define EPERM        1
#define ENOENT       2
#define EINTR        4
#define EIO          5
#define EBADF        9
#define EAGAIN       11
#define EWOULDBLOCK  EAGAIN
#define ENOMEM       12
#define EACCES       13
#define EFAULT       14
#define EBUSY        16
#define EEXIST       17
#define ENODEV       19
#define ENOTDIR      20
#define EISDIR       21
#define EINVAL       22
#define ENFILE       23
#define EMFILE       24
#define ENOSPC       28
#define EROFS        30
#define ERANGE       34
#define ENAMETOOLONG 36
#define ENOTEMPTY    39
#define ENOTSOCK     88
#define EAFNOSUPPORT 97
#define EADDRINUSE   98
#define EADDRNOTAVAIL 99
#define ENETDOWN     100
#define ENETUNREACH  101
#define ECONNABORTED 103
#define ECONNRESET   104
#define ENOBUFS      105
#define EISCONN      106
#define ENOTCONN     107
#define ETIMEDOUT    110
#define ECONNREFUSED 111
#define EHOSTUNREACH 113
#define EINPROGRESS  115

#endif
