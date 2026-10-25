/*
 * c_cli/mesh_fast.c - native client for the Mesh CLI (C11, no third-party code).
 *
 * Handled natively (no Python start-up, a few milliseconds end to end):
 *
 *   mesh --version | -v | version
 *   mesh ask|search|recall|find QUERY --json [--count N|-n N|--n N] [--namespace NAME]
 *        [--type TYPE] [--tags CSV] [--min-confidence F] [--min-similarity F]
 *        [--no-stale] [--no-other] [--no-private] [--include-quarantined]
 *
 * Everything else - and every case this client is not 100% sure about - is handed to
 * `python -m mesh.cli <same arguments>` and behaves exactly as before. A native answer is
 * only printed when ALL of these hold, otherwise the client silently falls back:
 *
 *   1. The daemon proves its identity FIRST: GET /identity?nonce=<256-bit random> must
 *      return HMAC-SHA256(token, nonce) (same protocol as mesh.daemon.verify_daemon_identity).
 *      The bearer token is never written to a socket before that check succeeds.
 *   2. The token comes from MESH_AUTH_TOKEN / MESH_TOKEN or <mesh dir>/auth_token, exactly like
 *      mesh.daemon.get_or_create_auth_token; a symlinked/reparse mesh dir or token file, a token
 *      file readable by group/other (POSIX), or a token shorter than 16 chars -> fall back so the
 *      Python code (which repairs permissions / refuses links) decides.
 *   3. Only 127.0.0.1 is ever contacted; MESH_HTTP_HOST must be unset or exactly 127.0.0.1.
 *   4. Every argument is validated with the same rules as the Python CLI (namespace regex, memory
 *      type list, canonical numbers); anything argparse might treat differently -> fall back.
 *   5. The reply is read completely (Content-Length or chunked or until close, up to 64 MiB) and
 *      must be a strict JSON object with "status":"ok" whose string/number tokens are exactly what
 *      Python's json.dumps(ensure_ascii=False, indent=2) would re-emit. The output is then
 *      byte-identical to `mesh ask ... --json` (checked by tests/test_native_cli_golden.py).
 *
 * Output differences that cannot be reproduced natively always fall back: non-ASCII text on
 * Windows (Python re-encodes to the console code page) or in a non-UTF-8 POSIX locale.
 *
 * Build: gcc -O2 -std=c11 -DMESH_VERSION=\"0.8.2\" c_cli/mesh_fast.c -o c_cli/mesh_fast
 *        (Windows: add -lws2_32 -lbcrypt -lshell32)
 */
#ifndef _WIN32
#define _POSIX_C_SOURCE 200809L
#endif
#include <ctype.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <bcrypt.h>
#include <shellapi.h>
#include <fcntl.h>
#include <io.h>
typedef SOCKET sock_t;
#define SOCK_BAD INVALID_SOCKET
#define sock_close closesocket
#else
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
typedef int sock_t;
#define SOCK_BAD (-1)
#define sock_close close
#endif

#ifndef MESH_VERSION
#define MESH_VERSION "0.8.2"
#endif

#define MAX_BODY (64u * 1024u * 1024u)
#define MAX_JSON_DEPTH 48

static int g_trace = 0;

static void trace(const char *why) {
    if (g_trace) fprintf(stderr, "mesh_fast: fallback: %s\n", why);
}

/* ------------------------------------------------------------------------------------------ */
/* growable buffer                                                                            */
/* ------------------------------------------------------------------------------------------ */
typedef struct {
    char *d;
    size_t n, cap;
    int oom;
} Buf;

static void buf_init(Buf *b) { b->d = NULL; b->n = b->cap = 0; b->oom = 0; }
static void buf_free(Buf *b) { free(b->d); buf_init(b); }
static int buf_reserve(Buf *b, size_t extra) {
    if (b->oom) return -1;
    if (b->n + extra + 1 <= b->cap) return 0;
    size_t nc = b->cap ? b->cap : 1024;
    while (nc < b->n + extra + 1) {
        if (nc > ((size_t)1 << 40)) { b->oom = 1; return -1; }
        nc *= 2;
    }
    char *p = (char *)realloc(b->d, nc);
    if (!p) { b->oom = 1; return -1; }
    b->d = p;
    b->cap = nc;
    return 0;
}
static void buf_put(Buf *b, const char *s, size_t len) {
    if (buf_reserve(b, len) != 0) return;
    memcpy(b->d + b->n, s, len);
    b->n += len;
    b->d[b->n] = '\0';
}
static void buf_puts(Buf *b, const char *s) { buf_put(b, s, strlen(s)); }
static void buf_putc(Buf *b, char c) { buf_put(b, &c, 1); }

/* ------------------------------------------------------------------------------------------ */
/* SHA-256 / HMAC-SHA256 (FIPS 180-4, RFC 2104)                                               */
/* ------------------------------------------------------------------------------------------ */
typedef struct {
    uint32_t h[8];
    uint8_t buf[64];
    uint64_t len;
    size_t fill;
} Sha256;

static const uint32_t K256[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};

#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha256_block(Sha256 *s, const uint8_t *p) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
        w[i] = ((uint32_t)p[4 * i] << 24) | ((uint32_t)p[4 * i + 1] << 16) | ((uint32_t)p[4 * i + 2] << 8) |
               (uint32_t)p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = s->h[0], b = s->h[1], c = s->h[2], d = s->h[3], e = s->h[4], f = s->h[5], g = s->h[6],
             h = s->h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t S1 = ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t t1 = h + S1 + ch + K256[i] + w[i];
        uint32_t S0 = ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22);
        uint32_t mj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t t2 = S0 + mj;
        h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d;
    s->h[4] += e; s->h[5] += f; s->h[6] += g; s->h[7] += h;
}

