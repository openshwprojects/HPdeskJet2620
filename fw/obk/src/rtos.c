/*
 * rtos.c - Cooperative FreeRTOS / Beken RTOS shim for Marvell 88PAX01 (ARM946E-S)
 */
#include "rtos.h"
#include "heap.h"
#include "libc.h"
#include "timer.h"

#define MAX_TASKS   12
#define MAX_TIMERS  16

enum {
    TASK_UNUSED = 0,
    TASK_READY,
    TASK_SLEEPING,
    TASK_SUSPENDED,
    TASK_DEAD,
};

typedef struct rtos_ctx {
    uint32_t r4, r5, r6, r7, r8, r9, r10, r11;
    uint32_t sp, lr;
} rtos_ctx_t;

typedef struct rtos_task {
    rtos_ctx_t              ctx;
    uint8_t                 state;
    uint8_t                 priority;
    char                    name[16];
    uint32_t                wake_ms;
    uint8_t                *stack_base;
    uint32_t                stack_bytes;
    beken_thread_function_t fn;
    void                   *arg;
} rtos_task_t;

extern void rtos_switch_context(rtos_ctx_t *old_ctx, const rtos_ctx_t *new_ctx);

static rtos_task_t    g_tasks[MAX_TASKS];
static int            g_cur_task = 0;
static int            g_inited = 0;
static beken_timer_t *g_timers[MAX_TIMERS];
static void         (*g_poll_cb)(void) = NULL;

/* Timer compatibility wrappers around fw/common/timer.h */
uint32_t micros(void)
{
    uint32_t tpus = g_ticks_per_us ? g_ticks_per_us : 180u;
    return timer_ticks() / tpus;
}

uint32_t elapsed_us(uint32_t since)
{
    return micros() - since;
}

uint32_t timer_ms(void)
{
    timer_poll();
    return timer_uptime_ms();
}

void delay_us(uint32_t us)
{
    timer_delay_us(us);
}

static void task_trampoline(void)
{
    rtos_task_t *t = &g_tasks[g_cur_task];
    if (t->fn)
        t->fn(t->arg);
    t->state = TASK_DEAD;
    for (;;)
        rtos_yield();
}

void rtos_init(void)
{
    if (g_inited)
        return;
    memset(g_tasks, 0, sizeof(g_tasks));
    memset(g_timers, 0, sizeof(g_timers));
    g_tasks[0].state = TASK_READY;
    strcpy(g_tasks[0].name, "main");
    g_cur_task = 0;
    g_inited = 1;
}

void rtos_register_poll(void (*fn)(void))
{
    g_poll_cb = fn;
}

static void fire_due_timers(uint32_t now)
{
    for (int i = 0; i < MAX_TIMERS; i++) {
        beken_timer_t *tm = g_timers[i];
        if (!tm || !tm->running || !tm->cb)
            continue;
        if ((int32_t)(now - tm->next_fire_ms) >= 0) {
            tm->next_fire_ms = now + tm->period_ms;
            tm->cb(tm->arg);
        }
    }
}

void rtos_yield(void)
{
    if (!g_inited)
        return;

    if (g_poll_cb)
        g_poll_cb();

    uint32_t now = timer_ms();
    fire_due_timers(now);

    for (int i = 0; i < MAX_TASKS; i++) {
        if (g_tasks[i].state == TASK_SLEEPING &&
            (int32_t)(now - g_tasks[i].wake_ms) >= 0) {
            g_tasks[i].state = TASK_READY;
        }
        if (g_tasks[i].state == TASK_DEAD && i != g_cur_task) {
            if (g_tasks[i].stack_base) {
                free(g_tasks[i].stack_base);
                g_tasks[i].stack_base = NULL;
            }
            g_tasks[i].state = TASK_UNUSED;
        }
    }

    int next = g_cur_task;
    for (int step = 1; step <= MAX_TASKS; step++) {
        int idx = (g_cur_task + step) % MAX_TASKS;
        if (g_tasks[idx].state == TASK_READY) {
            next = idx;
            break;
        }
    }

    if (next == g_cur_task)
        return;

    int prev = g_cur_task;
    g_cur_task = next;
    rtos_switch_context(&g_tasks[prev].ctx, &g_tasks[next].ctx);
}

