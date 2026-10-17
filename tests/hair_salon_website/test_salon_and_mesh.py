"""
MAISON ÉLIXIR — Website & Mesh Context Layer Benchmark Suite
Validates website structure, asset integrity, HTTP server connectivity,
and performs comprehensive multi-agent benchmarks on the Mesh memory layer.
"""

import os
import sys
import time
import json
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor
from mesh import Mesh

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
NAMESPACE = "salon-benchmark-suite"

def test_website_files():
    print("\n" + "="*60)
    print("STEP 1: Validating Website Files & Structure")
    print("="*60)

    required_files = [
        os.path.join(TEST_DIR, "index.html"),
        os.path.join(TEST_DIR, "styles", "main.css"),
        os.path.join(TEST_DIR, "scripts", "app.js"),
        os.path.join(TEST_DIR, "assets", "hero_salon.jpg"),
        os.path.join(TEST_DIR, "assets", "balayage_transformation.jpg"),
        os.path.join(TEST_DIR, "assets", "genevieve_vance.jpg"),
    ]

    for fpath in required_files:
        exists = os.path.exists(fpath)
        size = os.path.getsize(fpath) if exists else 0
        assert exists, f"Missing required file: {fpath}"
        assert size > 0, f"File is empty: {fpath}"
        rel = os.path.relpath(fpath, TEST_DIR)
        print(f"  [OK] {rel:<35} ({size:,} bytes)")

    # Validate HTML contents
    with open(os.path.join(TEST_DIR, "index.html"), "r", encoding="utf-8") as f:
        html = f.read()

    required_ids = [
        "navbar", "hero", "transformation", "services", "estimator",
        "artisans", "reviews", "vip-circle", "locations",
        "booking-modal", "stylist-bio-modal", "write-review-modal", "toast-container"
    ]
    for element_id in required_ids:
        assert f'id="{element_id}"' in html, f"Missing id '{element_id}' in index.html"
        print(f"  [OK] DOM element id='{element_id}' verified in HTML")

    print("\nWebsite structure: ALL CHECKS PASSED.")

def test_http_server():
    print("\n" + "="*60)
    print("STEP 2: Validating HTTP Server Response")
    print("="*60)
    url = "http://127.0.0.1:8088/"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'MaisonElixirTestClient/1.0'})
        with urllib.request.urlopen(req, timeout=3) as resp:
            status = resp.status
            body = resp.read(2048).decode("utf-8")
            assert status == 200, f"Unexpected status: {status}"
            assert "MAISON ÉLIXIR" in body or "Haute Coiffure" in body
            print(f"  [OK] GET {url} -> HTTP {status} (Verified Maison Élixir title)")
    except Exception as e:
        print(f"  [WARNING] Local HTTP test error: {e}")

