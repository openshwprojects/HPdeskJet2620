#include "cli.h"
#include "uart.h"
#include "libmini.h"
#include "hw.h"
#include "timer.h"

void cli_init(cli_t *cli, const cli_cmd_t *cmds, int count, const char *prompt)
{
    cli->cmds = cmds;
    cli->count = count;
    cli->prompt = prompt;
    cli->len = 0;
    cli->executed = 0;
    cli->idle = 0;
}

void cli_prompt(cli_t *cli) { uart_puts(cli->prompt); }

void cli_print_help(const cli_t *cli)
{
    int i;
    for (i = 0; i < cli->count; i++) uart_printf("  %-10s %s\n", cli->cmds[i].name, cli->cmds[i].help);
}

int cli_execute(cli_t *cli, char *line)
{
    char *argv[CLI_MAX_ARGS];
    int argc = 0, i;
    char *p = line;
    cli->executed++;
    while (*p) {
        while (*p == ' ' || *p == '\t') *p++ = 0;
        if (!*p) break;
        if (argc >= CLI_MAX_ARGS) {
            uart_printf("too many arguments (max %d)\n", CLI_MAX_ARGS);
            return -1;
        }
        if (*p == '"') {                /* "quoted argument" keeps its spaces */
            argv[argc++] = ++p;
            while (*p && *p != '"') p++;
            if (*p) *p++ = 0;
            continue;
        }
        argv[argc++] = p;
        while (*p && *p != ' ' && *p != '\t') p++;
    }
    if (argc == 0) return 0;
    if (!strcmp(argv[0], "help") || !strcmp(argv[0], "?")) {
        cli_print_help(cli);
        return 0;
    }
    for (i = 0; i < cli->count; i++) {
        if (!strcmp(argv[0], cli->cmds[i].name)) {
            int rc = cli->cmds[i].fn(argc, argv);
            if (rc != 0) uart_printf("error: %s returned %d\n", argv[0], rc);
            return rc;
        }
    }
    uart_printf("unknown command: %s (try help)\n", argv[0]);
    return -1;
}

int cli_feed(cli_t *cli, int c)
{
    if (c == '\r' || c == '\n') {
        if (c == '\n' && cli->len == 0) return 0;   /* LF after CR */
        uart_puts("\n");
        cli->line[cli->len] = 0;
        int ran = cli->len > 0;
        cli->len = 0;
        if (ran) cli_execute(cli, cli->line);
        cli_prompt(cli);
        return 1;
    }
    if (c == 0x08 || c == 0x7F) {
        if (cli->len > 0) { cli->len--; uart_puts("\b \b"); }
        return 0;
    }
    if (c == 0x03) {        /* Ctrl-C: discard the line */
        cli->len = 0;
        uart_puts("^C\n");
        cli_prompt(cli);
        return 0;
    }
    if (c >= 0x20 && c < 0x7F && cli->len < CLI_LINE_MAX) {
        cli->line[cli->len++] = (char)c;
        uart_putc((char)c);
    }
    return 0;
}

void cli_loop(cli_t *cli)
{
    cli_prompt(cli);
    for (;;) {
        int c = uart_getc_nonblock();
        if (c >= 0) cli_feed(cli, c);
        else {
            watchdog_kick();
            timer_poll();
            if (cli->idle) cli->idle();
        }
    }
}
