"""
benchmarks/prepare_pilot.py — Extract 20 seeded questions from official LongMemEval-S dataset.
"""
import json
import os
import random
import sys
from pathlib import Path

SRC_FILE = Path("benchmarks/data/longmemeval_s_cleaned.json")
OUT_FILE = Path("benchmarks/data/longmemeval_pilot_20.json")

def prepare_pilot(seed: int = 42, count: int = 20) -> list[dict]:
    if not SRC_FILE.exists():
        raise FileNotFoundError(f"Missing {SRC_FILE}. Download first.")

    print(f"Loading {SRC_FILE}...")
    with open(SRC_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"Total questions in source: {len(data)}")
    
    # Exclude abstention questions
    non_abs = [
        q for q in data
        if "_abs" not in q.get("question_id", "") and "_abs" not in q.get("question_type", "")
    ]
    print(f"Non-abstention questions: {len(non_abs)} (excluded {len(data) - len(non_abs)} abstention questions)")

    # Sort deterministically by question_id before sampling
    non_abs.sort(key=lambda x: str(x.get("question_id", "")))

    rng = random.Random(seed)
    pilot = rng.sample(non_abs, count)

    # Sort pilot deterministically by question_id
    pilot.sort(key=lambda x: str(x.get("question_id", "")))

    # Compute turns per question
    stats = []
    total_turns = 0
    type_counts = {}
    for idx, q in enumerate(pilot, 1):
        q_id = q.get("question_id")
        q_type = q.get("question_type", "unknown")
        type_counts[q_type] = type_counts.get(q_type, 0) + 1
        
        haystack = q.get("haystack_sessions", [])
        num_sessions = len(haystack)
        num_turns = sum(len(s) for s in haystack)
        total_turns += num_turns
        
        stats.append({
            "index": idx,
            "question_id": q_id,
            "question_type": q_type,
            "sessions_count": num_sessions,
            "turns_count": num_turns,
            "question_text": q.get("question", "")[:60] + "...",
        })

    print(f"\nSampled {len(pilot)} questions with seed={seed}:")
    print(f"{'Idx':<4} {'Question ID':<16} {'Type':<26} {'Sessions':<10} {'Turns':<8} {'Question Preview'}")
    print("-" * 90)
    for s in stats:
        print(f"{s['index']:<4} {s['question_id']:<16} {s['question_type']:<26} {s['sessions_count']:<10} {s['turns_count']:<8} {s['question_text']}")
    print("-" * 90)
    print(f"Total turns across 20 questions: {total_turns} (mean {total_turns / count:.1f} turns/question)")
    print(f"Question type breakdown: {type_counts}")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(pilot, f, indent=2, ensure_ascii=False)
    print(f"\nSaved pilot dataset to {OUT_FILE} ({OUT_FILE.stat().st_size:,} bytes)")
    return pilot

if __name__ == "__main__":
    prepare_pilot()
