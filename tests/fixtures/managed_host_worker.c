/* Native stand-in: executable identity survives its managed-path placement.
 * EOF releases the entire fixture, including its optional same-binary child.
 */
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc == 3 && (strcmp(argv[1], "--child") == 0 ||
                     strcmp(argv[1], "--orphan-child") == 0)) {
        pid_t child = fork();
        if (child < 0) return 2;
        if (child == 0) {
            execl(argv[2], argv[2], "--hold", (char *)NULL);
            _exit(3);
        }
        if (strcmp(argv[1], "--orphan-child") == 0) return 0;
        int status;
        return waitpid(child, &status, 0) == child ? 0 : 4;
    }
    printf("ready %ld\n", (long)getpid());
    fflush(stdout);
    char byte;
    while (read(STDIN_FILENO, &byte, 1) > 0) {}
    return 0;
}
