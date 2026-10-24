/*
 * c_cli/mesh_fast.c - native launcher for the Mesh CLI.
 *
 *   mesh_fast --version | -v | version     -> prints "mesh <version>" in well under 1 ms
 *   anything else                          -> exec's `python -m mesh.cli <args>` unchanged
 *
 * Why there is no daemon forwarding any more
 * ------------------------------------------
 * The previous version POSTed the bearer token to whatever was listening on the port
 * BEFORE verifying it was the Mesh daemon (the Python CLI and MCP server verify first),
 * silently truncated replies over 64 KB, pasted an unvalidated --namespace into the
 * request line, and printed raw JSON where the Python CLI prints formatted output.
 * Those are correctness and security regressions, so every non-version command now
 * goes through the real Python CLI, which already does the identity check, the flag
 * parsing and the formatting correctly.
 *
 * Build:  gcc -O2 -std=c11 -DMESH_VERSION=\"0.8.2\" c_cli/mesh_fast.c -o c_cli/mesh_fast
 *         (build_and_verify.py passes the real version from mesh/version.py)
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <process.h>
#else
#include <unistd.h>
#endif

#ifndef MESH_VERSION
#define MESH_VERSION "0.8.2"
#endif

static int exec_python(int argc, char **argv) {
    char **av = (char **)calloc((size_t)argc + 4, sizeof(char *));
    if (!av) return 1;
    av[1] = "-m";
    av[2] = "mesh.cli";
    for (int i = 1; i < argc; i++) av[i + 2] = argv[i];
    av[argc + 2] = NULL;
#ifdef _WIN32
    av[0] = "python";
    _execvp("python", (const char *const *)av);
    av[0] = "py";
    _execvp("py", (const char *const *)av);
#else
    av[0] = "python3";
    execvp("python3", av);
    av[0] = "python";
    execvp("python", av);
#endif
    fprintf(stderr, "mesh_fast: could not start Python (tried python3/python/py).\n");
    free(av);
    return 127;
}

int main(int argc, char **argv) {
    if (argc == 2 &&
        (strcmp(argv[1], "--version") == 0 || strcmp(argv[1], "-v") == 0 || strcmp(argv[1], "version") == 0)) {
        puts("mesh " MESH_VERSION);
        return 0;
    }
    return exec_python(argc, argv);
}
