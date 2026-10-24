/*
 * c_core/mesh_core.c - native C11 (+ runtime-dispatched AVX2/FMA) core for Mesh.
 *
 *   mesh_c_version()             -> ABI version (bridge refuses older libraries)
 *   mesh_c_cpu_has_avx2()        -> 1 if the AVX2+FMA kernel will be used
 *   mesh_c_guard_prescan()       -> 0 = guaranteed clean ASCII, 1 = run the full Python guards
 *   mesh_c_hash_embed_384()      -> bit-compatible (<=1e-6) port of memory._hash_embed_384
 *   mesh_c_cosine_batch_384()    -> n_rows x 384 dot products (unit vectors => cosine)
 *   mesh_c_compute_decay_batch() -> vectorisable TTL decay / staleness for n rows
 *
 * Build (Windows MinGW / MSYS2):
 *   gcc -O3 -std=c11 -shared -static-libgcc -o mesh/_c_core.dll c_core/mesh_core.c -lm
 * Build (Linux / macOS):
 *   gcc -O3 -std=c11 -shared -fPIC -o mesh/_c_core.so c_core/mesh_core.c -lm
 *
 * No -mavx2 flag is needed: the AVX2 kernel is compiled with a per-function target
 * attribute and selected at run time, so the library loads (and falls back to scalar
 * code) on CPUs without AVX2 instead of crashing with an illegal instruction.
 */
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#if defined(_WIN32)
#define MESH_EXPORT __declspec(dllexport)
#else
#define MESH_EXPORT __attribute__((visibility("default")))
#endif

#if (defined(__x86_64__) || defined(__i386__)) && defined(__GNUC__)
#define MESH_X86_GNU 1
#include <immintrin.h>
#endif

#define MESH_ABI_VERSION 2

MESH_EXPORT int mesh_c_version(void) { return MESH_ABI_VERSION; }

/* ======================================================================== */
/* 1. Guard prescan                                                          */
/* ======================================================================== */
/*
 * Returns 0 only when the text can be proven irrelevant to every secret / injection
 * regex in mesh/secrets.py and mesh/guards.py. Anything suspicious returns 1 and the
 * caller runs the full regex suite, so a false "1" only costs speed while a false "0"
 * would be a security hole. The trigger list is therefore deliberately broad and is
 * fuzzed against the real regexes by build_and_verify.py.
 *
 * Structural rejects: control chars (except \n, \t), any byte >= 127, '<', '=', '--',
 * '://', '_' , '*', '`', '~', '[', ']' (markup splitters / env-var style keys).
 */
static const char *const PRESCAN_TRIGGERS[] = {
    /* credential prefixes (mesh/secrets.py _KNOWN_API_KEY_RE) */
    "sk-", "ghp", "gho", "ghu", "ghs", "ghr", "github", "glpat", "akia", "asia", "aiza",
    "xox", "eyj", "pypi", "sg.", "npm", "hf_", "rk_", "demo_fake", "demo",
    /* credential keywords (keyword-assignment, yaml-block, CLI flag, passphrase regexes) */
    "password", "passwd", "passphrase", "passcode", "pwd", "pin", "secret", "token",
    "api_key", "apikey", "api key", "api-key", "key", "phrase", "mnemonic", "seed",
    "credential", "private", "code", "auth", "bearer", "basic", "hunter2", "vault",
    "locker", "login", "recovery", "aws",
    /* prompt-injection / override vocabulary (mesh/guards.py _POISON_PATTERNS) */
    "ignor", "disregard", "forget", "override", "bypass", "system", "inst", "instruct",
    "prompt", "directive", "guideline", "constraint", "guardrail", "policies", "follow",
    "obey", "adhere", "mode", "jailbreak", "unrestricted", "developer", "mesh", "context",
    "important", "critical", "urgent", "anweisung", "instrucc", "regeln", "reglas",
    "regles", "instruc", "dan",
};
#define PRESCAN_N (sizeof(PRESCAN_TRIGGERS) / sizeof(PRESCAN_TRIGGERS[0]))

