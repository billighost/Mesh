export const SITE_URL = "https://mesh.example";
export const AGENSI_URL = "https://www.agensi.io/skills/mesh-context-layer";
export const GITHUB_URL = "https://github.com/billighost/mesh";
export const PYPI_URL = "https://pypi.org/project/mesh-context-layer/";

export const INSTALL_CMD = "pip install mesh-context-layer";

export const nav = [
  { label: "How it works", href: "#how" },
  { label: "Search", href: "#search" },
  { label: "Memory", href: "#memory" },
  { label: "Performance", href: "#performance" },
  { label: "Interfaces", href: "#interfaces" },
  { label: "Agensi", href: "#agensi" },
];

export const agents = [
  "Claude Code", "Cursor", "Codex CLI", "Muse", "Dots",
  "OpenClaw", "Manus", "Grok", "Grok Bot",
];

export const searchSamples = [
  {
    id: "postgres-port",
    query: "what port does postgres use?",
    fact: "Production PostgreSQL 16 runs on port 5432 with pgBouncer connection pooling.",
    source: "backend-agent",
    tags: ["fact", "db / prod", "384d"],
    stages: [
      { key: "dense", score: 0.98 },
      { key: "bm25", score: 0.88 },
      { key: "idf", score: 0.86 },
      { key: "synonyms", score: 0.92 },
      { key: "exact", score: 0.97 },
    ],
    fused: 0.93,
    vectorOnly: 0.84,
    latency: "0.08 ms",
  },
  {
    id: "staging-port",
    query: "staging database port",
    fact: "Staging database port is 5439.",
    source: "devops-agent",
    tags: ["fact", "database / staging", "384d"],
    stages: [
      { key: "dense", score: 0.91 },
      { key: "bm25", score: 0.96 },
      { key: "idf", score: 0.78 },
      { key: "synonyms", score: 0.95 },
      { key: "exact", score: 0.99 },
    ],
    fused: 0.92,
    vectorOnly: 0.81,
    latency: "0.07 ms",
  },
  {
    id: "jwt-rs256",
    query: "JWT RS256",
    fact: "Auth service requires JWT RS256 signatures.",
    source: "security-agent",
    tags: ["instruction", "auth / jwt", "384d"],
    stages: [
      { key: "dense", score: 0.88 },
      { key: "bm25", score: 0.97 },
      { key: "idf", score: 0.91 },
      { key: "synonyms", score: 0.78 },
      { key: "exact", score: 1.0 },
    ],
    fused: 0.9,
    vectorOnly: 0.73,
    latency: "0.06 ms",
  },
];

export const stageMeta = [
  { key: "dense", name: "Dense vector", desc: "384d embeddings, SIMD cosine on a local matrix cache.", tag: "<0.1 ms" },
  { key: "bm25", name: "BM25", desc: "SQLite FTS5 keyword relevance over the namespace.", tag: "FTS5" },
  { key: "idf", name: "IDF", desc: "Robertson–Spärck Jones smoothed inverse document frequency.", tag: "rare terms" },
  { key: "synonyms", name: "Synonyms", desc: "Domain dictionary bridges postgres ↔ PostgreSQL, port ↔ port number.", tag: "domain-aware" },
  { key: "exact", name: "Exact tokens", desc: "Ports, IDs, flags, identifiers. Exact overlap is a signal too.", tag: "exact" },
];

export const memoryFeatures = [
  { title: "Namespaces", body: "One SQLite WAL per project. MESH_LOCKED_NAMESPACE can confine a server to a single namespace. Separate databases means separate isolation — not just logical partitions." },
  { title: "Consent", body: "y/n before starting a daemon, registering auto-start, downloading the ~90 MB model, or syncing logs to a cloud API. Nothing happens silently in the background." },
  { title: "Audit", body: "Reads, writes, updates, and deletes are logged to a local, content-free audit DB. Private memories are scrubbed from audit, conflict history, and digests." },
  { title: "Portable", body: "A local SQLite file. No external database daemon. Back it up like any file, move it across machines, or copy it to an offline environment." },
];

export const stats = [
  { value: "2.44 ms", label: "project brief (p50)" },
  { value: "0.05 ms", label: "embed cache hit" },
  { value: "17", label: "MCP tools" },
  { value: "5", label: "retrieval stages" },
  { value: "0", label: "external database daemons" },
];