OSStatus rtos_create_thread(beken_thread_t *thread, uint8_t priority,
                            const char *name, beken_thread_function_t function,
                            uint32_t stack_size, beken_thread_arg_t arg)
{
    if (!g_inited)
        rtos_init();

    int slot = -1;
    for (int i = 1; i < MAX_TASKS; i++) {
        if (g_tasks[i].state == TASK_UNUSED) {
            slot = i;
            break;
        }
    }
    if (slot < 0)
        return kGeneralErr;

    if (stack_size < 4096)
        stack_size = 4096;
    stack_size = (stack_size + 15u) & ~15u;

    uint8_t *stk = (uint8_t *)malloc(stack_size);
    if (!stk)
        return errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY;

    memset(stk, 0, stack_size);
    rtos_task_t *t = &g_tasks[slot];
    memset(t, 0, sizeof(*t));
    t->state = TASK_READY;
    t->priority = priority;
    if (name) {
        strncpy(t->name, name, sizeof(t->name) - 1);
        t->name[sizeof(t->name) - 1] = '\0';
    }
    t->stack_base = stk;
    t->stack_bytes = stack_size;
    t->fn = function;
    t->arg = arg;

    uint32_t top = ((uint32_t)(stk + stack_size) - 32u) & ~7u;
    t->ctx.sp = top;
    t->ctx.lr = (uint32_t)task_trampoline;

    if (thread)
        *thread = (beken_thread_t)t;
    return kNoErr;
}

OSStatus rtos_delete_thread(beken_thread_t *thread)
{
    rtos_task_t *t = thread ? (rtos_task_t *)(*thread) : &g_tasks[g_cur_task];
    if (!t || t == &g_tasks[0])
        return kNoErr;

    if (thread)
        *thread = NULL;

    if (t == &g_tasks[g_cur_task]) {
        t->state = TASK_DEAD;
        for (;;)
            rtos_yield();
    } else {
        if (t->stack_base) {
            free(t->stack_base);
            t->stack_base = NULL;
        }
        t->state = TASK_UNUSED;
    }
    return kNoErr;
}

OSStatus rtos_suspend_thread(beken_thread_t *thread)
{
    rtos_task_t *t = thread ? (rtos_task_t *)(*thread) : &g_tasks[g_cur_task];
    if (!t || t == &g_tasks[0])
        return kNoErr;
    t->state = TASK_SUSPENDED;
    if (t == &g_tasks[g_cur_task]) {
        while (t->state == TASK_SUSPENDED)
            rtos_yield();
    }
    return kNoErr;
}

BaseType_t xTaskCreate(TaskFunction_t pxTaskCode, const char * const pcName,
                       uint32_t usStackDepth, void * const pvParameters,
                       UBaseType_t uxPriority, TaskHandle_t * const pxCreatedTask)
{
    beken_thread_t h = NULL;
    uint32_t bytes = usStackDepth * sizeof(uint32_t);
    if (bytes < 4096)
        bytes = 4096;
    OSStatus st = rtos_create_thread(&h, (uint8_t)uxPriority, pcName,
                                     (beken_thread_function_t)pxTaskCode,
                                     bytes, pvParameters);
    if (pxCreatedTask)
        *pxCreatedTask = h;
    return (st == kNoErr) ? pdPASS : pdFAIL;
}

void vTaskDelete(TaskHandle_t xTaskToDelete)
{
    beken_thread_t h = xTaskToDelete;
    rtos_delete_thread(xTaskToDelete ? &h : NULL);
}

void vTaskSuspend(TaskHandle_t xTaskToSuspend)
{
    beken_thread_t h = xTaskToSuspend;
    rtos_suspend_thread(xTaskToSuspend ? &h : NULL);
}

void vTaskResume(TaskHandle_t xTaskToResume)
{
    rtos_task_t *t = (rtos_task_t *)xTaskToResume;
    if (t && t->state == TASK_SUSPENDED)
        t->state = TASK_READY;
}

void rtos_delay_milliseconds(uint32_t num_ms)
{
    if (!g_inited) {
        timer_delay_ms(num_ms);
        return;
    }
    if (num_ms == 0) {
        rtos_yield();
        return;
    }
    uint32_t start = timer_ms();
    if (g_cur_task != 0) {
        g_tasks[g_cur_task].wake_ms = start + num_ms;
        g_tasks[g_cur_task].state = TASK_SLEEPING;
        while (g_tasks[g_cur_task].state == TASK_SLEEPING)
            rtos_yield();
    } else {
        while ((uint32_t)(timer_ms() - start) < num_ms)
            rtos_yield();
    }
}