MESH_EXPORT int mesh_c_guard_prescan(const char *text, size_t len) {
    if (!text || len == 0 || len > 16384) return 1;

    char low[16384];
    size_t i;
    for (i = 0; i < len; i++) {
        unsigned char c = (unsigned char)text[i];
        if ((c < 32 && c != 10 && c != 9) || c >= 127) return 1;
        switch (c) {
            case '<': case '=': case '_': case '*': case '`': case '~':
            case '[': case ']':
                return 1;
            case ':':
                if (i + 2 < len && text[i + 1] == '/' && text[i + 2] == '/') return 1;
                break;
            case '-':
                if (i + 1 < len && text[i + 1] == '-') return 1;
                break;
            default:
                break;
        }
        low[i] = (c >= 'A' && c <= 'Z') ? (char)(c + 32) : (char)c;
    }

    for (size_t t = 0; t < PRESCAN_N; t++) {
        const char *nd = PRESCAN_TRIGGERS[t];
        size_t nl = strlen(nd);
        if (nl > len) continue;
        size_t limit = len - nl;
        char first = nd[0];
        for (i = 0; i <= limit; i++) {
            if (low[i] == first && memcmp(low + i, nd, nl) == 0) return 1;
        }
    }
    return 0;
}

/* ======================================================================== */
/* 2. Cosine / dot batch (AVX2+FMA with runtime dispatch)                    */
/* ======================================================================== */
static void cosine_scalar(const float *matrix, const float *query, size_t n_rows, float *out) {
    for (size_t i = 0; i < n_rows; i++) {
        const float *row = matrix + i * 384;
        float a0 = 0.f, a1 = 0.f, a2 = 0.f, a3 = 0.f;
        for (size_t j = 0; j < 384; j += 4) {
            a0 += row[j] * query[j];
            a1 += row[j + 1] * query[j + 1];
            a2 += row[j + 2] * query[j + 2];
            a3 += row[j + 3] * query[j + 3];
        }
        out[i] = (a0 + a1) + (a2 + a3);
    }
}

#ifdef MESH_X86_GNU
__attribute__((target("avx2,fma")))
static void cosine_avx2(const float *matrix, const float *query, size_t n_rows, float *out) {
    for (size_t i = 0; i < n_rows; i++) {
        const float *row = matrix + i * 384;
        __m256 s0 = _mm256_setzero_ps(), s1 = _mm256_setzero_ps();
        __m256 s2 = _mm256_setzero_ps(), s3 = _mm256_setzero_ps();
        for (size_t k = 0; k < 384; k += 32) { /* 48 vectors of 8 floats = 384 dims */
            s0 = _mm256_fmadd_ps(_mm256_loadu_ps(row + k),      _mm256_loadu_ps(query + k),      s0);
            s1 = _mm256_fmadd_ps(_mm256_loadu_ps(row + k + 8),  _mm256_loadu_ps(query + k + 8),  s1);
            s2 = _mm256_fmadd_ps(_mm256_loadu_ps(row + k + 16), _mm256_loadu_ps(query + k + 16), s2);
            s3 = _mm256_fmadd_ps(_mm256_loadu_ps(row + k + 24), _mm256_loadu_ps(query + k + 24), s3);
        }
        __m256 s = _mm256_add_ps(_mm256_add_ps(s0, s1), _mm256_add_ps(s2, s3));
        __m128 lo = _mm256_castps256_ps128(s);
        __m128 hi = _mm256_extractf128_ps(s, 1);
        __m128 s4 = _mm_add_ps(lo, hi);
        __m128 sh = _mm_movehdup_ps(s4);
        __m128 s2v = _mm_add_ps(s4, sh);
        sh = _mm_movehl_ps(sh, s2v);
        out[i] = _mm_cvtss_f32(_mm_add_ss(s2v, sh));
    }
}
#endif

static int g_have_avx2 = -1;

