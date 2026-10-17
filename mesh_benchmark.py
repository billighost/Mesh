"""
mesh_benchmark.py — High-speed latency benchmark for Mesh (ONNX + SQLite WAL + SIMD engine).
Run:
    python mesh_benchmark.py
"""
from __future__ import annotations

import os
import statistics
import sys
import time
import uuid

# Ensure UTF-8 console output on Windows so box-drawing characters and emojis never fail
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

os.environ.setdefault("MESH_EMBED_BACKEND", "auto")


def bench(label: str, fn, reps: int = 5):
    times = []
    result = None
    for _ in range(reps):
        t0 = time.perf_counter()
        result = fn()
        times.append((time.perf_counter() - t0) * 1000.0)
    avg = statistics.mean(times)
    mn = min(times)
    mx = max(times)
    p50 = statistics.median(times)
    verdict = "🟢 FAST" if avg < 50.0 else ("🟡 OK" if avg < 300.0 else "🔴 SLOW")
    print(
        f"  {label:<40} avg={avg:7.2f}ms  min={mn:6.2f}  max={mx:6.2f}  p50={p50:6.2f}  {verdict}"
    )
    return avg, result


def get_id(r) -> str:
    """Extract memory_id whether result is a dict or string."""
    if isinstance(r, dict):
        return r.get("memory_id") or r.get("id") or ""
    return str(r)