static void sha256_init(Sha256 *s) {
    static const uint32_t iv[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(s->h, iv, sizeof iv);
    s->len = 0;
    s->fill = 0;
}

static void sha256_update(Sha256 *s, const uint8_t *p, size_t n) {
    s->len += n;
    while (n > 0) {
        size_t take = 64 - s->fill;
        if (take > n) take = n;
        memcpy(s->buf + s->fill, p, take);
        s->fill += take;
        p += take;
        n -= take;
        if (s->fill == 64) {
            sha256_block(s, s->buf);
            s->fill = 0;
        }
    }
}

static void sha256_final(Sha256 *s, uint8_t out[32]) {
    uint64_t bits = s->len * 8u;
    uint8_t pad = 0x80;
    sha256_update(s, &pad, 1);
    uint8_t z = 0;
    while (s->fill != 56) sha256_update(s, &z, 1);
    uint8_t lenb[8];
    for (int i = 0; i < 8; i++) lenb[i] = (uint8_t)(bits >> (56 - 8 * i));
    sha256_update(s, lenb, 8);
    for (int i = 0; i < 8; i++) {
        out[4 * i] = (uint8_t)(s->h[i] >> 24);
        out[4 * i + 1] = (uint8_t)(s->h[i] >> 16);
        out[4 * i + 2] = (uint8_t)(s->h[i] >> 8);
        out[4 * i + 3] = (uint8_t)s->h[i];
    }
}

static void hmac_sha256(const uint8_t *key, size_t klen, const uint8_t *msg, size_t mlen, uint8_t out[32]) {
    uint8_t k0[64], ipad[64], opad[64], inner[32];
    memset(k0, 0, sizeof k0);
    if (klen > 64) {
        Sha256 t;
        sha256_init(&t);
        sha256_update(&t, key, klen);
        sha256_final(&t, k0);
    } else if (klen) {
        memcpy(k0, key, klen);
    }
    for (int i = 0; i < 64; i++) { ipad[i] = k0[i] ^ 0x36; opad[i] = k0[i] ^ 0x5c; }
    Sha256 s;
    sha256_init(&s);
    sha256_update(&s, ipad, 64);
    sha256_update(&s, msg, mlen);
    sha256_final(&s, inner);
    sha256_init(&s);
    sha256_update(&s, opad, 64);
    sha256_update(&s, inner, 32);
    sha256_final(&s, out);
}

static void hex_lower(const uint8_t *in, size_t n, char *out) {
    static const char *hx = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) { out[2 * i] = hx[in[i] >> 4]; out[2 * i + 1] = hx[in[i] & 15]; }
    out[2 * n] = '\0';
}

static int ct_equal(const char *a, const char *b, size_t n) {
    unsigned char d = 0;
    for (size_t i = 0; i < n; i++) d |= (unsigned char)(a[i] ^ b[i]);
    return d == 0;
}

static int random_bytes(uint8_t *out, size_t n) {
#ifdef _WIN32
    return BCryptGenRandom(NULL, out, (ULONG)n, BCRYPT_USE_SYSTEM_PREFERRED_RNG) == 0 ? 0 : -1;
#else
    FILE *f = fopen("/dev/urandom", "rb");
    if (!f) return -1;
    size_t got = fread(out, 1, n, f);
    fclose(f);
    return got == n ? 0 : -1;
#endif
}

/* ------------------------------------------------------------------------------------------ */
/* strict JSON scanner (validates, never allocates)                                           */
/* ------------------------------------------------------------------------------------------ */
typedef struct { const char *p, *end; } Span;

static const char *skip_ws(const char *p, const char *end) {
    while (p < end && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) p++;
    return p;
}

/* strict UTF-8 sequence length at p (0 = invalid) */
static int utf8_len(const unsigned char *p, const unsigned char *end) {
    unsigned c = p[0];
    if (c < 0x80) return 1;
    if (c >= 0xC2 && c <= 0xDF) return (p + 1 < end && (p[1] & 0xC0) == 0x80) ? 2 : 0;
    if (c >= 0xE0 && c <= 0xEF) {
        if (p + 2 >= end || (p[1] & 0xC0) != 0x80 || (p[2] & 0xC0) != 0x80) return 0;
        if (c == 0xE0 && p[1] < 0xA0) return 0;
        if (c == 0xED && p[1] > 0x9F) return 0;
        return 3;
    }
    if (c >= 0xF0 && c <= 0xF4) {
        if (p + 3 >= end || (p[1] & 0xC0) != 0x80 || (p[2] & 0xC0) != 0x80 || (p[3] & 0xC0) != 0x80) return 0;
        if (c == 0xF0 && p[1] < 0x90) return 0;
        if (c == 0xF4 && p[1] > 0x8F) return 0;
        return 4;
    }
    return 0;
}

static int utf8_valid(const char *s, size_t n) {
    const unsigned char *p = (const unsigned char *)s, *e = p + n;
    while (p < e) {
        int l = utf8_len(p, e);
        if (!l) return 0;
        p += l;
    }
    return 1;
}

/*
 * Scan one JSON string starting at the opening quote. Only strings that Python would re-emit
 * unchanged under json.dumps(ensure_ascii=False) are accepted (raw UTF-8, escapes limited to
 * \" \\ \b \f \n \r \t and lower-case \u00XX for control characters without a short form).
 */
static const char *scan_string(const char *p, const char *end) {
    if (p >= end || *p != '"') return NULL;
    p++;
    while (p < end) {
        unsigned char c = (unsigned char)*p;
        if (c == '"') return p + 1;
        if (c < 0x20) return NULL;
        if (c == '\\') {
            p++;
            if (p >= end) return NULL;
            switch (*p) {
            case '"': case '\\': case 'b': case 'f': case 'n': case 'r': case 't': p++; break;
            case 'u': {
                if (p + 4 >= end) return NULL;
                if (p[1] != '0' || p[2] != '0') return NULL;
                int hi = p[3], lo = p[4];
                if (!((hi == '0' || hi == '1') && ((lo >= '0' && lo <= '9') || (lo >= 'a' && lo <= 'f')))) return NULL;
                int val = (hi - '0') * 16 + (lo <= '9' ? lo - '0' : lo - 'a' + 10);
                if (val == 0x08 || val == 0x09 || val == 0x0a || val == 0x0c || val == 0x0d) return NULL;
                p += 5;
                break;
            }
            default: return NULL;
            }
            continue;
        }
        if (c < 0x80) { p++; continue; }
        int l = utf8_len((const unsigned char *)p, (const unsigned char *)end);
        if (!l) return NULL;
        p += l;
    }
    return NULL;
}

/*
 * Number tokens must be exactly what Python's repr()/int formatting produces: no leading '+',
 * no leading zeros, no trailing zeros in an exponent mantissa, lower-case 'e' with sign and >=2
 * digits, and "-0" (an int) is rejected because Python would print 0.
 */
static const char *scan_number(const char *p, const char *end) {
    const char *s = p;
    int neg = 0;
    if (p < end && *p == '-') { neg = 1; p++; }
    if (p >= end || !isdigit((unsigned char)*p)) return NULL;
    if (*p == '0') {
        p++;
        if (p < end && isdigit((unsigned char)*p)) return NULL;
    } else {
        while (p < end && isdigit((unsigned char)*p)) p++;
    }
    int is_float = 0;
    if (p < end && *p == '.') {
        is_float = 1;
        p++;
        if (p >= end || !isdigit((unsigned char)*p)) return NULL;
        while (p < end && isdigit((unsigned char)*p)) p++;
    }
    if (p < end && (*p == 'e' || *p == 'E')) {
        if (*p == 'E') return NULL;
        is_float = 1;
        p++;
        if (p >= end || (*p != '+' && *p != '-')) return NULL;
        p++;
        int digits = 0;
        while (p < end && isdigit((unsigned char)*p)) { p++; digits++; }
        if (digits < 2) return NULL;
    }
    if (neg && !is_float && (p - s) == 2 && s[1] == '0') return NULL;
    return p;
}

