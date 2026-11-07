#ifndef RTOS_H
#define RTOS_H

#include <stdint.h>
#include <stddef.h>

typedef void *TaskHandle_t;
typedef void *SemaphoreHandle_t;
typedef uint32_t TickType_t;
typedef long BaseType_t;
typedef unsigned long UBaseType_t;
typedef void (*TaskFunction_t)(void *);

typedef void *beken_thread_t;
typedef void *beken_thread_arg_t;
typedef void (*beken_thread_function_t)(beken_thread_arg_t arg);
typedef void *beken_mutex_t;
typedef int   OSStatus;

typedef void (*timer_handler_t)(void *arg);

typedef struct beken_timer_s {
    uint32_t        period_ms;
    uint32_t        next_fire_ms;
    timer_handler_t cb;
    void           *arg;
    uint8_t         running;
    uint8_t         initialized;
} beken_timer_t;

typedef beken_timer_t ext_timer_t;

#ifndef pdPASS
#define pdPASS               1
#define pdFAIL               0
#define pdTRUE               1
#define pdFALSE              0
#define errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY (-1)
#define portTICK_PERIOD_MS   1
#define portTICK_RATE_MS     1
#define portMAX_DELAY        0xFFFFFFFFu
#define configMINIMAL_STACK_SIZE 1024
#define tskIDLE_PRIORITY     0
#endif

#ifndef kNoErr
#define kNoErr               0
#define kGeneralErr          (-1)
#endif

void rtos_init(void);
void rtos_register_poll(void (*fn)(void));
void rtos_yield(void);

BaseType_t xTaskCreate(TaskFunction_t pxTaskCode, const char * const pcName,
                       uint32_t usStackDepth, void * const pvParameters,
                       UBaseType_t uxPriority, TaskHandle_t * const pxCreatedTask);
void vTaskDelete(TaskHandle_t xTaskToDelete);
void vTaskSuspend(TaskHandle_t xTaskToSuspend);
void vTaskResume(TaskHandle_t xTaskToResume);
void vTaskDelay(TickType_t xTicksToDelay);
TickType_t xTaskGetTickCount(void);

typedef struct xTIME_OUT {
    BaseType_t xOverflowCount;
    TickType_t xTimeOnEntering;
} TimeOut_t;

void vTaskSetTimeOutState(TimeOut_t * const pxTimeOut);

SemaphoreHandle_t xSemaphoreCreateMutex(void);
BaseType_t xSemaphoreTake(SemaphoreHandle_t xSemaphore, TickType_t xBlockTime);
BaseType_t xSemaphoreGive(SemaphoreHandle_t xSemaphore);
void vSemaphoreDelete(SemaphoreHandle_t xSemaphore);

OSStatus rtos_create_thread(beken_thread_t *thread, uint8_t priority,
                            const char *name, beken_thread_function_t function,
                            uint32_t stack_size, beken_thread_arg_t arg);
OSStatus rtos_delete_thread(beken_thread_t *thread);
OSStatus rtos_suspend_thread(beken_thread_t *thread);
void     rtos_delay_milliseconds(uint32_t num_ms);
uint32_t rtos_get_time(void);

OSStatus rtos_init_mutex(beken_mutex_t *mutex);
OSStatus rtos_lock_mutex(beken_mutex_t *mutex);
OSStatus rtos_unlock_mutex(beken_mutex_t *mutex);
OSStatus rtos_deinit_mutex(beken_mutex_t *mutex);

OSStatus rtos_init_timer(beken_timer_t *timer, uint32_t time_ms,
                         timer_handler_t function, void *arg);
OSStatus rtos_start_timer(beken_timer_t *timer);
OSStatus rtos_stop_timer(beken_timer_t *timer);
OSStatus rtos_reload_timer(beken_timer_t *timer);
OSStatus rtos_deinit_timer(beken_timer_t *timer);
int      rtos_is_timer_running(beken_timer_t *timer);
int      rtos_is_timer_init(beken_timer_t *timer);

void     delay_ms(uint32_t ms);
void     sys_msleep(uint32_t ms);
uint32_t sys_now(void);

#endif