void vTaskDelay(TickType_t xTicksToDelay)
{
    rtos_delay_milliseconds(xTicksToDelay);
}

void delay_ms(uint32_t ms)
{
    if (g_inited)
        rtos_delay_milliseconds(ms);
    else
        timer_delay_ms(ms);
}

void sys_msleep(uint32_t ms)
{
    rtos_delay_milliseconds(ms);
}

TickType_t xTaskGetTickCount(void)
{
    return timer_ms();
}

uint32_t rtos_get_time(void)
{
    return timer_ms();
}

uint32_t sys_now(void)
{
    return timer_ms();
}

void vTaskSetTimeOutState(TimeOut_t * const pxTimeOut)
{
    if (pxTimeOut) {
        pxTimeOut->xOverflowCount = 0;
        pxTimeOut->xTimeOnEntering = timer_ms();
    }
}

SemaphoreHandle_t xSemaphoreCreateMutex(void)
{
    uint32_t *m = (uint32_t *)malloc(sizeof(uint32_t));
    if (m)
        *m = 0;
    return (SemaphoreHandle_t)m;
}

BaseType_t xSemaphoreTake(SemaphoreHandle_t xSemaphore, TickType_t xBlockTime)
{
    (void)xSemaphore;
    (void)xBlockTime;
    return pdTRUE;
}

BaseType_t xSemaphoreGive(SemaphoreHandle_t xSemaphore)
{
    (void)xSemaphore;
    return pdTRUE;
}

void vSemaphoreDelete(SemaphoreHandle_t xSemaphore)
{
    if (xSemaphore)
        free(xSemaphore);
}

OSStatus rtos_init_mutex(beken_mutex_t *mutex)
{
    if (mutex)
        *mutex = (beken_mutex_t)1;
    return kNoErr;
}

OSStatus rtos_lock_mutex(beken_mutex_t *mutex)
{
    (void)mutex;
    return kNoErr;
}

OSStatus rtos_unlock_mutex(beken_mutex_t *mutex)
{
    (void)mutex;
    return kNoErr;
}

OSStatus rtos_deinit_mutex(beken_mutex_t *mutex)
{
    if (mutex)
        *mutex = NULL;
    return kNoErr;
}

OSStatus rtos_init_timer(beken_timer_t *timer, uint32_t time_ms,
                         timer_handler_t function, void *arg)
{
    if (!timer)
        return kGeneralErr;
    timer->period_ms = time_ms ? time_ms : 1;
    timer->next_fire_ms = timer_ms() + timer->period_ms;
    timer->cb = function;
    timer->arg = arg;
    timer->running = 0;
    timer->initialized = 1;

    for (int i = 0; i < MAX_TIMERS; i++) {
        if (g_timers[i] == timer)
            return kNoErr;
    }
    for (int i = 0; i < MAX_TIMERS; i++) {
        if (g_timers[i] == NULL) {
            g_timers[i] = timer;
            break;
        }
    }
    return kNoErr;
}

OSStatus rtos_start_timer(beken_timer_t *timer)
{
    if (!timer)
        return kGeneralErr;
    timer->next_fire_ms = timer_ms() + timer->period_ms;
    timer->running = 1;
    return kNoErr;
}

OSStatus rtos_stop_timer(beken_timer_t *timer)
{
    if (!timer)
        return kGeneralErr;
    timer->running = 0;
    return kNoErr;
}

OSStatus rtos_reload_timer(beken_timer_t *timer)
{
    if (!timer)
        return kGeneralErr;
    timer->next_fire_ms = timer_ms() + timer->period_ms;
    timer->running = 1;
    return kNoErr;
}

OSStatus rtos_deinit_timer(beken_timer_t *timer)
{
    if (!timer)
        return kGeneralErr;
    timer->running = 0;
    timer->initialized = 0;
    for (int i = 0; i < MAX_TIMERS; i++) {
        if (g_timers[i] == timer)
            g_timers[i] = NULL;
    }
    return kNoErr;
}

int rtos_is_timer_running(beken_timer_t *timer)
{
    return (timer && timer->running) ? 1 : 0;
}

int rtos_is_timer_init(beken_timer_t *timer)
{
    return (timer && timer->initialized) ? 1 : 0;
}