static const char *scan_value(const char *p, const char *end, int depth);

static const char *scan_container(const char *p, const char *end, int depth, char open, char close) {
    if (depth > MAX_JSON_DEPTH) return NULL;
    p = skip_ws(p + 1, end);
    if (p < end && *p == close) return p + 1;
    for (;;) {
        p = skip_ws(p, end);
        if (open == '{') {
            p = scan_string(p, end);
            if (!p) return NULL;
            p = skip_ws(p, end);
            if (p >= end || *p != ':') return NULL;
            p++;
        }
        p = scan_value(p, end, depth + 1);
        if (!p) return NULL;
        p = skip_ws(p, end);
        if (p >= end) return NULL;
        if (*p == ',') { p++; continue; }
        if (*p == close) return p + 1;
        return NULL;
    }
}

static const char *scan_value(const char *p, const char *end, int depth) {
    p = skip_ws(p, end);
    if (p >= end) return NULL;
    switch (*p) {
    case '{': return scan_container(p, end, depth, '{', '}');
    case '[': return scan_container(p, end, depth, '[', ']');
    case '"': return scan_string(p, end);
    case 't': return (end - p >= 4 && !memcmp(p, "true", 4)) ? p + 4 : NULL;
    case 'f': return (end - p >= 5 && !memcmp(p, "false", 5)) ? p + 5 : NULL;
    case 'n': return (end - p >= 4 && !memcmp(p, "null", 4)) ? p + 4 : NULL;
    default: return scan_number(p, end);
    }
}

/* re-emit an already validated value exactly like json.dumps(indent=2, ensure_ascii=False) */
static void emit_pretty(Buf *out, const char *p, const char *end, int level) {
    p = skip_ws(p, end);
    if (p >= end) return;
    if (*p == '{' || *p == '[') {
        char open = *p, close = (open == '{') ? '}' : ']';
        const char *q = skip_ws(p + 1, end);
        if (q < end && *q == close) { buf_putc(out, open); buf_putc(out, close); return; }
        buf_putc(out, open);
        buf_putc(out, '\n');
        int first = 1;
        p = q;
        for (;;) {
            p = skip_ws(p, end);
            if (!first) { /* comma already consumed below */ }
            first = 0;
            for (int i = 0; i < (level + 1) * 2; i++) buf_putc(out, ' ');
            if (open == '{') {
                const char *ke = scan_string(p, end);
                buf_put(out, p, (size_t)(ke - p));
                buf_puts(out, ": ");
                p = skip_ws(ke, end);
                p++; /* ':' */
            }
            const char *ve = scan_value(p, end, 0);
            emit_pretty(out, p, ve, level + 1);
            p = skip_ws(ve, end);
            if (*p == ',') {
                buf_puts(out, ",\n");
                p++;
                continue;
            }
            break;
        }
        buf_putc(out, '\n');
        for (int i = 0; i < level * 2; i++) buf_putc(out, ' ');
        buf_putc(out, close);
        return;
    }
    const char *ve = scan_value(p, end, 0);
    buf_put(out, p, (size_t)(ve - p));
}

/* find top-level key in an object span (last occurrence wins, like a Python dict) */
static int obj_get(const char *p, const char *end, const char *key, Span *val) {
    size_t klen = strlen(key);
    int found = 0;
    p = skip_ws(p, end);
    if (p >= end || *p != '{') return 0;
    p = skip_ws(p + 1, end);
    if (p < end && *p == '}') return 0;
    for (;;) {
        p = skip_ws(p, end);
        const char *ke = scan_string(p, end);
        if (!ke) return found;
        int match = ((size_t)(ke - p) == klen + 2) && p[0] == '"' && !memcmp(p + 1, key, klen);
        p = skip_ws(ke, end);
        p++; /* ':' */
        p = skip_ws(p, end);
        const char *ve = scan_value(p, end, 0);
        if (!ve) return found;
        if (match) { val->p = p; val->end = ve; found = 1; }
        p = skip_ws(ve, end);
        if (p < end && *p == ',') { p++; continue; }
        return found;
    }
}

static void json_escape_into(Buf *out, const char *s) {
    static const char *hx = "0123456789abcdef";
    buf_putc(out, '"');
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        unsigned char c = *p;
        switch (c) {
        case '"': buf_puts(out, "\\\""); break;
        case '\\': buf_puts(out, "\\\\"); break;
        case '\n': buf_puts(out, "\\n"); break;
        case '\r': buf_puts(out, "\\r"); break;
        case '\t': buf_puts(out, "\\t"); break;
        case '\b': buf_puts(out, "\\b"); break;
        case '\f': buf_puts(out, "\\f"); break;
        default:
            if (c < 0x20) {
                buf_puts(out, "\\u00");
                buf_putc(out, hx[c >> 4]);
                buf_putc(out, hx[c & 15]);
            } else {
                buf_putc(out, (char)c);
            }
        }
    }
    buf_putc(out, '"');
}

/* ------------------------------------------------------------------------------------------ */
/* sockets + one HTTP/1.1 exchange                                                            */
/* ------------------------------------------------------------------------------------------ */
static void net_init(void) {
#ifdef _WIN32
    WSADATA w;
    WSAStartup(MAKEWORD(2, 2), &w);
#endif
}

static sock_t tcp_connect_loopback(int port, int timeout_ms) {
    sock_t s = socket(AF_INET, SOCK_STREAM, 0);
    if (s == SOCK_BAD) return SOCK_BAD;
    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_port = htons((unsigned short)port);
    a.sin_addr.s_addr = htonl(0x7F000001u);
#ifdef _WIN32
    u_long nb = 1;
    ioctlsocket(s, FIONBIO, &nb);
#else
    int fl = fcntl(s, F_GETFL, 0);
    fcntl(s, F_SETFL, fl | O_NONBLOCK);
#endif
    int rc = connect(s, (struct sockaddr *)&a, sizeof a);
    if (rc != 0) {
#ifdef _WIN32
        int inprog = (WSAGetLastError() == WSAEWOULDBLOCK);
#else
        int inprog = (errno == EINPROGRESS);
#endif
        if (!inprog) { sock_close(s); return SOCK_BAD; }
        fd_set wf, ef;
        FD_ZERO(&wf);
        FD_ZERO(&ef);
        FD_SET(s, &wf);
        FD_SET(s, &ef);
        struct timeval tv;
        tv.tv_sec = timeout_ms / 1000;
        tv.tv_usec = (timeout_ms % 1000) * 1000;
        if (select((int)s + 1, NULL, &wf, &ef, &tv) <= 0 || !FD_ISSET(s, &wf)) { sock_close(s); return SOCK_BAD; }
        int err = 0;
        socklen_t el = sizeof err;
        if (getsockopt(s, SOL_SOCKET, SO_ERROR, (char *)&err, &el) != 0 || err != 0) { sock_close(s); return SOCK_BAD; }
    }
#ifdef _WIN32
    nb = 0;
    ioctlsocket(s, FIONBIO, &nb);
    DWORD to = (DWORD)timeout_ms;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&to, sizeof to);
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, (const char *)&to, sizeof to);
#else
    fcntl(s, F_SETFL, fl);
    struct timeval tv2;
    tv2.tv_sec = timeout_ms / 1000;
    tv2.tv_usec = (timeout_ms % 1000) * 1000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv2, sizeof tv2);
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv2, sizeof tv2);
#endif
    int one = 1;
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, (const char *)&one, sizeof one);
    return s;
}

