/* Tunables and documented assumptions. */
#ifndef FW_CONFIG_H
#define FW_CONFIG_H

/*
 * System counter rate. The emulator advances the 32-bit counter at
 * 0xBD040814 by 4 ticks per microsecond (hardware.py: us_ticks=4). The real
 * clock feeding this counter has NOT been measured on the board, so this is
 * only the compiled-in default: at boot the bootloader measures the counter
 * against the UART bit clock (115200 8N1 is established, see
 * analysis/uart_baud.md) and uses the measured rate when it is plausible
 * (timer_calibrate_uart). "uptime" shows which value is in use.
 */
#define TIMER_TICKS_PER_US   4U
#define TIMER_TICKS_PER_MS   (TIMER_TICKS_PER_US * 1000U)
#define TIMER_CAL_BYTES      256U       /* UART bytes timed by the calibration */
#define TIMER_CAL_MIN_X100   25U        /* accept 0.25 .. 1000 ticks per microsecond */
#define TIMER_CAL_MAX_X100   100000U

/* Bounded SPI controller status wait (polls, not time: must not depend on the timer). */
#define SPI_WAIT_SPINS            200000U

/* Bootloader: how long to wait for a key before autobooting the application. */
#ifndef BL_AUTOBOOT_DELAY_MS
#define BL_AUTOBOOT_DELAY_MS 1000U
#endif
/* If the shell was entered by a key press (possibly line noise) and nothing
   is typed for this long, autoboot resumes so an unattended printer does not
   stay parked in the bootloader. 0 disables. */
#ifndef BL_SHELL_IDLE_AUTOBOOT_MS
#define BL_SHELL_IDLE_AUTOBOOT_MS 60000U
#endif
/* "reboot" arms the watchdog and waits this long for the reset before giving
   the shell back with a diagnostic (the reset is unverified on hardware). */
#define REBOOT_WAIT_MS            3000U
/* Consecutive automatic launches after which the bootloader stops autobooting
   an application that never reported itself alive (crash loop through HP's
   exception handlers and the watchdog). */
#define BL_MAX_BOOT_ATTEMPTS      3U
/* XMODEM: give up after this many non-protocol bytes once a transfer started. */
#define XMODEM_MAX_NOISE_BYTES    4096U

/* XMODEM receiver timeouts (milliseconds). */
#define XMODEM_START_TIMEOUT_MS   60000U   /* waiting for the first block */
#define XMODEM_BYTE_TIMEOUT_MS    2000U    /* between bytes inside a block */
#define XMODEM_POLL_INTERVAL_MS   1000U    /* "C" retransmit period while idle */
#define XMODEM_MAX_RETRIES        10

/* SPI flash: how long an erase/program may take before we give up. */
#define FLASH_ERASE_TIMEOUT_MS    3000U
#define FLASH_PROGRAM_TIMEOUT_MS  100U

/* UART line editor */
#define CLI_LINE_MAX              120
#define CLI_MAX_ARGS              12

#endif /* FW_CONFIG_H */