// Verified benchmark data from benchmarks/RESULTS.md — Mesh v0.8.3, Windows 11 AMD64, Python 3.14.0, SQLite 3.50.4
export const benchmarkData = {
  sdk: [
    { op: "project_brief (500-token)", p50: "2.44 ms", p95: "5.27 ms", verdict: "fast" },
    { op: "task_list (status filter)", p50: "1.05 ms", p95: "3.61 ms", verdict: "fast" },
    { op: "task_create (with tags)", p50: "3.47 ms", p95: "8.48 ms", verdict: "fast" },
    { op: "task_complete (+ evidence)", p50: "3.63 ms", p95: "6.74 ms", verdict: "fast" },
    { op: "changelog / mesh log (markdown)", p50: "3.07 ms", p95: "6.71 ms", verdict: "fast" },
    { op: "recall hybrid top-5 (cached)", p50: "5.33 ms", p95: "10.04 ms", verdict: "ok" },
    { op: "recall hybrid top-5 (uncached)", p50: "8.58 ms", p95: "13.08 ms", verdict: "ok" },
    { op: "learn (new write, ONNX embed)", p50: "9.63 ms", p95: "13.32 ms", verdict: "ok" },
    { op: "embed cache hit", p50: "0.05 ms", p95: "0.17 ms", verdict: "fast" },
    { op: "guard_ingest (secret detection)", p50: "0.12 ms", p95: "0.34 ms", verdict: "fast" },
  ],
  mcp: [
    { op: "mesh_project_brief", p50: "2.77 ms", p95: "3.82 ms", verdict: "fast" },
    { op: "mesh_task_create", p50: "4.26 ms", p95: "6.09 ms", verdict: "fast" },
    { op: "mesh_task_list", p50: "3.38 ms", p95: "6.30 ms", verdict: "fast" },
    { op: "mesh_learn", p50: "7.79 ms", p95: "10.71 ms", verdict: "ok" },
    { op: "mesh_recall", p50: "14.68 ms", p95: "39.72 ms", verdict: "ok" },
    { op: "mesh_log (changelog)", p50: "4.99 ms", p95: "27.29 ms", verdict: "fast" },
    { op: "mesh_inspect", p50: "4.86 ms", p95: "9.20 ms", verdict: "fast" },
    { op: "mesh_contradictions", p50: "2.37 ms", p95: "3.27 ms", verdict: "fast" },
  ],
  scale: [
    { memories: "500", workItems: "100", recall: "143.7 ms", brief: "7.04 ms", taskList: "4.11 ms", dbSize: "5.39 MB", ram: "+195 MB" },
    { memories: "1,000", workItems: "200", recall: "152.1 ms", brief: "7.82 ms", taskList: "4.35 ms", dbSize: "10.6 MB", ram: "+198 MB" },
    { memories: "5,000", workItems: "1,000", recall: "178.4 ms", brief: "9.15 ms", taskList: "5.12 ms", dbSize: "52.4 MB", ram: "+215 MB" },
    { memories: "10,000", workItems: "2,000", recall: "195.2 ms", brief: "11.40 ms", taskList: "6.40 ms", dbSize: "104.8 MB", ram: "+240 MB" },
  ],
};

export const architecture = [
  { title: "Agents", items: ["Claude Code", "Cursor", "Codex CLI", "custom agent"] },
  { title: "Interfaces", items: ["MCP", "HTTP", "CLI", "Python SDK"] },
  { title: "Core", items: ["namespaces", "consent", "auth", "sanitize", "dedupe", "contradictions", "TTL", "tasks", "secrets"], hot: true },
  { title: "Storage", items: ["SQLite WAL", "FTS5", "vector cache", "ONNX / hash-384"] },
];