static int send_all(sock_t s, const char *d, size_t n) {
    while (n > 0) {
        int chunk = n > 65536 ? 65536 : (int)n;
#ifdef _WIN32
        int w = send(s, d, chunk, 0);
#else
        ssize_t w = send(s, d, (size_t)chunk, 0
#ifdef MSG_NOSIGNAL
                                                  | MSG_NOSIGNAL
#endif
        );
#endif
        if (w <= 0) return -1;
        d += w;
        n -= (size_t)w;
    }
    return 0;
}

static const char *find_hdr_end(const char *d, size_t n) {
    for (size_t i = 0; i + 3 < n; i++)
        if (d[i] == '\r' && d[i + 1] == '\n' && d[i + 2] == '\r' && d[i + 3] == '\n') return d + i;
    return NULL;
}

static int ci_prefix(const char *line, const char *name) {
    size_t l = strlen(name);
    for (size_t i = 0; i < l; i++)
        if (tolower((unsigned char)line[i]) != name[i]) return 0;
    return 1;
}

/* parse the header block [0, hend): content-length and chunked flag */
static void parse_framing(const char *d, const char *hend, long *clen, int *chunked) {
    *clen = -1;
    *chunked = 0;
    for (const char *l = d; l < hend;) {
        const char *e = memchr(l, '\n', (size_t)(hend + 2 - l));
        if (!e) break;
        if (ci_prefix(l, "content-length:")) *clen = strtol(l + 15, NULL, 10);
        if (ci_prefix(l, "transfer-encoding:") && memchr(l, 'c', (size_t)(e - l))) *chunked = 1;
        l = e + 1;
    }
}

/* One request on a fresh connection; returns HTTP status (>0) with the decoded body, or -1. */
static int http_exchange(int port, const char *req, size_t reqlen, int timeout_ms, Buf *body) {
    sock_t s = tcp_connect_loopback(port, timeout_ms);
    if (s == SOCK_BAD) return -1;
    if (send_all(s, req, reqlen) != 0) { sock_close(s); return -1; }

    Buf raw;
    buf_init(&raw);
    char tmp[16384];
    for (;;) {
        int r = recv(s, tmp, sizeof tmp, 0);
        if (r < 0) { sock_close(s); buf_free(&raw); return -1; }
        if (r == 0) break;
        buf_put(&raw, tmp, (size_t)r);
        if (raw.oom || raw.n > MAX_BODY + 65536u) { sock_close(s); buf_free(&raw); return -1; }
        const char *hend = find_hdr_end(raw.d, raw.n); /* recomputed: raw.d may have moved */
        if (hend) {
            size_t hlen = (size_t)(hend - raw.d) + 4;
            long clen;
            int chunked;
            parse_framing(raw.d, hend, &clen, &chunked);
            if (!chunked && clen >= 0 && raw.n >= hlen + (size_t)clen) break;
            if (chunked && raw.n >= 5 && memcmp(raw.d + raw.n - 5, "0\r\n\r\n", 5) == 0) break;
        }
    }
    sock_close(s);
    const char *hend = raw.d ? find_hdr_end(raw.d, raw.n) : NULL;
    if (!hend || raw.n < 12 || strncmp(raw.d, "HTTP/1.", 7) != 0) { buf_free(&raw); return -1; }
    int status = atoi(raw.d + 9);
    long clen;
    int chunked;
    parse_framing(raw.d, hend, &clen, &chunked);
    const char *b = hend + 4;
    size_t avail = raw.n - (size_t)(b - raw.d);
    buf_init(body);
    if (chunked) {
        size_t pos = 0;
        for (;;) {
            size_t ls = pos;
            while (pos < avail && b[pos] != '\n') pos++;
            if (pos >= avail) { buf_free(&raw); buf_free(body); return -1; }
            char *endp;
            unsigned long csz = strtoul(b + ls, &endp, 16);
            if (endp == b + ls) { buf_free(&raw); buf_free(body); return -1; }
            pos++;
            if (csz == 0) break;
            if (csz > MAX_BODY || pos + csz > avail) { buf_free(&raw); buf_free(body); return -1; }
            buf_put(body, b + pos, csz);
            pos += csz;
            if (pos + 2 > avail || b[pos] != '\r' || b[pos + 1] != '\n') { buf_free(&raw); buf_free(body); return -1; }
            pos += 2;
        }
    } else if (clen >= 0) {
        if ((size_t)clen > avail || (size_t)clen > MAX_BODY) { buf_free(&raw); buf_free(body); return -1; }
        buf_put(body, b, (size_t)clen);
    } else {
        buf_put(body, b, avail);
    }
    buf_free(&raw);
    if (body->oom) { buf_free(body); return -1; }
    if (!body->d) buf_put(body, "", 0);
    return status;
}

/* ------------------------------------------------------------------------------------------ */
/* configuration (must mirror the Python CLI / daemon rules or fall back)                     */
/* ------------------------------------------------------------------------------------------ */
static int ieq(const char *a, const char *b) {
    while (*a && *b) {
        if (tolower((unsigned char)*a) != tolower((unsigned char)*b)) return 0;
        a++;
        b++;
    }
    return *a == *b;
}

/* mirrors envutil.env_bool-style truthiness used for MESH_NO_DAEMON_PROXY: 1/true/yes after strip */
static int env_truthy(const char *name) {
    const char *v = getenv(name);
    if (!v) return 0;
    char t[16];
    while (*v == ' ' || *v == '\t') v++;
    size_t n = strlen(v);
    while (n > 0 && (v[n - 1] == ' ' || v[n - 1] == '\t')) n--;
    if (n == 0 || n >= sizeof t) return 0;
    memcpy(t, v, n);
    t[n] = '\0';
    return ieq(t, "1") || ieq(t, "true") || ieq(t, "yes");
}

