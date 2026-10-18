export const SITE_URL = "https://mesh.example";
export const AGENSI_URL = "https://www.agensi.io/skills/mesh-context-layer";
export const GITHUB_URL = "https://github.com/billighost/mesh";
export const PYPI_URL = "https://pypi.org/project/mesh-context-layer/";

export const INSTALL_CMD = "pip install mesh-context-layer";

export const nav = [
  { label: "How it works", href: "#how" },
  { label: "Search", href: "#search" },
  { label: "Memory", href: "#memory" },
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
  { title: "Namespaces", body: "One SQLite WAL per project. MESH_LOCKED_NAMESPACE can confine a server to a single namespace." },
  { title: "Consent", body: "y/n before starting a daemon, registering auto-start, downloading the model, or syncing logs to a cloud API." },
  { title: "Audit", body: "Reads, writes, updates, and deletes are logged. Private versions are scrubbed from audit and conflict history." },
  { title: "Portable", body: "A local SQLite file. No external database daemon. Back it up like a file." },
];

export const stats = [
  { value: "<0.1 ms", label: "dense cosine" },
  { value: "384d", label: "embeddings" },
  { value: "5", label: "retrieval stages" },
  { value: "10", label: "MCP tools" },
  { value: "0", label: "external database daemons" },
];

export const architecture = [
  { title: "Agents", items: ["Claude Code", "Cursor", "Codex CLI", "custom agent"] },
  { title: "Interfaces", items: ["MCP", "HTTP", "CLI", "Python SDK"] },
  { title: "Core", items: ["namespaces", "consent", "auth", "sanitize", "dedupe", "contradictions", "TTL"], hot: true },
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
    "Production PostgreSQL 16 database runs on port 5432 with pgBouncer connection pooling",
    memory_type="fact",
    tags=["db", "postgres", "prod"],
)

# Agent 2 recalls it without talking to Agent 1.
agent_b = Mesh(namespace="checkout-service", agent_id="frontend-agent")
results = agent_b.recall("what port does production postgres use?", n=3)`,
    note: "The same namespace is available to every agent. No shared process required.",
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
    note: "Works with Claude Desktop, Cursor, Windsurf, and other MCP clients. Mesh ships 10 tools and a bundled agent skill (mesh install-skills).",
  },
  {
    id: "cli",
    label: "CLI",
    lang: "bash",
    code: `mesh add "PostgreSQL 16 runs on port 5432" --tags db,prod --type fact
mesh ask "what port does postgres use?" --n 3
mesh conflicts
mesh context --query "database config" --format markdown`,
    note: "The CLI is the fastest way to prove the loop: teach a fact, then ask for it.",
  },
  {
    id: "http",
    label: "HTTP REST",
    lang: "bash",
    code: `mesh-http --port 7701

TOKEN=$(cat ~/.mesh/auth_token)

curl -s -X POST http://127.0.0.1:7701/recall \\
  -H "Content-Type: application/json" \\
  -H "X-Mesh-Token: $TOKEN" \\
  -d '{"query": "auth JWT algorithm", "n": 3}'`,
    note: "Local only. Binding to a non-loopback interface requires MESH_ALLOWED_HOSTS.",
  },
];

export const faq = [
  {
    q: "Is Mesh a vector database?",
    a: "No. Mesh is a memory layer. It uses SQLite WAL and FTS5 for storage and retrieval, with a local vector cache and local embeddings. There is no external database daemon to run.",
  },
  {
    q: "Does my data leave my machine?",
    a: "Not by default. Mesh is local-first and does not silently phone home. Cloud log sync and update checks are explicit opt-ins.",
  },
  {
    q: "What happens when two agents learn conflicting facts?",
    a: "Mesh flags the contradiction for review instead of silently overwriting. You can inspect and resolve it from the CLI, MCP, or the dashboard.",
  },
  {
    q: "Do I need a GPU or an API key?",
    a: "No GPU and no API key for local use. Mesh runs CPU embeddings via ONNX; if the model is unavailable, it falls back to the built-in hash-384 engine.",
  },
  {
    q: "Which agents does it work with?",
    a: "Any MCP-compatible client, plus the Python SDK, CLI, and local HTTP API. Via Agensi, it is listed for 27 agents, including Claude Code, Cursor, Codex CLI, Muse, Dots, OpenClaw, Manus, Grok, and Grok Bot.",
  },
  {
    q: "How fast is recall?",
    a: "The documented dense vector cosine stage runs in under 0.1 ms on the local matrix cache; the hash-384 fallback serves requests in under 0.05 ms. End-to-end recall depends on corpus size and the stages involved.",
  },
  {
    q: "Is it production-ready?",
    a: "Mesh is MIT-licensed and at v0.6.5. It is a local-first library and local server, not a hosted service. Read the security and consent notes in the README before deploying it in a shared environment.",
  },
];