static int detect_avx2(void) {
    if (g_have_avx2 < 0) {
#ifdef MESH_X86_GNU
        __builtin_cpu_init();
        g_have_avx2 = (__builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma")) ? 1 : 0;
#else
        g_have_avx2 = 0;
#endif
    }
    return g_have_avx2;
}

MESH_EXPORT int mesh_c_cpu_has_avx2(void) { return detect_avx2(); }

MESH_EXPORT void mesh_c_cosine_batch_384(const float *matrix, const float *query,
                                         size_t n_rows, float *out_sims) {
    if (!matrix || !query || !out_sims || n_rows == 0) return;
#ifdef MESH_X86_GNU
    if (detect_avx2()) { cosine_avx2(matrix, query, n_rows, out_sims); return; }
#endif
    cosine_scalar(matrix, query, n_rows, out_sims);
}

/* ======================================================================== */
/* 3. TTL decay batch                                                        */
/* ======================================================================== */
/*
 * Mirrors store._compute_row_effective_confidence:
 *   ttl NaN / <= 0, or decay start NaN  -> eff = round4(conf), stale = conf < threshold
 *   else factor = clamp(1 - max(0, now - start) / 86400 / ttl, 0, 1);
 *        eff = round4(conf * factor), stale = eff < threshold
 */
static double round4(double x) { return nearbyint(x * 10000.0) / 10000.0; }

MESH_EXPORT void mesh_c_compute_decay_batch(
    const float *confidences, const float *ttl_days, const double *decay_starts,
    size_t n_rows, double now_epoch, float stale_threshold,
    float *out_eff_conf, uint8_t *out_is_stale) {
    if (!confidences || !ttl_days || !decay_starts || !out_eff_conf || !out_is_stale) return;
    for (size_t i = 0; i < n_rows; i++) {
        double conf = (double)confidences[i];
        double ttl = (double)ttl_days[i];
        double start = decay_starts[i];
        if (!(ttl > 0.0) || start != start) {
            out_eff_conf[i] = (float)round4(conf);
            out_is_stale[i] = (uint8_t)((float)conf < stale_threshold);
            continue;
        }
        double elapsed = (now_epoch - start) / 86400.0;
        if (elapsed < 0.0) elapsed = 0.0;
        double f = 1.0 - elapsed / ttl;
        if (f < 0.0) f = 0.0;
        if (f > 1.0) f = 1.0;
        double eff = round4(conf * f);
        out_eff_conf[i] = (float)eff;
        out_is_stale[i] = (uint8_t)((float)eff < stale_threshold);
    }
}

/* ======================================================================== */
/* 4. Hash embedding (port of memory._hash_embed_384)                        */
/* ======================================================================== */
/* BLAKE2b with 8-byte digest, unkeyed - identical to hashlib.blake2b(digest_size=8). */
static const uint64_t B2_IV[8] = {
    0x6a09e667f3bcc908ULL, 0xbb67ae8584caa73bULL, 0x3c6ef372fe94f82bULL, 0xa54ff53a5f1d36f1ULL,
    0x510e527fade682d1ULL, 0x9b05688c2b3e6c1fULL, 0x1f83d9abfb41bd6bULL, 0x5be0cd19137e2179ULL};

static const uint8_t B2_SIGMA[12][16] = {
    {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15},
    {14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3},
    {11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4},
    {7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8},
    {9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13},
    {2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9},
    {12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11},
    {13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10},
    {6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5},
    {10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0},
    {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15},
    {14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3}};

#define B2_ROTR(x, n) (((x) >> (n)) | ((x) << (64 - (n))))
#define B2_G(a, b, c, d, x, y)                         \
    do {                                               \
        v[a] = v[a] + v[b] + (x); v[d] = B2_ROTR(v[d] ^ v[a], 32); \
        v[c] = v[c] + v[d];       v[b] = B2_ROTR(v[b] ^ v[c], 24); \
        v[a] = v[a] + v[b] + (y); v[d] = B2_ROTR(v[d] ^ v[a], 16); \
        v[c] = v[c] + v[d];       v[b] = B2_ROTR(v[b] ^ v[c], 63); \
    } while (0)

static void b2_compress(uint64_t h[8], const uint8_t blk[128], uint64_t t, int last) {
    uint64_t m[16], v[16];
    for (int i = 0; i < 16; i++) {
        uint64_t w = 0;
        for (int b = 7; b >= 0; b--) w = (w << 8) | blk[i * 8 + b];
        m[i] = w;
    }
    for (int i = 0; i < 8; i++) { v[i] = h[i]; v[i + 8] = B2_IV[i]; }
    v[12] ^= t;
    if (last) v[14] = ~v[14];
    for (int r = 0; r < 12; r++) {
        const uint8_t *s = B2_SIGMA[r];
        B2_G(0, 4, 8, 12, m[s[0]], m[s[1]]);
        B2_G(1, 5, 9, 13, m[s[2]], m[s[3]]);
        B2_G(2, 6, 10, 14, m[s[4]], m[s[5]]);
        B2_G(3, 7, 11, 15, m[s[6]], m[s[7]]);
        B2_G(0, 5, 10, 15, m[s[8]], m[s[9]]);
        B2_G(1, 6, 11, 12, m[s[10]], m[s[11]]);
        B2_G(2, 7, 8, 13, m[s[12]], m[s[13]]);
        B2_G(3, 4, 9, 14, m[s[14]], m[s[15]]);
    }
    for (int i = 0; i < 8; i++) h[i] ^= v[i] ^ v[i + 8];
}

/* First 8 digest bytes as a little-endian integer == Python int.from_bytes(d, "little"). */
static uint64_t b2_hash64(const uint8_t *in, size_t n) {
    uint64_t h[8];
    for (int i = 0; i < 8; i++) h[i] = B2_IV[i];
    h[0] ^= 0x01010000ULL ^ 8ULL; /* fanout=1, depth=1, digest_length=8, no key */
    uint8_t blk[128];
    uint64_t t = 0;
    while (n > 128) {
        t += 128;
        b2_compress(h, in, t, 0);
        in += 128;
        n -= 128;
    }
    memset(blk, 0, sizeof(blk));
    if (n) memcpy(blk, in, n);
    t += n;
    b2_compress(h, blk, t, 1);
    return h[0];
}

/* ---- GENERATED from mesh/memory.py (_STOPWORDS, _NEGATION_TOKENS, _CONCEPT_CLUSTERS). ---- */
typedef struct { const char *w; uint32_t flags; } he_word_t;
static const he_word_t HE_WORDS[] = {
    {"a", 1},
    {"about", 1},
    {"after", 1},
    {"against", 1},
    {"agent", 16384},
    {"agents", 16384},
    {"all", 1},
    {"among", 1},
    {"an", 1},
    {"and", 1},
    {"anime", 16},
    {"any", 1},
    {"api", 1024},
    {"apikey", 64},
    {"appearance", 4},
    {"are", 1},
    {"aren't", 2},
    {"arent", 2},
    {"around", 1},
    {"as", 1},
    {"at", 1},
    {"auth", 128},
    {"authentication", 128},
    {"bag", 32},
    {"be", 1},
    {"been", 1},
    {"before", 1},
    {"being", 1},
    {"between", 1},
    {"black", 4},
    {"blue", 4},
    {"book", 16},
    {"brown", 4},
    {"bug", 512},
    {"bugs", 512},
    {"but", 1},
    {"by", 1},
    {"can", 1},
    {"can't", 2},
    {"cannot", 2},
    {"cant", 2},
    {"car", 32},
    {"cat", 4096},
    {"cats", 4096},
    {"chair", 32},
    {"code", 2048},
    {"coding", 2048},
    {"color", 4},
    {"colour", 4},
    {"constraint", 1024},
    {"constraints", 1024},
    {"cookie", 128},
    {"cookies", 128},
    {"coordinate", 16384},
    {"could", 1},
    {"credential", 64},
    {"credentials", 64},
    {"cyan", 4},
    {"dark", 4},
    {"database", 256},
    {"db", 256},
    {"deploy", 8192},
    {"desk", 32},
    {"dev", 8192},
    {"did", 1},
    {"didn't", 2},
    {"didnt", 2},
    {"disabled", 2},
    {"disallow", 2},
    {"do", 1},
    {"does", 1},
    {"doesn't", 2},
    {"doesnt", 2},
    {"dog", 4096},
    {"dogs", 4096},
    {"don't", 2},
    {"dont", 2},
    {"during", 1},
    {"editor", 4},
    {"editors", 4},
    {"else", 1},
    {"environment", 8192},
    {"error", 512},
    {"errors", 512},
    {"failed", 512},
    {"fails", 512},
    {"false", 2},
    {"favorite", 8},
    {"favourite", 8},
    {"file", 2048},
    {"files", 2048},
    {"for", 1},
    {"forbidden", 2},
    {"from", 1},
    {"game", 16},
    {"go", 2048},
    {"gold", 4},
    {"gray", 4},
    {"green", 4},
    {"grey", 4},
    {"had", 1},
    {"hadn't", 2},
    {"hadnt", 2},
    {"has", 1},
    {"hasn't", 2},
    {"hasnt", 2},
    {"have", 1},
    {"haven't", 2},
    {"havent", 2},
    {"having", 1},
    {"host", 8192},
    {"how", 1},
    {"httponly", 128},
    {"i", 1},
    {"if", 1},
    {"in", 1},
    {"incident", 512},
    {"index", 256},
    {"indexing", 256},
    {"into", 1},
    {"ip", 1024},
    {"is", 1},
    {"isn't", 2},
    {"isnt", 2},
    {"issue", 512},
    {"issues", 512},
    {"it", 1},
    {"its", 1},
    {"java", 2048},
    {"javascript", 2048},
    {"jwt", 128},
    {"key", 64},
    {"keyboard", 32},
    {"language", 2048},
    {"languages", 2048},
    {"laptop", 32},
    {"learn", 16384},
    {"light", 4},
    {"like", 9},
    {"likes", 8},
    {"limit", 1024},
    {"limits", 1024},
    {"localstorage", 128},
    {"login", 128},
    {"love", 8},
    {"loves", 8},
    {"magenta", 4},
    {"management", 128},
    {"manga", 16},
    {"may", 1},
    {"me", 1},
    {"memories", 16384},
    {"memory", 16384},
    {"mesh", 16384},
    {"might", 1},
    {"migration", 256},
    {"minute", 1024},
    {"mode", 4},
    {"movie", 16},
    {"movies", 16},
    {"music", 16},
    {"must", 1},
    {"my", 1},
    {"neither", 2},
    {"never", 2},
    {"no", 2},
    {"none", 2},
    {"nor", 2},
    {"not", 2},
    {"notebook", 32},
    {"of", 1},
    {"off", 2},
    {"on", 1},
    {"or", 1},
    {"orange", 4},
    {"our", 1},
    {"out", 1},
    {"over", 1},
    {"password", 64},
    {"pen", 32},
    {"pencil", 32},
    {"pet", 4096},
    {"pets", 4096},
    {"pgbouncer", 256},
    {"phone", 32},
    {"pink", 4},
    {"pooling", 256},
    {"port", 8192},
    {"postgres", 256},
    {"postgresql", 256},
    {"prefer", 8},
    {"preference", 8},
    {"preferences", 8},
    {"prefers", 8},
    {"private", 64},
    {"problem", 512},
    {"problems", 512},
    {"prod", 8192},
    {"production", 8192},
    {"program", 2048},
    {"programming", 2048},
    {"purple", 4},
    {"python", 2048},
    {"quorum", 512},
    {"quota", 1024},
    {"rate", 1024},
    {"recall", 16384},
    {"red", 4},
    {"redis", 256},
    {"requests", 1024},
    {"rust", 2048},
    {"schema", 256},
    {"script", 8192},
    {"secret", 64},
    {"secrets", 64},
    {"series", 16},
    {"server", 8192},
    {"session", 128},
    {"shall", 1},
    {"shared", 16384},
    {"should", 1},
    {"shouldn't", 2},
    {"shouldnt", 2},
    {"show", 16},
    {"silver", 4},
    {"some", 1},
    {"song", 16},
    {"sql", 256},
    {"sqlite", 256},
    {"staging", 8192},
    {"strongly", 8},
    {"such", 1},
    {"that", 1},
    {"the", 1},
    {"their", 1},
    {"them", 1},
    {"theme", 4},
    {"then", 1},
    {"there", 1},
    {"these", 1},
    {"they", 1},
    {"this", 1},
    {"those", 1},
    {"through", 1},
    {"timeout", 512},
    {"to", 1},
    {"token", 128},
    {"tokens", 128},
    {"typescript", 2048},
    {"ui", 4},
    {"under", 1},
    {"url", 8192},
    {"vault", 64},
    {"very", 1},
    {"visual", 4},
    {"want", 8},
    {"was", 1},
    {"wasn't", 2},
    {"wasnt", 2},
    {"watch", 32},
    {"we", 1},
    {"were", 1},
    {"weren't", 2},
    {"werent", 2},
    {"what", 1},
    {"when", 1},
    {"where", 1},
    {"which", 1},
    {"white", 4},
    {"who", 1},
    {"whom", 1},
    {"why", 1},
    {"will", 1},
    {"with", 1},
    {"without", 2},
    {"won't", 2},
    {"wont", 2},
    {"would", 1},
    {"wouldn't", 2},
    {"wouldnt", 2},
    {"yellow", 4},
    {"you", 1},
    {"your", 1},
};
#define HE_WORDS_N (sizeof(HE_WORDS) / sizeof(HE_WORDS[0]))
static const char *const HE_CLUSTER_IDS[13] = {
    "@c_theme",
    "@c_pref",
    "@c_media",
    "@c_item",
    "@c_cred",
    "@c_auth",
    "@c_db",
    "@c_err",
    "@c_limit",
    "@c_lang",
    "@c_pet",
    "@c_env",
    "@c_mem",
};

static uint32_t he_flags(const char *w, size_t n) {
    char buf[40];
    if (n == 0 || n >= sizeof(buf)) return 0;
    memcpy(buf, w, n);
    buf[n] = 0;
    size_t lo = 0, hi = HE_WORDS_N;
    while (lo < hi) {
        size_t mid = (lo + hi) / 2;
        int c = strcmp(buf, HE_WORDS[mid].w);
        if (c == 0) return HE_WORDS[mid].flags;
        if (c < 0) hi = mid; else lo = mid + 1;
    }
    return 0;
}

#define HE_MAX_LEN 8192
#define HE_MAX_WORDS 512
#define HE_MAX_TOKLEN 128
#define HE_NCLUSTERS 13
#define HE_SEEN_MAX 320

typedef struct { uint16_t off; uint16_t len; } he_span_t;

static int he_is_alnum(unsigned char c) {
    return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
}
static int he_is_wordch(unsigned char c) { /* regex \w for ASCII */
    return he_is_alnum(c) || (c >= 'A' && c <= 'Z') || c == '_';
}
static int he_is_space(unsigned char c) { /* str.isspace() for ASCII */
    return c == ' ' || (c >= 9 && c <= 13) || (c >= 28 && c <= 31);
}

static void he_acc(float *vec, const char *feat, size_t n, double weight) {
    uint64_t h = b2_hash64((const uint8_t *)feat, n);
    unsigned i1 = (unsigned)(h % 368u);
    unsigned i2 = (unsigned)((h >> 16) % 368u);
    unsigned i3 = (unsigned)((h >> 32) % 368u);
    double s1 = (((h >> 9) & 1u) == 0) ? 1.0 : -1.0;
    double s2 = (((h >> 25) & 1u) == 0) ? 1.0 : -1.0;
    double s3 = (((h >> 41) & 1u) == 0) ? 1.0 : -1.0;
    vec[i1] = vec[i1] + (float)(s1 * weight);
    vec[i2] = vec[i2] + (float)(s2 * (weight * 0.6));
    vec[i3] = vec[i3] + (float)(s3 * (weight * 0.3));
}

static size_t he_stem(const char *w, size_t n, char *out) {
    int has_apos = 0;
    for (size_t i = 0; i < n; i++) if (w[i] == '\'') { has_apos = 1; break; }
    memcpy(out, w, n);
    if (n <= 3 || has_apos) return n;
    if (n > 4 && w[n - 3] == 'i' && w[n - 2] == 'e' && w[n - 1] == 's') {
        out[n - 3] = 'y';
        return n - 2;
    }
    if (n > 5 && w[n - 3] == 'i' && w[n - 2] == 'n' && w[n - 1] == 'g') {
        size_t b = n - 3;
        if (b >= 3 && w[b - 1] == w[b - 2]) return b - 1;
        return b;
    }
    if (n > 4 && w[n - 2] == 'e' && w[n - 1] == 'd') return n - 2;
    if (n > 4 && w[n - 2] == 'e' && w[n - 1] == 's') return n - 2;
    if (w[n - 1] == 's' && !(w[n - 2] == 's') && n > 3) return n - 1;
    return n;
}

/*
 * Returns 0 on success, -1 when the input is outside the supported envelope
 * (non-ASCII, > 8 KiB, > 512 words, token > 128 bytes); the caller then uses the
 * Python implementation, so results are identical either way.
 */
MESH_EXPORT int mesh_c_hash_embed_384(const char *text, size_t len, float *out_vec) {
    if (!text || !out_vec || len > HE_MAX_LEN) return -1;
    memset(out_vec, 0, 384 * sizeof(float));

    /* strip + lower + backtick->apostrophe; reject non-ASCII */
    size_t a = 0, b = len;
    for (size_t i = 0; i < len; i++) if ((unsigned char)text[i] >= 128) return -1;
    while (a < b && he_is_space((unsigned char)text[a])) a++;
    while (b > a && he_is_space((unsigned char)text[b - 1])) b--;
    size_t n0 = b - a;
    char low[HE_MAX_LEN + 1];
    for (size_t i = 0; i < n0; i++) {
        unsigned char c = (unsigned char)text[a + i];
        if (c >= 'A' && c <= 'Z') c = (unsigned char)(c + 32);
        else if (c == '`') c = '\'';
        low[i] = (char)c;
    }
    /* re.sub(r"(?<!n)'s\b", "", s) */
    char cl[HE_MAX_LEN + 1];
    size_t cn = 0;
    for (size_t i = 0; i < n0;) {
        if (low[i] == '\'' && i + 1 < n0 && low[i + 1] == 's' && (i == 0 || low[i - 1] != 'n') &&
            (i + 2 >= n0 || !he_is_wordch((unsigned char)low[i + 2]))) {
            i += 2;
            continue;
        }
        cl[cn++] = low[i++];
    }
    cl[cn] = 0;

    /* words: [a-z0-9]+(?:'[a-z0-9]+)? */
    he_span_t raw[HE_MAX_WORDS];
    size_t nraw = 0;
    for (size_t i = 0; i < cn;) {
        if (!he_is_alnum((unsigned char)cl[i])) { i++; continue; }
        size_t j = i;
        while (j < cn && he_is_alnum((unsigned char)cl[j])) j++;
        if (j + 1 < cn && cl[j] == '\'' && he_is_alnum((unsigned char)cl[j + 1])) {
            j++;
            while (j < cn && he_is_alnum((unsigned char)cl[j])) j++;
        }
        if (j - i > HE_MAX_TOKLEN || nraw >= HE_MAX_WORDS) return -1;
        raw[nraw].off = (uint16_t)i;
        raw[nraw].len = (uint16_t)(j - i);
        nraw++;
        i = j;
    }
    /* numbers: \d+(?:\.\d+)? */
    he_span_t nums[HE_MAX_WORDS];
    size_t nnum = 0;
    for (size_t i = 0; i < cn;) {
        if (!(cl[i] >= '0' && cl[i] <= '9')) { i++; continue; }
        size_t j = i;
        while (j < cn && cl[j] >= '0' && cl[j] <= '9') j++;
        if (j + 1 < cn && cl[j] == '.' && cl[j + 1] >= '0' && cl[j + 1] <= '9') {
            j++;
            while (j < cn && cl[j] >= '0' && cl[j] <= '9') j++;
        }
        if (j - i > HE_MAX_TOKLEN || nnum >= HE_MAX_WORDS) return -1;
        nums[nnum].off = (uint16_t)i;
        nums[nnum].len = (uint16_t)(j - i);
        nnum++;
        i = j;
    }

    /* content words: not stopword, or negation */
    uint16_t content[HE_MAX_WORDS];
    size_t ncont = 0;
    for (size_t i = 0; i < nraw; i++) {
        uint32_t f = he_flags(cl + raw[i].off, raw[i].len);
        if (!(f & 1u) || (f & 2u)) content[ncont++] = (uint16_t)i;
    }
    static const char EMPTY_WORD[] = "empty";
    int use_empty = 0;
    if (ncont == 0 && nnum == 0) {
        if (nraw > 0) {
            for (size_t i = 0; i < nraw; i++) content[i] = (uint16_t)i;
            ncont = nraw;
        } else {
            use_empty = 1;
            ncont = 1;
        }
    }

    /* stems */
    char stem_buf[HE_MAX_WORDS][HE_MAX_TOKLEN]; /* stack: thread-safe (ctypes drops the GIL) */
    size_t stem_len[HE_MAX_WORDS];
    const char *word_ptr[HE_MAX_WORDS];
    size_t word_len[HE_MAX_WORDS];
    int has_non_framing = 0;
    for (size_t i = 0; i < ncont; i++) {
        const char *w;
        size_t wl;
        if (use_empty) { w = EMPTY_WORD; wl = 5; }
        else { w = cl + raw[content[i]].off; wl = raw[content[i]].len; }
        word_ptr[i] = w;
        word_len[i] = wl;
        stem_len[i] = he_stem(w, wl, stem_buf[i]);
        size_t sl = stem_len[i];
        int framing = (sl == 4 && memcmp(stem_buf[i], "user", 4) == 0) ||
                      (sl == 5 && memcmp(stem_buf[i], "agent", 5) == 0) ||
                      (sl == 6 && memcmp(stem_buf[i], "system", 6) == 0);
        if (!framing) has_non_framing = 1;
    }

    char feat[2 * HE_MAX_TOKLEN + 16];
    /* w:/raw: features */
    for (size_t i = 0; i < ncont; i++) {
        size_t sl = stem_len[i];
        int framing = (sl == 4 && memcmp(stem_buf[i], "user", 4) == 0) ||
                      (sl == 5 && memcmp(stem_buf[i], "agent", 5) == 0) ||
                      (sl == 6 && memcmp(stem_buf[i], "system", 6) == 0);
        double ww = (has_non_framing && framing) ? 0.25 : 1.35;
        memcpy(feat, "w:", 2);
        memcpy(feat + 2, stem_buf[i], sl);
        he_acc(out_vec, feat, 2 + sl, ww);
        if (!(word_len[i] == sl && memcmp(word_ptr[i], stem_buf[i], sl) == 0)) {
            memcpy(feat, "raw:", 4);
            memcpy(feat + 4, word_ptr[i], word_len[i]);
            he_acc(out_vec, feat, 4 + word_len[i], ww * 0.45);
        }
    }
    /* num: features */
    for (size_t i = 0; i < nnum; i++) {
        memcpy(feat, "num:", 4);
        memcpy(feat + 4, cl + nums[i].off, nums[i].len);
        he_acc(out_vec, feat, 4 + nums[i].len, 1.6);
    }
    /* bigrams over stemmed content words */
    for (size_t i = 0; i + 1 < ncont; i++) {
        size_t p = 0;
        memcpy(feat, "bi:", 3); p = 3;
        memcpy(feat + p, stem_buf[i], stem_len[i]); p += stem_len[i];
        feat[p++] = '_';
        memcpy(feat + p, stem_buf[i + 1], stem_len[i + 1]); p += stem_len[i + 1];
        he_acc(out_vec, feat, p, 0.65);
    }
    /* concept clusters: |word_set & cluster| over DISTINCT words (raw words + stems) */
    {
        int overlap[HE_NCLUSTERS] = {0};
        const char *seen_p[HE_SEEN_MAX];
        size_t seen_l[HE_SEEN_MAX];
        size_t nseen = 0;
        for (size_t pass = 0; pass < 2; pass++) {
            size_t cnt = (pass == 0) ? nraw : ncont;
            for (size_t i = 0; i < cnt; i++) {
                const char *w;
                size_t wl;
                if (pass == 0) { w = cl + raw[i].off; wl = raw[i].len; }
                else { w = stem_buf[i]; wl = stem_len[i]; }
                uint32_t f = he_flags(w, wl) >> 2;
                if (!f) continue;
                int dup = 0;
                for (size_t k = 0; k < nseen; k++) {
                    if (seen_l[k] == wl && memcmp(seen_p[k], w, wl) == 0) { dup = 1; break; }
                }
                if (dup) continue;
                if (nseen < HE_SEEN_MAX) { seen_p[nseen] = w; seen_l[nseen] = wl; nseen++; }
                for (int c = 0; c < HE_NCLUSTERS; c++) if (f & (1u << c)) overlap[c]++;
            }
        }
        if (use_empty) { /* "empty" is in no cluster */ }
        for (int c = 0; c < HE_NCLUSTERS; c++) {
            if (overlap[c] > 0) {
                double m = overlap[c] * 0.4;
                if (m > 1.2) m = 1.2;
                size_t cl_n = strlen(HE_CLUSTER_IDS[c]);
                he_acc(out_vec, HE_CLUSTER_IDS[c], cl_n, 1.5 + m);
            }
        }
    }
    /* char trigrams of stems with len >= 4 */
    for (size_t i = 0; i < ncont; i++) {
        size_t sl = stem_len[i];
        if (sl >= 4) {
            for (size_t j = 0; j + 2 < sl; j++) {
                memcpy(feat, "c3:", 3);
                memcpy(feat + 3, stem_buf[i] + j, 3);
                he_acc(out_vec, feat, 6, 0.22);
            }
        }
    }

    /* L2 normalise (368 active dims; 368..383 stay 0 as the hash-384 watermark) */
    float sq = 0.f;
    for (int i = 0; i < 368; i++) sq += out_vec[i] * out_vec[i];
    float norm = sqrtf(sq);
    if ((double)norm > 1e-12) {
        for (int i = 0; i < 368; i++) out_vec[i] = out_vec[i] / norm;
    }
    return 0;
}