static int has_high_bit(const char *s) {
    for (; *s; s++)
        if ((unsigned char)*s >= 0x80) return 1;
    return 0;
}

/* strip ASCII whitespace in place (returns start); caller guarantees no high-bit bytes */
static char *strip_ascii(char *s) {
    while (*s == ' ' || *s == '\t' || *s == '\n' || *s == '\r' || *s == '\v' || *s == '\f') s++;
    size_t n = strlen(s);
    while (n > 0 && (s[n - 1] == ' ' || s[n - 1] == '\t' || s[n - 1] == '\n' || s[n - 1] == '\r' ||
                     s[n - 1] == '\v' || s[n - 1] == '\f'))
        s[--n] = '\0';
    return s;
}

#ifdef _WIN32
#define PATH_SEP '\\'
#else
#define PATH_SEP '/'
#endif

/* mesh dir = $MESH_HOME | $MESH_DIR | ~/.mesh  (audit.get_mesh_dir) */
static int mesh_dir(char *out, size_t cap) {
    const char *ov = getenv("MESH_HOME");
    if (!ov || !*ov) ov = getenv("MESH_DIR");
    char tmp[1024];
    if (ov) {
        size_t n = strlen(ov);
        if (n >= sizeof tmp) return -1;
        memcpy(tmp, ov, n + 1);
        char *t = strip_ascii(tmp);
        if (*t) {
            if (*t == '~' || has_high_bit(t)) return -1; /* expanduser / Unicode path: let Python decide */
            if (strlen(t) + 1 > cap) return -1;
            strcpy(out, t);
            return 0;
        }
    }
#ifdef _WIN32
    const char *home = getenv("USERPROFILE");
#else
    const char *home = getenv("HOME");
#endif
    if (!home || !*home || has_high_bit(home) || strlen(home) + 8 > cap) return -1;
    snprintf(out, cap, "%s%c.mesh", home, PATH_SEP);
    return 0;
}

static int path_is_link_or_missing_dir(const char *path, int want_dir) {
#ifdef _WIN32
    DWORD a = GetFileAttributesA(path);
    if (a == INVALID_FILE_ATTRIBUTES) return 1;
    if (a & FILE_ATTRIBUTE_REPARSE_POINT) return 1;
    if (want_dir && !(a & FILE_ATTRIBUTE_DIRECTORY)) return 1;
    if (!want_dir && (a & FILE_ATTRIBUTE_DIRECTORY)) return 1;
    return 0;
#else
    struct stat st;
    if (lstat(path, &st) != 0) return 1;
    if (S_ISLNK(st.st_mode)) return 1;
    if (want_dir ? !S_ISDIR(st.st_mode) : !S_ISREG(st.st_mode)) return 1;
    return 0;
#endif
}

static int file_exists_follow(const char *path) {
#ifdef _WIN32
    return GetFileAttributesA(path) != INVALID_FILE_ATTRIBUTES;
#else
    struct stat st;
    return stat(path, &st) == 0;
#endif
}

/* token = $MESH_AUTH_TOKEN | $MESH_TOKEN | <mesh dir>/auth_token  (daemon.get_or_create_auth_token) */
static int load_token(const char *mdir, char *tok, size_t cap) {
    const char *e = getenv("MESH_AUTH_TOKEN");
    if (!e || !*e) e = getenv("MESH_TOKEN");
    char tmp[512];
    if (e && *e) {
        size_t n = strlen(e);
        if (n >= sizeof tmp || has_high_bit(e)) return -1;
        memcpy(tmp, e, n + 1);
        char *t = strip_ascii(tmp);
        if (*t) {
            if (strlen(t) + 1 > cap) return -1;
            strcpy(tok, t);
            goto validate;
        }
    }
    {
        char path[1100];
        if (path_is_link_or_missing_dir(mdir, 1)) return -1;
        snprintf(path, sizeof path, "%s%cauth_token", mdir, PATH_SEP);
        if (path_is_link_or_missing_dir(path, 0)) return -1;
#ifndef _WIN32
        struct stat st;
        if (lstat(path, &st) != 0 || (st.st_mode & 077) != 0) return -1; /* Python repairs perms */
#endif
        FILE *f = fopen(path, "rb");
        if (!f) return -1;
        size_t got = fread(tmp, 1, sizeof tmp - 1, f);
        int more = fgetc(f);
        fclose(f);
        if (more != EOF) return -1;
        tmp[got] = '\0';
        if (has_high_bit(tmp)) return -1;
        char *t = strip_ascii(tmp);
        if (strlen(t) + 1 > cap) return -1;
        strcpy(tok, t);
    }
validate:
    if (strlen(tok) < 16) return -1;
    for (const char *p = tok; *p; p++)
        if ((unsigned char)*p <= 0x20 || (unsigned char)*p >= 0x7F) return -1;
    return 0;
}

/* ------------------------------------------------------------------------------------------ */
/* argument validation (same rules as audit.validate_namespace / the argparse definition)      */
/* ------------------------------------------------------------------------------------------ */
static int valid_namespace(const char *ns) {
    size_t n = strlen(ns);
    if (n < 1 || n > 64) return 0;
    if (!isalnum((unsigned char)ns[0]) || (unsigned char)ns[0] >= 0x80) return 0;
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)ns[i];
        if (!(isalnum(c) || c == '.' || c == '_' || c == '-') || c >= 0x80) return 0;
    }
    return strstr(ns, "..") == NULL;
}

static const char *const MEMORY_TYPES[] = {"fact",    "preference", "context",  "result",     "instruction",
                                           "process", "decision",   "error",    "general",    "secret_ref"};

static int valid_memory_type(const char *t) {
    for (size_t i = 0; i < sizeof MEMORY_TYPES / sizeof MEMORY_TYPES[0]; i++)
        if (!strcmp(t, MEMORY_TYPES[i])) return 1;
    return 0;
}

/* ^(0|[1-9][0-9]*)(\.[0-9]+)?$ and short: a valid JSON number whose value Python's float() agrees on */
static int canonical_decimal(const char *s) {
    size_t n = strlen(s);
    if (n == 0 || n > 20) return 0;
    size_t i = 0;
    if (s[0] == '0') i = 1;
    else if (s[0] >= '1' && s[0] <= '9') { while (i < n && isdigit((unsigned char)s[i])) i++; }
    else return 0;
    if (i == n) return 1;
    if (s[i] != '.') return 0;
    i++;
    if (i == n) return 0;
    for (; i < n; i++)
        if (!isdigit((unsigned char)s[i])) return 0;
    return 1;
}

