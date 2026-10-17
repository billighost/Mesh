import json
import sys
from pathlib import Path

bench_dir = Path(__file__).resolve().parent
if str(bench_dir.parent.parent) not in sys.path:
    sys.path.insert(0, str(bench_dir.parent.parent))

from tests.bench.benchmark import run_benchmark


def test_retrieval_benchmark_no_regression():
    bench_dir = Path(__file__).resolve().parent
    baseline_path = bench_dir / "baseline.json"
    assert baseline_path.exists(), "baseline.json must exist"

    with open(baseline_path, "r", encoding="utf-8") as f:
        baseline = json.load(f)

    current = run_benchmark(save_baseline=False)

    # Hard-negative false positives must drop to ~0 (or at most baseline)
    assert current["hard_negative_fp_rate_rank1"] <= baseline["hard_negative_fp_rate_rank1"]

    # MRR must not regress more than 0.02 below baseline
    assert current["mrr"] >= baseline["mrr"] - 0.02, (
        f"MRR regressed from {baseline['mrr']} to {current['mrr']}"
    )

    # Recall@5 must not regress
    assert current["recall_at_5"] >= baseline["recall_at_5"] - 0.02
