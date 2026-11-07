#ifndef _SHIM_FREERTOS_H
#define _SHIM_FREERTOS_H

#include "../rtos.h"
#include "../heap.h"

#define portTickType TickType_t
#define xTaskHandle  TaskHandle_t
#define xSemaphoreHandle SemaphoreHandle_t
#define configTICK_RATE_HZ 1000

#endif
