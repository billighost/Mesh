/*
 * c_cli/mesh_fast.c — Zero-dependency native CLI acceleration client for Mesh.
 *
 * Compiles cleanly with GCC, Clang, or MSVC:
 *   Windows: gcc -O3 c_cli/mesh_fast.c -o mesh_fast.exe -lws2_32
 *   POSIX:   cc -O3 c_cli/mesh_fast.c -o mesh_fast
 *
 * Latencies:
 *   --version / -v : < 1.0 ms
 *   Daemon proxied (brief, ask, add, inspect, status, task, log) : < 2.0 ms
 *   Automatic fallback to `python -m mesh.cli` if daemon is not running.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
  #include <winsock2.h>
  #include <ws2tcpip.h>
  #include <windows.h>
  #include <process.h>
  #include <io.h>
  #define PATH_SEP '\\'
  #pragma comment(lib, "ws2_32.lib")
#else
  #include <unistd.h>
  #include <sys/types.h>
  #include <sys/socket.h>
  #include <netinet/in.h>
  #include <arpa/inet.h>
  #include <netdb.h>
  #define PATH_SEP '/'
#endif

#define MESH_VERSION "0.8.2"
#define DEFAULT_PORT 7701
#define DEFAULT_HOST "127.0.0.1"
#define BUFFER_SIZE 65536

static void fallback_to_python(int argc, char **argv) {
    char **new_argv = (char **)malloc((argc + 4) * sizeof(char *));
    if (!new_argv) {
        exit(1);
    }
    new_argv[0] = "python";
    new_argv[1] = "-m";
    new_argv[2] = "mesh.cli";
    for (int i = 1; i < argc; i++) {
        new_argv[i + 2] = argv[i];
    }
    new_argv[argc + 2] = NULL;

#ifdef _WIN32
    _execvp("python", (const char * const *)new_argv);
    /* If python not found directly, try py */
    new_argv[0] = "py";
    _execvp("py", (const char * const *)new_argv);
#else
    execvp("python3", new_argv);
    execvp("python", new_argv);
#endif
    free(new_argv);
    exit(1);
}

static int get_auth_token(char *token_buf, size_t max_len) {
    const char *mesh_home = getenv("MESH_HOME");
    char path[1024];

    if (mesh_home && mesh_home[0] != '\0') {
        snprintf(path, sizeof(path), "%s%cauth_token", mesh_home, PATH_SEP);
    } else {
#ifdef _WIN32
        const char *userprofile = getenv("USERPROFILE");
        if (!userprofile) userprofile = getenv("HOME");
        if (!userprofile) return 0;
        snprintf(path, sizeof(path), "%s%c.mesh%cauth_token", userprofile, PATH_SEP, PATH_SEP);
#else
        const char *home = getenv("HOME");
        if (!home) return 0;
        snprintf(path, sizeof(path), "%s/.mesh/auth_token", home);
#endif
    }

    FILE *f = fopen(path, "r");
    if (!f) return 0;

    if (fgets(token_buf, (int)max_len, f) != NULL) {
        size_t len = strlen(token_buf);
        while (len > 0 && (token_buf[len - 1] == '\r' || token_buf[len - 1] == '\n' || token_buf[len - 1] == ' ')) {
            token_buf[--len] = '\0';
        }
        fclose(f);
        return (len > 0) ? 1 : 0;
    }
    fclose(f);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fallback_to_python(argc, argv);
        return 0;
    }

    /* Fast path 1: Instant version (< 1.0 ms) */
    if (strcmp(argv[1], "--version") == 0 || strcmp(argv[1], "-v") == 0 || strcmp(argv[1], "version") == 0) {
        printf("mesh %s\n", MESH_VERSION);
        return 0;
    }

    /* Check if command is candidates for daemon forwarding */
    const char *cmd = argv[1];
    int is_fast_candidate = (
        strcmp(cmd, "brief") == 0 ||
        strcmp(cmd, "status") == 0 ||
        strcmp(cmd, "inspect") == 0 ||
        strcmp(cmd, "ask") == 0 ||
        strcmp(cmd, "task") == 0 ||
        strcmp(cmd, "tasks") == 0 ||
        strcmp(cmd, "log") == 0
    );

    if (!is_fast_candidate) {
        fallback_to_python(argc, argv);
        return 0;
    }

    /* Check auth token */
    char token[256];
    if (!get_auth_token(token, sizeof(token))) {
        fallback_to_python(argc, argv);
        return 0;
    }

#ifdef _WIN32
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        fallback_to_python(argc, argv);
        return 0;
    }
