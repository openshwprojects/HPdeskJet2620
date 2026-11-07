#ifndef RTL8196C_COMPAT_H
#define RTL8196C_COMPAT_H

#include <stdint.h>
#include "timer.h"

#define TC_DATA_MAX 0xFFFFFFFFu

uint32_t micros(void);
uint32_t elapsed_us(uint32_t since);
uint32_t timer_ms(void);
void     delay_us(uint32_t us);
void     delay_ms(uint32_t ms);

#endif
