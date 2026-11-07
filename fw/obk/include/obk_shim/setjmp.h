#ifndef _SHIM_SETJMP_H
#define _SHIM_SETJMP_H

typedef unsigned long jmp_buf[12];

int setjmp(jmp_buf env);
void longjmp(jmp_buf env, int val);

#endif