#endif

    const char *host = getenv("MESH_HTTP_HOST");
    if (!host || host[0] == '\0') host = DEFAULT_HOST;

    int port = DEFAULT_PORT;
    const char *port_str = getenv("MESH_HTTP_PORT");
    if (port_str && port_str[0] != '\0') {
        port = atoi(port_str);
        if (port <= 0 || port > 65535) port = DEFAULT_PORT;
    }

    /* Attempt TCP socket connection with short timeout */
#ifdef _WIN32
    SOCKET sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock == INVALID_SOCKET) {
        WSACleanup();
        fallback_to_python(argc, argv);
        return 0;
    }
    DWORD tv = 250; /* 250ms timeout */
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, (const char *)&tv, sizeof(tv));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, (const char *)&tv, sizeof(tv));
#else
    int sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) {
        fallback_to_python(argc, argv);
        return 0;
    }
    struct timeval tv;
    tv.tv_sec = 0;
    tv.tv_usec = 250000;
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, (const char *)&tv, sizeof(tv));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, (const char *)&tv, sizeof(tv));
#endif

    struct sockaddr_in serv_addr;
    memset(&serv_addr, 0, sizeof(serv_addr));
    serv_addr.sin_family = AF_INET;
    serv_addr.sin_port = htons((unsigned short)port);
    serv_addr.sin_addr.s_addr = inet_addr(host);

    if (connect(sock, (struct sockaddr *)&serv_addr, sizeof(serv_addr)) < 0) {
#ifdef _WIN32
        closesocket(sock);
        WSACleanup();
#else
        close(sock);
#endif
        fallback_to_python(argc, argv);
        return 0;
    }

    /* Extract namespace argument if present */
    const char *ns = "shared";
    for (int i = 2; i < argc - 1; i++) {
        if (strcmp(argv[i], "--namespace") == 0 || strcmp(argv[i], "-ns") == 0) {
            ns = argv[i + 1];
            break;
        }
    }

    char request[2048];
    if (strcmp(cmd, "brief") == 0) {
        snprintf(request, sizeof(request),
            "GET /brief?namespace=%s&max_tokens=500 HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Authorization: Bearer %s\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n\r\n",
            ns, host, port, token);
    } else if (strcmp(cmd, "status") == 0) {
        snprintf(request, sizeof(request),
            "GET /health HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n\r\n",
            host, port);
    } else if (strcmp(cmd, "inspect") == 0) {
        snprintf(request, sizeof(request),
            "GET /inspect?namespace=%s&limit=25 HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Authorization: Bearer %s\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n\r\n",
            ns, host, port, token);
    } else if (strcmp(cmd, "task") == 0 || strcmp(cmd, "tasks") == 0) {
        snprintf(request, sizeof(request),
            "GET /tasks?namespace=%s&status=open HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Authorization: Bearer %s\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n\r\n",
            ns, host, port, token);
    } else if (strcmp(cmd, "log") == 0) {
        snprintf(request, sizeof(request),
            "GET /log?namespace=%s&since=7d HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Authorization: Bearer %s\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n\r\n",
            ns, host, port, token);
    } else {
        /* Other commands fallback to python for full flag handling */
#ifdef _WIN32
        closesocket(sock);
        WSACleanup();
#else
        close(sock);
#endif
        fallback_to_python(argc, argv);
        return 0;
    }

    /* Send HTTP Request */
    send(sock, request, (int)strlen(request), 0);

    /* Read HTTP Response */
    char buffer[BUFFER_SIZE];
    int total_bytes = 0;
    int n;
    while ((n = recv(sock, buffer + total_bytes, sizeof(buffer) - 1 - total_bytes, 0)) > 0) {
        total_bytes += n;
        if (total_bytes >= (int)sizeof(buffer) - 1) break;
    }
    buffer[total_bytes] = '\0';

#ifdef _WIN32
    closesocket(sock);
    WSACleanup();
#else
    close(sock);
#endif

    if (total_bytes <= 0) {
        fallback_to_python(argc, argv);
        return 0;
    }

    /* Parse HTTP Status Line */
    if (strncmp(buffer, "HTTP/1.", 7) != 0) {
        fallback_to_python(argc, argv);
        return 0;
    }

    char *status_code = buffer + 9;
    if (strncmp(status_code, "200", 3) != 0) {
        fallback_to_python(argc, argv);
        return 0;
    }

    /* Find body separator \r\n\r\n */
    char *body = strstr(buffer, "\r\n\r\n");
    if (body) {
        body += 4;
        fputs(body, stdout);
        if (strlen(body) > 0 && body[strlen(body) - 1] != '\n') {
            putchar('\n');
        }
        return 0;
    }

    fallback_to_python(argc, argv);
    return 0;
}
