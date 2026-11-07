#ifndef _SHIM_LWIP_SOCKETS_H
#define _SHIM_LWIP_SOCKETS_H

#include <stddef.h>
#include <stdint.h>
#include "../errno.h"
#include "../fcntl.h"
#include "../sys/time.h"
#include "inet.h"
#include "ip_addr.h"

#define MEMP_NUM_TCP_PCB 8
#define LWIP_SO_RCVTIMEO 1

typedef uint32_t socklen_t;
typedef uint8_t  sa_family_t;

#define AF_UNSPEC   0
#define AF_INET     2
#define PF_INET     AF_INET
#define PF_UNSPEC   AF_UNSPEC

#define SOCK_STREAM 1
#define SOCK_DGRAM  2
#define SOCK_RAW    3

#define IPPROTO_IP  0
#define IPPROTO_ICMP 1
#define IPPROTO_TCP 6
#define IPPROTO_UDP 17

#define SOL_SOCKET   0xfff
#define SO_REUSEADDR 0x0004
#define SO_KEEPALIVE 0x0008
#define SO_BROADCAST 0x0020
#define SO_SNDBUF    0x1001
#define SO_RCVBUF    0x1002
#define SO_SNDTIMEO  0x1005
#define SO_RCVTIMEO  0x1006
#define SO_ERROR     0x1007
#define SO_NO_CHECK  0x100a

#define IP_TOS             1
#define IP_TTL             2
#define IP_MULTICAST_IF    32
#define IP_MULTICAST_TTL   33
#define IP_MULTICAST_LOOP  34
#define IP_ADD_MEMBERSHIP  35
#define IP_DROP_MEMBERSHIP 36

#define TCP_NODELAY    0x01
#define TCP_KEEPALIVE  0x02
#define TCP_KEEPIDLE   0x03
#define TCP_KEEPINTVL  0x04
#define TCP_KEEPCNT    0x05

#define MSG_PEEK       0x01
#define MSG_WAITALL    0x02
#define MSG_OOB        0x04
#define MSG_DONTWAIT   0x08
#define MSG_MORE       0x10

#define FIONBIO        0x8004667eUL

struct sockaddr_in {
    uint8_t        sin_len;
    sa_family_t    sin_family;
    in_port_t      sin_port;
    struct in_addr sin_addr;
    char           sin_zero[8];
};

struct sockaddr {
    uint8_t     sa_len;
    sa_family_t sa_family;
    char        sa_data[14];
};

struct sockaddr_storage {
    uint8_t     s2_len;
    sa_family_t ss_family;
    char        s2_data1[2];
    uint32_t    s2_data2[3];
};

struct ip_mreq {
    struct in_addr imr_multiaddr;
    struct in_addr imr_interface;
};

#define FD_SETSIZE 32
typedef struct fd_set {
    uint32_t fds_bits;
} fd_set;

#define FD_ZERO(p)      do { (p)->fds_bits = 0; } while (0)
#define FD_SET(n, p)    do { if ((unsigned)(n) < 32) (p)->fds_bits |= (1u << (unsigned)(n)); } while (0)
#define FD_CLR(n, p)    do { if ((unsigned)(n) < 32) (p)->fds_bits &= ~(1u << (unsigned)(n)); } while (0)
#define FD_ISSET(n, p)  (((unsigned)(n) < 32) ? (((p)->fds_bits & (1u << (unsigned)(n))) != 0) : 0)

int lwip_socket(int domain, int type, int protocol);
int lwip_bind(int s, const struct sockaddr *name, socklen_t namelen);
int lwip_listen(int s, int backlog);
int lwip_accept(int s, struct sockaddr *addr, socklen_t *addrlen);
int lwip_connect(int s, const struct sockaddr *name, socklen_t namelen);
int lwip_send(int s, const void *data, size_t size, int flags);
int lwip_sendto(int s, const void *data, size_t size, int flags,
                const struct sockaddr *to, socklen_t tolen);
int lwip_recv(int s, void *mem, size_t len, int flags);
int lwip_recvfrom(int s, void *mem, size_t len, int flags,
                  struct sockaddr *from, socklen_t *fromlen);
int lwip_read(int s, void *mem, size_t len);
int lwip_write(int s, const void *data, size_t size);
int lwip_close(int s);
int lwip_close_force(int s);
int lwip_shutdown(int s, int how);
int lwip_setsockopt(int s, int level, int optname, const void *optval, socklen_t optlen);
int lwip_getsockopt(int s, int level, int optname, void *optval, socklen_t *optlen);
int lwip_getsockname(int s, struct sockaddr *name, socklen_t *namelen);
int lwip_getpeername(int s, struct sockaddr *name, socklen_t *namelen);
int lwip_ioctl(int s, long cmd, void *argp);
int lwip_fcntl(int s, int cmd, int val);
int lwip_select(int maxfdp1, fd_set *readset, fd_set *writeset,
                fd_set *exceptset, struct timeval *timeout);

#define socket      lwip_socket
#define bind        lwip_bind
#define listen      lwip_listen
#define accept      lwip_accept
#define connect     lwip_connect
#define send        lwip_send
#define sendto      lwip_sendto
#define recv        lwip_recv
#define recvfrom    lwip_recvfrom
#define closesocket lwip_close
#define close       lwip_close
#define shutdown    lwip_shutdown
#define setsockopt  lwip_setsockopt
#define getsockopt  lwip_getsockopt
#define getsockname lwip_getsockname
#define getpeername lwip_getpeername
#define ioctlsocket lwip_ioctl
#define fcntl       lwip_fcntl
#define select      lwip_select

#endif