static int small_uint(const char *s, long *out) {
    size_t n = strlen(s);
    if (n == 0 || n > 9) return 0;
    if (n > 1 && s[0] == '0') return 0;
    long v = 0;
    for (size_t i = 0; i < n; i++) {
        if (!isdigit((unsigned char)s[i])) return 0;
        v = v * 10 + (s[i] - '0');
    }
    *out = v;
    return 1;
}

typedef struct {
    const char *query, *ns, *type, *tags, *minconf, *minsim;
    long count;
    int json, no_stale, no_other, no_private, incl_q;
} AskArgs;

static int opt_value(int argc, char **argv, int *i, const char **val) {
    if (*i + 1 >= argc) return 0;
    const char *v = argv[*i + 1];
    if (v[0] == '-') return 0; /* argparse would treat it as an option or a negative number */
    *val = v;
    (*i)++;
    return 1;
}

static int parse_ask(int argc, char **argv, AskArgs *a) {
    memset(a, 0, sizeof *a);
    a->count = 5;
    for (int i = 2; i < argc; i++) {
        const char *t = argv[i];
        if (!strcmp(t, "--json")) a->json = 1;
        else if (!strcmp(t, "--no-stale")) a->no_stale = 1;
        else if (!strcmp(t, "--no-other")) a->no_other = 1;
        else if (!strcmp(t, "--no-private")) a->no_private = 1;
        else if (!strcmp(t, "--include-quarantined")) a->incl_q = 1;
        else if (!strcmp(t, "--count") || !strcmp(t, "-n") || !strcmp(t, "--n")) {
            const char *v;
            if (!opt_value(argc, argv, &i, &v) || !small_uint(v, &a->count)) return 0;
        } else if (!strcmp(t, "--namespace")) {
            if (!opt_value(argc, argv, &i, &a->ns)) return 0;
        } else if (!strcmp(t, "--type")) {
            if (!opt_value(argc, argv, &i, &a->type) || !valid_memory_type(a->type)) return 0;
        } else if (!strcmp(t, "--tags")) {
            if (!opt_value(argc, argv, &i, &a->tags)) return 0;
        } else if (!strcmp(t, "--min-confidence")) {
            if (!opt_value(argc, argv, &i, &a->minconf) || !canonical_decimal(a->minconf)) return 0;
        } else if (!strcmp(t, "--min-similarity")) {
            if (!opt_value(argc, argv, &i, &a->minsim) || !canonical_decimal(a->minsim)) return 0;
        } else if (t[0] == '-') {
            return 0; /* unknown / abbreviated / --opt=value / -h : Python decides */
        } else {
            if (a->query) return 0;
            a->query = t;
        }
    }
    if (!a->json || !a->query || !*a->query) return 0;
    size_t ql = strlen(a->query);
    if (ql > 65536 || !utf8_valid(a->query, ql)) return 0;
    return 1;
}

static int locale_allows_utf8_output(void) {
#ifdef _WIN32
    return 0;
#else
    if (getenv("PYTHONIOENCODING")) return 0;
    const char *v = getenv("LC_ALL");
    if (!v || !*v) v = getenv("LC_CTYPE");
    if (!v || !*v) v = getenv("LANG");
    if (!v || !*v) return 1; /* Python coerces the C locale to UTF-8 (PEP 538/540) */
    if (!strcmp(v, "C") || !strcmp(v, "POSIX")) return 1;
    for (const char *p = v; *p; p++) {
        if (tolower((unsigned char)p[0]) == 'u' && tolower((unsigned char)p[1]) == 't' &&
            tolower((unsigned char)p[2]) == 'f') {
            const char *q = p + 3;
            if (*q == '-') q++;
            if (*q == '8') return 1;
        }
    }
    return 0;
#endif
}

/* ------------------------------------------------------------------------------------------ */
/* native `ask --json`                                                                        */
/* ------------------------------------------------------------------------------------------ */
static int build_tags_json(const char *tags, Buf *out) {
    if (!tags || !*tags) { buf_puts(out, "null"); return 1; }
    if (has_high_bit(tags)) return 0;
    char *copy = (char *)malloc(strlen(tags) + 1);
    if (!copy) return 0;
    strcpy(copy, tags);
    Buf items;
    buf_init(&items);
    int count = 0, ok = 1;
    char *p = copy;
    for (;;) {
        char *comma = strchr(p, ',');
        if (comma) *comma = '\0';
        char *t = strip_ascii(p);
        for (const char *c = t; *c; c++)
            if ((unsigned char)*c < 0x20 || *c == 0x7f) ok = 0;
        if (*t) {
            if (count++) buf_putc(&items, ',');
            json_escape_into(&items, t);
        }
        if (!comma) break;
        p = comma + 1;
    }
    free(copy);
    if (!ok) { buf_free(&items); return 0; }
    if (count == 0) buf_puts(out, "null");
    else {
        buf_putc(out, '[');
        buf_put(out, items.d, items.n);
        buf_putc(out, ']');
    }
    int oom = items.oom;
    buf_free(&items);
    return !oom;
}