export const interfaces = [
  {
    id: "python",
    label: "Python SDK",
    lang: "python",
    code: `from mesh import Mesh

# Agent 1 learns an architecture constraint.
agent_a = Mesh(namespace="checkout-service", agent_id="backend-agent")
agent_a.learn(
    "Production PostgreSQL 16 runs on port 5432 with pgBouncer connection pooling",
    memory_type="fact",
    tags=["db", "postgres", "prod"],
)

# Store a secret pointer — never the value itself.
agent_a.learn(
    "Stripe live key for checkout billing",
    memory_type="secret_ref",
    secret_ref="env://STRIPE_SECRET_KEY",
)

# Agent 2 recalls it without talking to Agent 1.
agent_b = Mesh(namespace="checkout-service", agent_id="frontend-agent")
results = agent_b.recall("what port does production postgres use?", n=3)

# Track work across sessions.
task = agent_b.create_work_item(
    title="Ship checkout v2", kind="goal", priority="high"
)`,
    note: "The same namespace is available to every agent. No shared process required. Secrets are stored as pointers, never values.",
  },
  {
    id: "mcp",
    label: "MCP",
    lang: "json",
    code: `{
  "mcpServers": {
    "mesh": {
      "command": "mesh-server",
      "env": {
        "MESH_NAMESPACE": "my-project",
        "MESH_AGENT_ID": "claude-desktop"
      }
    }
  }
}`,
    note: "Works with Claude Code, Claude Desktop, Cursor, Windsurf, and any MCP client. Mesh ships 17 tools including memory, tasks, brief, changelog, secrets, and contradictions.",
  },
  {
    id: "cli",
    label: "CLI",
    lang: "bash",
    code: `# Store facts and query them
mesh add "PostgreSQL 16 runs on port 5432" --tags db,prod --type fact
mesh ask "what port does postgres use?" -n 3

# Store secret pointers (never values)
mesh add "Stripe key for billing" --secret-ref env://STRIPE_SECRET_KEY
mesh run --env STRIPE_KEY=env://STRIPE_SECRET_KEY -- ./deploy.sh

# Track work and get a session brief
mesh task add "Ship auth service" --kind goal --priority high
mesh task done task-1 --summary "Added RS256 signing" --evidence src/auth.py
mesh brief --max-tokens 500
mesh log --since 7d`,
    note: "The CLI is the fastest way to prove the loop: teach a fact, then ask for it. Tasks, secrets, briefs, and changelogs all live here too.",
  },
  {
    id: "http",
    label: "HTTP REST",
    lang: "bash",
    code: `mesh-http --port 7701

TOKEN=$(cat ~/.mesh/auth_token)

# Recall memories
curl -s -X POST http://127.0.0.1:7701/recall \\
  -H "Content-Type: application/json" \\
  -H "X-Mesh-Token: $TOKEN" \\
  -d '{"query": "auth JWT algorithm", "n": 3}'

# Get a project brief
curl -s http://127.0.0.1:7701/brief?max_tokens=500 \\
  -H "X-Mesh-Token: $TOKEN"`,
    note: "Local only. Binding to a non-loopback interface requires MESH_ALLOWED_HOSTS. All routes except /health require a bearer token.",
  },
];

export const faq = [
  {
    q: "Is Mesh a vector database?",
    a: "No. Mesh is a memory layer. It uses SQLite WAL and FTS5 for storage and retrieval, with a local vector cache and local ONNX embeddings. There is no external database daemon to run or manage.",
  },
  {
    q: "Does my data leave my machine?",
    a: "Not by default. Mesh is local-first and does not silently phone home. Embeddings are computed in-process via ONNX Runtime. Cloud log sync, model downloads, and update checks are all explicit opt-ins.",
  },
  {
    q: "What happens when two agents learn conflicting facts?",
    a: "Mesh flags the contradiction for review instead of silently overwriting. Both facts are kept. You can inspect and resolve it from the CLI (mesh contradictions), MCP (mesh_contradictions, mesh_resolve_contradiction), or the dashboard.",
  },
  {
    q: "Do I need a GPU or an API key?",
    a: "No GPU and no API key for local use. Mesh runs CPU embeddings via ONNX Runtime (all-MiniLM-L6-v2, 384-dim). If the model is unavailable, it falls back to the built-in hash-384 engine automatically.",
  },
  {
    q: "Which agents does it work with?",
    a: "Any MCP-compatible client, plus the Python SDK, CLI, and local HTTP API. The installer supports Claude Code, Claude Desktop, Antigravity, Gemini CLI, Cursor, Windsurf, OpenCode, and Codex. Via Agensi, it is listed for 27 agents.",
  },
  {
    q: "How fast is recall?",
    a: "At ~100 memories with ONNX loaded: recall runs in ~5–9 ms p50 end-to-end; the project brief in ~2.4 ms p50; the embedding cache hit in ~0.05 ms. At 10,000 memories, recall is ~195 ms p50. Run benchmark_all_surfaces.py to measure on your hardware.",
  },
  {
    q: "How does mesh setup work?",
    a: "mesh setup asks before doing anything: downloading the ONNX model (~90 MB, SHA-256 verified), starting the optional daemon, and optionally registering login auto-start. Each step is an explicit y/n. Use MESH_NO_MODEL_DOWNLOAD=1 to skip the download entirely.",
  },
  {
    q: "Can I store secrets in Mesh?",
    a: "Do not store secret values. Store pointers: mesh add \"Stripe key\" --secret-ref env://STRIPE_SECRET_KEY. If a secret is accidentally included in content, Mesh detects and redacts it (best-effort), forces local_only=true, and can vault it in the OS keyring with --vault. Secret detection is heuristic; always use secret_ref instead.",
  },
  {
    q: "How do tasks and goals work?",
    a: "Goals and tasks are first-class rows in work_items — not memories. They have status (todo → in_progress → blocked/done/cancelled), priority, parent links, summaries, and evidence. Completion requires a non-empty summary. Use mesh brief to get a token-budgeted session summary, and mesh log to see the project changelog.",
  },
  {
    q: "Is it production-ready?",
    a: "Mesh is MIT-licensed at v0.8.3 (Beta). It has passed a targeted concurrency retest and an independent field test. It is local-first, not a hosted service. Read the security model and consent notes in the README before deploying in a shared or multi-user environment.",
  },
];
