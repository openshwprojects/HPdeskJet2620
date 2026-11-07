/* Line editor and command dispatcher shared by BL and application. */
#ifndef FW_CLI_H
#define FW_CLI_H
#include <stdint.h>
#include "config.h"

typedef int (*cli_fn)(int argc, char **argv);

typedef struct {
    const char *name;
    const char *help;
    cli_fn fn;
} cli_cmd_t;

typedef struct {
    const cli_cmd_t *cmds;
    int count;
    const char *prompt;
    char line[CLI_LINE_MAX + 1];
    int len;
    uint32_t executed;      /* command lines run so far */
    void (*idle)(void);     /* called while waiting for input (may be NULL) */
} cli_t;

void cli_init(cli_t *cli, const cli_cmd_t *cmds, int count, const char *prompt);
void cli_prompt(cli_t *cli);
/* Feed one received byte; executes the line on CR/LF. Returns 1 when a line ran. */
int  cli_feed(cli_t *cli, int c);
/* Tokenize and run one command line in place. */
int  cli_execute(cli_t *cli, char *line);
void cli_print_help(const cli_t *cli);
/* Blocking REPL. */
void cli_loop(cli_t *cli);

#endif