static int try_native_ask(int argc, char **argv) {
    AskArgs a;
    if (!parse_ask(argc, argv, &a)) { trace("arguments not natively supported"); return -1; }
    if (env_truthy("MESH_NO_DAEMON_PROXY")) { trace("MESH_NO_DAEMON_PROXY"); return -1; }
    const char *locked = getenv("MESH_LOCKED_NAMESPACE");
    if (locked && *locked) { trace("MESH_LOCKED_NAMESPACE"); return -1; }
    const char *host = getenv("MESH_HTTP_HOST");
    if (host && strcmp(host, "127.0.0.1") != 0) { trace("non-loopback MESH_HTTP_HOST"); return -1; }

    /* namespace */
    char nsbuf[256];
    const char *nsraw = (a.ns && *a.ns) ? a.ns : getenv("MESH_NAMESPACE");
    if (!nsraw) nsraw = "shared";
    if (strlen(nsraw) >= sizeof nsbuf || has_high_bit(nsraw)) { trace("namespace"); return -1; }
    strcpy(nsbuf, nsraw);
    char *ns = strip_ascii(nsbuf);
    if (!*ns) ns = (char *)"shared";
    if (!valid_namespace(ns)) { trace("invalid namespace"); return -1; }

    /* port + pid-file gate (cli._daemon_request) */
    int port = 7701;
    const char *pe = getenv("MESH_HTTP_PORT");
    char mdir[1024];
    if (mesh_dir(mdir, sizeof mdir) != 0) { trace("mesh dir"); return -1; }
    if (pe) {
        long pv;
        if (!small_uint(pe, &pv) || pv < 1 || pv > 65535) { trace("MESH_HTTP_PORT"); return -1; }
        port = (int)pv;
    } else {
        char pid[1100];
        snprintf(pid, sizeof pid, "%s%cdaemon.pid", mdir, PATH_SEP);
        if (!file_exists_follow(pid)) { trace("no daemon.pid"); return -1; }
    }

    char token[512];
    if (load_token(mdir, token, sizeof token) != 0) { trace("token unavailable/unsafe"); return -1; }

    net_init();

    /* 1) identity challenge BEFORE the token is ever sent */
    uint8_t nonce_raw[32];
    if (random_bytes(nonce_raw, sizeof nonce_raw) != 0) { trace("rng"); return -1; }
    char nonce[65], expected[65];
    hex_lower(nonce_raw, 32, nonce);
    uint8_t mac[32];
    hmac_sha256((const uint8_t *)token, strlen(token), nonce_raw, 32, mac);
    hex_lower(mac, 32, expected);

    char req[2048];
    int rl = snprintf(req, sizeof req,
                      "GET /identity?nonce=%s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAccept: application/json\r\n"
                      "X-Mesh-Nonce: %s\r\nConnection: close\r\n\r\n",
                      nonce, port, nonce);
    Buf body;
    int st = http_exchange(port, req, (size_t)rl, 500, &body);
    if (st != 200) { if (st > 0) buf_free(&body); trace("daemon unreachable or identity HTTP error"); return -1; }
    Span proof;
    int ident_ok = 0;
    if (scan_value(body.d, body.d + body.n, 0) && obj_get(body.d, body.d + body.n, "proof", &proof) &&
        (size_t)(proof.end - proof.p) == 66 && proof.p[0] == '"' && ct_equal(proof.p + 1, expected, 64))
        ident_ok = 1;
    buf_free(&body);
    if (!ident_ok) { trace("identity proof mismatch"); return -1; }

    /* 2) the real request */
    Buf pl;
    buf_init(&pl);
    buf_puts(&pl, "{\"query\":");
    json_escape_into(&pl, a.query);
    char num[64];
    snprintf(num, sizeof num, ",\"n\":%ld,\"min_similarity\":", a.count);
    buf_puts(&pl, num);
    buf_puts(&pl, a.minsim ? a.minsim : "0.0");
    buf_puts(&pl, ",\"min_confidence\":");
    buf_puts(&pl, a.minconf ? a.minconf : "0.0");
    buf_puts(&pl, a.no_stale ? ",\"include_stale\":false" : ",\"include_stale\":true");
    buf_puts(&pl, a.no_private ? ",\"include_private\":false" : ",\"include_private\":true");
    buf_puts(&pl, a.incl_q ? ",\"include_quarantined\":true" : ",\"include_quarantined\":false");
    buf_puts(&pl, a.no_other ? ",\"include_other_possible\":false" : ",\"include_other_possible\":true");
    buf_puts(&pl, ",\"memory_type\":");
    if (a.type) json_escape_into(&pl, a.type); else buf_puts(&pl, "null");
    buf_puts(&pl, ",\"tags\":");
    if (!build_tags_json(a.tags, &pl)) { buf_free(&pl); trace("tags"); return -1; }
    buf_puts(&pl, ",\"namespace\":");
    json_escape_into(&pl, ns);
    buf_puts(&pl, ",\"agent_id\":\"human-cli\"}");
    if (pl.oom) { buf_free(&pl); return -1; }

    Buf rq;
    buf_init(&rq);
    char head[1800];
    int hl = snprintf(head, sizeof head,
                      "POST /recall HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\nX-Mesh-Token: %s\r\n"
                      "Connection: close\r\nContent-Type: application/json\r\nAccept: */*\r\nContent-Length: %zu\r\n\r\n",
                      port, token, token, pl.n);
    buf_put(&rq, head, (size_t)hl);
    buf_put(&rq, pl.d, pl.n);
    buf_free(&pl);
    if (rq.oom) { buf_free(&rq); return -1; }
    memset(head, 0, sizeof head);
    st = http_exchange(port, rq.d, rq.n, 4000, &body);
    memset(rq.d, 0, rq.n);
    buf_free(&rq);
    memset(token, 0, sizeof token);
    if (st != 200) { if (st > 0) buf_free(&body); trace("recall not HTTP 200"); return -1; }

    const char *b = body.d, *be = body.d + body.n;
    const char *vend = scan_value(b, be, 0);
    if (!vend || skip_ws(vend, be) != be || *skip_ws(b, be) != '{') { buf_free(&body); trace("reply not strict JSON object"); return -1; }
    Span v;
    if (!obj_get(b, be, "status", &v) || (size_t)(v.end - v.p) != 4 || memcmp(v.p, "\"ok\"", 4) != 0) {
        buf_free(&body); trace("status != ok"); return -1;
    }
    if (obj_get(b, be, "namespace_exists", &v) && (size_t)(v.end - v.p) == 5 && !memcmp(v.p, "false", 5)) {
        buf_free(&body); trace("namespace missing (Python prints suggestions)"); return -1;
    }
    Span results, other, hint;
    int have_results = obj_get(b, be, "results", &results);
    long count = 0;
    if (have_results) {
        const char *rp = skip_ws(results.p, results.end);
        if (*rp != '[') { buf_free(&body); trace("results not a list"); return -1; }
        rp = skip_ws(rp + 1, results.end);
        if (*rp != ']') {
            for (;;) {
                const char *ve = scan_value(rp, results.end, 0);
                count++;
                rp = skip_ws(ve, results.end);
                if (*rp == ',') { rp++; continue; }
                break;
            }
        }
    }
    int other_empty = 1;
    int have_other = obj_get(b, be, "other_possible_memories", &other);
    if (have_other) {
        const char *op = skip_ws(other.p, other.end);
        if (*op == '[' || *op == '{') {
            const char *q = skip_ws(op + 1, other.end);
            other_empty = (*q == ']' || *q == '}');
        } else if (!strncmp(op, "null", 4)) {
            other_empty = 1;
        } else {
            buf_free(&body); trace("unexpected other_possible_memories type"); return -1;
        }
    }
    int have_hint = obj_get(b, be, "filter_hint", &hint);

    Buf out;
    buf_init(&out);
    buf_puts(&out, "{\n  \"namespace\": ");
    json_escape_into(&out, ns);
    buf_puts(&out, ",\n  \"query\": ");
    json_escape_into(&out, a.query);
    snprintf(num, sizeof num, ",\n  \"count\": %ld,\n  \"results\": ", count);
    buf_puts(&out, num);
    if (have_results) emit_pretty(&out, results.p, results.end, 1);
    else buf_puts(&out, "[]");
    buf_puts(&out, ",\n  \"other_possible_memories\": ");
    if (a.no_other || other_empty) buf_puts(&out, "[]");
    else emit_pretty(&out, other.p, other.end, 1);
    buf_puts(&out, ",\n  \"filter_hint\": ");
    if (have_hint) emit_pretty(&out, hint.p, hint.end, 1);
    else buf_puts(&out, "null");
    buf_puts(&out, "\n}\n");
    buf_free(&body);
    if (out.oom) { buf_free(&out); return -1; }

    if (has_high_bit(out.d) && !locale_allows_utf8_output()) {
        buf_free(&out);
        trace("non-ASCII output needs Python's console encoding");
        return -1;
    }
    size_t wrote = fwrite(out.d, 1, out.n, stdout);
    int fl = fflush(stdout);
    buf_free(&out);
    (void)wrote;
    (void)fl;
    return 0; /* output has been written: never fall back after printing */
}