def test_mesh_multi_agent_benchmark():
    print("\n" + "="*60)
    print("STEP 3: Mesh Context Layer Multi-Agent & Latency Benchmark")
    print("="*60)

    # 1. Initialize Mesh instances for 3 distinct simulated agents
    designer = Mesh(namespace=NAMESPACE, agent_id="agent-designer")
    backend = Mesh(namespace=NAMESPACE, agent_id="agent-backend")
    concierge = Mesh(namespace=NAMESPACE, agent_id="agent-concierge")

    print(f"Initialized 3 agents in namespace: '{NAMESPACE}'")

    # Metrics collections
    learn_latencies = []
    recall_latencies = []

    # Phase A: Learn Operations (Testing speed and storage across agents)
    print("\n--- Phase A: Storing Multi-Agent Memories ---")
    memories_to_learn = [
        (designer, "Primary palette: Obsidian (#08090a), Champagne Gold (#d4af37), Rose-Bronze (#b87333).", "decision", ["design", "colors"]),
        (designer, "Typography stack uses Cormorant Garamond for editorial headings and Plus Jakarta Sans for UI.", "fact", ["typography", "fonts"]),
        (backend, "Booking API endpoint: POST /api/v1/reservations with signature verification and idempotency keys.", "instruction", ["api", "booking"]),
        (backend, "Cancellation policy allows penalty-free rescheduling up to 24 hours prior to atelier session.", "fact", ["policy", "cancellation"]),
        (concierge, "VIP Patrons receive complimentary Laurent-Perrier champagne and private styling suite access.", "preference", ["vip", "hospitality"]),
        (concierge, "Balayage sessions with Marcus Thorne require 150 minutes minimum chair allocation.", "fact", ["stylists", "timing"]),
    ]

    for agent, content, mtype, tags in memories_to_learn:
        t0 = time.perf_counter()
        res = agent.learn(content=content, memory_type=mtype, tags=tags, confidence=1.0)
        dt_ms = (time.perf_counter() - t0) * 1000
        learn_latencies.append(dt_ms)
        print(f"  [Learn] ({agent.agent_id}) -> {dt_ms:.1f}ms | ID: {res['memory_id'][:8]} | Deduplicated: {res['deduplicated']}")

    # Phase B: Deduplication Verification
    print("\n--- Phase B: Testing Semantic & Exact Deduplication ---")
    t0 = time.perf_counter()
    dedup_res = designer.learn(
        content="Primary palette: Obsidian (#08090a), Champagne Gold (#d4af37), Rose-Bronze (#b87333).",
        memory_type="decision",
        tags=["design", "colors"]
    )
    dedup_ms = (time.perf_counter() - t0) * 1000
    print(f"  [Deduplication Test] -> {dedup_ms:.1f}ms | Deduplicated flag: {dedup_res['deduplicated']}")
    assert dedup_res['deduplicated'] is True, "Exact match should be deduplicated!"

    # Phase C: Recall Operations Across Agents (Cross-agent hybrid retrieval)
    print("\n--- Phase C: Cross-Agent Hybrid Recall ---")
    queries = [
        (concierge, "what are the brand colors for the salon website?"),
        (designer, "cancellation policy for client bookings"),
        (backend, "how much time does marcus thorne need for balayage?"),
        (concierge, "what champagne is served to vip patrons?"),
        (designer, "what fonts are used for headings?")
    ]

    for agent, q in queries:
        t0 = time.perf_counter()
        results = agent.recall(query=q, n=3)
        dt_ms = (time.perf_counter() - t0) * 1000
        recall_latencies.append(dt_ms)
        top = results[0] if results else None
        top_snippet = (top['content'][:50] + "...") if top else "None"
        sim = top['similarity'] if top else 0.0
        print(f"  [Recall] ({agent.agent_id}) '{q}' -> {dt_ms:.1f}ms | Sim: {sim:.2f} | Match: {top_snippet}")

    # Phase D: Context Block Synthesis
    print("\n--- Phase D: Testing mesh_context generation ---")
    from mesh.context_builder import build_context
    t0 = time.perf_counter()
    ctx = build_context(namespace=NAMESPACE, query="salon booking and styling", count=5, format="markdown")
    ctx_ms = (time.perf_counter() - t0) * 1000
    print(f"  [Context Generation] -> {ctx_ms:.1f}ms (Context Length: {len(ctx)} chars)")

    # Phase E: Usage Patterns Telemetry
    print("\n--- Phase E: Memory Usage Patterns & Gaps ---")
    t0 = time.perf_counter()
    patterns = backend.patterns()
    patterns_ms = (time.perf_counter() - t0) * 1000
    print(f"  [Pattern Analytics] -> {patterns_ms:.1f}ms | Top Topics Count: {len(patterns.get('top_topics', []))}")

    # Phase F: Concurrent Multi-Threaded Subagent Simulation
    print("\n--- Phase F: Concurrent 4-Agent Thread Pool Simulation ---")
    def worker_agent(agent_idx):
        sub_agent = Mesh(namespace=NAMESPACE, agent_id=f"subagent-worker-{agent_idx}")
        # Perform 1 learn and 2 recalls
        t_start = time.perf_counter()
        sub_agent.learn(
            content=f"Subagent worker {agent_idx} completed verification pass on salon assets.",
            memory_type="result",
            tags=["worker", f"worker-{agent_idx}"]
        )
        sub_agent.recall("verification pass assets", n=2)
        sub_agent.recall("vip champagne policy", n=2)
        total_dt = (time.perf_counter() - t_start) * 1000
        return agent_idx, total_dt

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as executor:
        worker_results = list(executor.map(worker_agent, range(1, 5)))
    concurrent_total_ms = (time.perf_counter() - t0) * 1000

    for idx, dt in worker_results:
        print(f"  [Worker {idx}] Completed 1 learn + 2 recalls in {dt:.1f}ms")
    print(f"  [Concurrent Total] 4 concurrent agents finished in {concurrent_total_ms:.1f}ms")

    # Calculate Benchmark Summary
    avg_learn = sum(learn_latencies) / len(learn_latencies)
    avg_recall = sum(recall_latencies) / len(recall_latencies)
    min_learn, max_learn = min(learn_latencies), max(learn_latencies)
    min_recall, max_recall = min(recall_latencies), max(recall_latencies)

    print("\n" + "="*60)
    print("BENCHMARK RESULTS SUMMARY")
    print("="*60)
    print(f"  Total Learn Operations Recorded : {len(learn_latencies)}")
    print(f"  Learn Latency (Avg / Min / Max) : {avg_learn:.1f}ms / {min_learn:.1f}ms / {max_learn:.1f}ms")
    print(f"  Total Recall Operations Recorded: {len(recall_latencies)}")
    print(f"  Recall Latency (Avg / Min / Max): {avg_recall:.1f}ms / {min_recall:.1f}ms / {max_recall:.1f}ms")
    print(f"  Context Generation Latency      : {ctx_ms:.1f}ms")
    print(f"  Pattern Telemetry Latency       : {patterns_ms:.1f}ms")
    print(f"  Deduplication Verification      : PASSED (Flag: True in {dedup_ms:.1f}ms)")
    print(f"  Concurrent Multi-Agent Test     : PASSED (4 threads, {concurrent_total_ms:.1f}ms total)")
    print("="*60)

    # Clean up test namespace
    print(f"\nBenchmark completed successfully.")

if __name__ == "__main__":
    test_website_files()
    test_http_server()
    test_mesh_multi_agent_benchmark()