def main() -> None:
    from mesh.context_builder import build_context
    from mesh.memory import (
        Mesh,
        _encode_texts,
        ensure_model_downloaded,
        get_engine_status,
        warmup_model,
    )
    from mesh.namespaces import delete_namespace
    from mesh.version import VERSION

    NS = f"bench-{uuid.uuid4().hex[:8]}"

    print("\n" + "=" * 78)
    print(f"  MESH v{VERSION} MCP TOOL BENCHMARK  (Auto ONNX / SIMD Hybrid Engine)")
    print("=" * 78)
    print(f"\n  Namespace : {NS}")
    print("  Reps/op   : 5\n")

    try:
        # ── Warmup ─────────────────────────────────────────────────────────────
        print("[ WARMUP ] Initializing embedding engine & vector cache...")
        # Wait briefly if a first-time background ONNX download is finishing
        ensure_model_downloaded(blocking=False, interactive_prompt=False)
        t0 = time.perf_counter()
        warmup_model(namespace=NS, blocking=False, assume_yes=False)
        warmup_ms = (time.perf_counter() - t0) * 1000.0
        st = get_engine_status()
        print(
            f"           Done in {warmup_ms:.2f}ms  "
            f"backend={st['backend']}  onnx_cached={st.get('onnx_cached_on_disk')}\n"
        )

        mesh = Mesh(namespace=NS, agent_id="bench-agent")

        # ── Seed 10 memories ───────────────────────────────────────────────────
        print("[ SEEDING ] Writing 10 base memories...")
        seed_ids: list[str] = []
        t_seed = time.perf_counter()
        for i in range(10):
            r = mesh.learn(
                content=f"Seed fact #{i}: The quick brown fox jumps over the lazy dog. Index={i}.",
                memory_type="fact",
                tags=["bench", f"seed-{i}"],
                confidence=0.9,
            )
            seed_ids.append(get_id(r))
        seed_ms = (time.perf_counter() - t_seed) * 1000.0
        print(f"            Done. {len(seed_ids)} memories seeded in {seed_ms:.2f}ms.\n")

        print("─" * 78)
        print("  OPERATION LATENCIES  (5 reps each)")
        print("─" * 78 + "\n")

        # ── 1. learn ───────────────────────────────────────────────────────────
        print("[ 1 ] mesh_learn — store a new memory (write + embed + dedup check)")
        learn_ids: list[str] = []
        learn_ctr = [0]

        def do_learn():
            learn_ctr[0] += 1
            r = mesh.learn(
                content=(
                    f"AI agents use shared memory to coordinate efficiently. "
                    f"seq={learn_ctr[0]} ts={time.perf_counter():.6f}"
                ),
                memory_type="fact",
                tags=["bench"],
                confidence=0.85,
            )
            learn_ids.append(get_id(r))
            return r

        bench("learn (write + embed + dedup check)", do_learn)
        bench(
            "learn (duplicate in-place reinforcement)",
            lambda: mesh.learn(
                content="Seed fact #0: The quick brown fox jumps over the lazy dog. Index=0.",
                memory_type="fact",
                tags=["bench", "reinforced"],
                confidence=0.95,
            ),
        )

        # ── 2. recall ──────────────────────────────────────────────────────────
        print("\n[ 2 ] mesh_recall — 3-stage hybrid semantic + BM25 + token search")
        bench(
            "recall top-5  ('AI agents coordinate')",
            lambda: mesh.recall("AI agents coordinate using memory", n=5),
        )
        bench(
            "recall top-1  ('quick brown fox')",
            lambda: mesh.recall("quick brown fox", n=1),
        )
        bench(
            "recall top-10 ('benchmark seed')",
            lambda: mesh.recall("benchmark seed memory index", n=10),
        )

        # ── 3. update ──────────────────────────────────────────────────────────
        print("\n[ 3 ] mesh_update — modify existing memory in-place (re-embed)")
        target_id = seed_ids[0]
        upd_ctr = [0]

        def do_update():
            upd_ctr[0] += 1
            return mesh.update(
                memory_id=target_id,
                content=(
                    f"Updated: Mesh is a high-speed shared memory layer. "
                    f"rev={upd_ctr[0]} ts={time.perf_counter():.6f}"
                ),
                confidence=0.95,
            )

        bench("update (re-embed + overwrite)", do_update)

        # ── 4. forget ──────────────────────────────────────────────────────────
        print("\n[ 4 ] mesh_forget — delete memory by ID")
        forget_pool = [x for x in learn_ids if x]

        def do_forget():
            if forget_pool:
                mesh.forget(memory_id=forget_pool.pop())
            else:
                r = mesh.learn(content=f"temp ts={time.perf_counter()}", memory_type="fact")
                mesh.forget(memory_id=get_id(r))

        bench("forget (delete by ID)", do_forget)

        # ── 5. get / inspect ───────────────────────────────────────────────────
        print("\n[ 5 ] mesh_inspect — fetch single memory by ID & list recent")
        bench("get (fetch single memory by ID)", lambda: mesh.get(memory_id=seed_ids[-1]))
        bench("inspect (list top-10 memories)", lambda: mesh.inspect(limit=10))

        # ── 6. count ───────────────────────────────────────────────────────────
        print("\n[ 6 ] mesh_count — count all memories in namespace")
        bench("count (namespace total)", lambda: mesh.count())

        # ── 7. context ─────────────────────────────────────────────────────────
        print("\n[ 7 ] mesh_context — build system prompt string")
        bench(
            "build_context top-20 (general)",
            lambda: build_context(namespace=NS, max_memories=20),
        )
        bench(
            "build_context top-5  (focused query)",
            lambda: build_context(
                query="AI agents coordinate",
                namespace=NS,
                max_memories=5,
            ),
        )

        # ── 8. patterns ────────────────────────────────────────────────────────
        print("\n[ 8 ] mesh_patterns — detect topic patterns & gaps")
        bench("patterns (detect)", lambda: mesh.patterns())

        # ── 9. contradictions ──────────────────────────────────────────────────
        print("\n[ 9 ] mesh_contradictions — scan for conflicting facts")
        bench("contradictions (detect)", lambda: mesh.contradictions())

        # ── 10. embed cache hit vs miss ────────────────────────────────────────
        print("\n[ 10 ] embed cache — uncached inference vs LRU cache hit")
        bench(
            "embed MISS (uncached phrase inference)",
            lambda: _encode_texts([f"unique uncached phrase ever {uuid.uuid4()}"]),
        )
        repeated_phrase = "the shared mesh benchmark repeated text for cache hit"
        _encode_texts([repeated_phrase])
        bench(
            "embed HIT  (LRU cache, zero inference)",
            lambda: _encode_texts([repeated_phrase]),
        )

        # ── Summary ────────────────────────────────────────────────────────────
        print("\n" + "=" * 78)
        print("  ENGINE STATUS (post-benchmark)")
        print("=" * 78)
        for k, v in get_engine_status().items():
            print(f"  {k:<40} {v}")

    finally:
        # ── Cleanup ────────────────────────────────────────────────────────────
        print(f"\n[ CLEANUP ] Deleting bench namespace '{NS}'...")
        try:
            delete_namespace(NS)
            print("            Done.")
        except Exception as e:
            print(f"            Skipped: {e}")

    print("\n✅ Benchmark complete!\n")


if __name__ == "__main__":
    main()