/* ------------------------------------------------------------------------------------------ */
/* fallback: `python -m mesh.cli <args>`                                                      */
/* ------------------------------------------------------------------------------------------ */
#ifdef _WIN32
static int run_python_win(wchar_t **wargv, int wargc) {
    /* build a wide command line: <exe> -m mesh.cli args... with CommandLineToArgvW quoting */
    size_t cap = 1024;
    for (int i = 1; i < wargc; i++) cap += wcslen(wargv[i]) * 2 + 4;
    wchar_t *cl = (wchar_t *)malloc(cap * sizeof(wchar_t));
    if (!cl) return 127;
    const wchar_t *exes[3] = {NULL, L"python", L"py"};
    wchar_t envpy[512];
    DWORD el = GetEnvironmentVariableW(L"MESH_PYTHON", envpy, 512);
    if (el > 0 && el < 512) exes[0] = envpy;
    int rc = 127;
    for (int e = 0; e < 3; e++) {
        if (!exes[e]) continue;
        wchar_t *w = cl;
        *w++ = L'"';
        for (const wchar_t *p = exes[e]; *p; p++) *w++ = *p;
        *w++ = L'"';
        const wchar_t *fixed = L" -m mesh.cli";
        for (const wchar_t *p = fixed; *p; p++) *w++ = *p;
        for (int i = 1; i < wargc; i++) {
            *w++ = L' ';
            const wchar_t *a = wargv[i];
            int needs = (*a == 0);
            for (const wchar_t *p = a; *p; p++)
                if (*p == L' ' || *p == L'\t' || *p == L'\n' || *p == L'\v' || *p == L'"') needs = 1;
            if (!needs) {
                for (const wchar_t *p = a; *p; p++) *w++ = *p;
                continue;
            }
            *w++ = L'"';
            for (const wchar_t *p = a;; p++) {
                unsigned bs = 0;
                while (*p == L'\\') { bs++; p++; }
                if (*p == 0) {
                    for (unsigned k = 0; k < bs * 2; k++) *w++ = L'\\';
                    break;
                }
                if (*p == L'"') {
                    for (unsigned k = 0; k < bs * 2 + 1; k++) *w++ = L'\\';
                    *w++ = L'"';
                } else {
                    for (unsigned k = 0; k < bs; k++) *w++ = L'\\';
                    *w++ = *p;
                }
            }
            *w++ = L'"';
        }
        *w = 0;
        STARTUPINFOW si;
        PROCESS_INFORMATION pi;
        ZeroMemory(&si, sizeof si);
        si.cb = sizeof si;
        ZeroMemory(&pi, sizeof pi);
        if (CreateProcessW(NULL, cl, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) {
            WaitForSingleObject(pi.hProcess, INFINITE);
            DWORD code = 1;
            GetExitCodeProcess(pi.hProcess, &code);
            CloseHandle(pi.hProcess);
            CloseHandle(pi.hThread);
            rc = (int)code;
            break;
        }
    }
    if (rc == 127) fprintf(stderr, "mesh_fast: could not start Python (tried MESH_PYTHON, python, py).\n");
    free(cl);
    return rc;
}
#else
static int run_python_posix(int argc, char **argv) {
    char **av = (char **)calloc((size_t)argc + 4, sizeof(char *));
    if (!av) return 127;
    av[1] = (char *)"-m";
    av[2] = (char *)"mesh.cli";
    for (int i = 1; i < argc; i++) av[i + 2] = argv[i];
    av[argc + 2] = NULL;
    const char *mp = getenv("MESH_PYTHON");
    if (mp && *mp) {
        av[0] = (char *)mp;
        execvp(mp, av);
    }
    av[0] = (char *)"python3";
    execvp("python3", av);
    av[0] = (char *)"python";
    execvp("python", av);
    fprintf(stderr, "mesh_fast: could not start Python (tried MESH_PYTHON, python3, python).\n");
    free(av);
    return 127;
}
#endif

static int is_version_flag(const char *s) {
    return !strcmp(s, "--version") || !strcmp(s, "-v") || !strcmp(s, "version");
}

static int is_ask_alias(const char *s) {
    return !strcmp(s, "ask") || !strcmp(s, "search") || !strcmp(s, "recall") || !strcmp(s, "find");
}

#ifdef _WIN32
int main(void) {
    int wargc = 0;
    wchar_t **wargv = CommandLineToArgvW(GetCommandLineW(), &wargc);
    if (!wargv) return 127;
    g_trace = getenv("MESH_NATIVE_TRACE") != NULL;
    char **argv = (char **)calloc((size_t)wargc + 1, sizeof(char *));
    int convertible = (argv != NULL);
    for (int i = 0; convertible && i < wargc; i++) {
        int n = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, wargv[i], -1, NULL, 0, NULL, NULL);
        if (n <= 0) { convertible = 0; break; }
        argv[i] = (char *)malloc((size_t)n);
        if (!argv[i] || WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, wargv[i], -1, argv[i], n, NULL, NULL) <= 0)
            convertible = 0;
    }
    if (convertible && wargc == 2 && is_version_flag(argv[1])) {
        puts("mesh " MESH_VERSION);
        return 0;
    }
    if (convertible && wargc >= 3 && is_ask_alias(argv[1]) && !env_truthy("MESH_NATIVE_DISABLE")) {
        if (try_native_ask(wargc, argv) == 0) return 0;
    }
    return run_python_win(wargv, wargc);
}
#else
int main(int argc, char **argv) {
    g_trace = getenv("MESH_NATIVE_TRACE") != NULL;
    if (argc == 2 && is_version_flag(argv[1])) {
        puts("mesh " MESH_VERSION);
        return 0;
    }
    if (argc >= 3 && is_ask_alias(argv[1]) && !env_truthy("MESH_NATIVE_DISABLE")) {
        if (try_native_ask(argc, argv) == 0) return 0;
    }
    return run_python_posix(argc, argv);
}
#endif
