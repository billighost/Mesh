"""Real process death must preserve atomic memory, receipt and outbox recovery."""
from __future__ import annotations

import json

from benchmarks import production_bench as bench
from tests.test_production_storage import isolated_storage as isolated_storage


def test_process_death_before_and_after_commit_then_fresh_process_replay():
    options = bench.parser().parse_args([])
    options.timeout = 30
    probes = bench.crash_probes(options)
    assert {probe["phase"] for probe in probes} == {"crash-before", "crash-after"}
    for probe in probes:
        assert probe["status"] == "completed", json.dumps(probe, sort_keys=True)
        after = probe["phase"] == "crash-after"
        assert probe["process_exit_code"] == (72 if after else 71)
        assert probe["crashed_worker"]["metadata"]["synchronous"] == "FULL"
        assert probe["memory_visible_before_replay"] is after
        assert probe["receipt_status_before_replay"] == ("recorded" if after else "unknown")
        counts = probe["counts_before_replay"]
        assert counts["memories"] == counts["operation_receipts"] == int(after)
        audit_before = probe["audit_before_replay"]
        assert bool(counts["mutation_outbox"] or audit_before["durable_audit_events"]) is after
        assert probe["counts_after_replay"] == {"memories": 1, "operation_receipts": 1}
        assert probe["replay_confirmed"] is True and probe["quick_check"] == ["ok"]
        assert probe["foreign_key_errors"] == 0
        delivery = probe["audit_delivery_after_restart"]
        assert delivery["integrity_ok"] is True and delivery["remaining_outbox_events"] == 0
        assert delivery["durable_audit_events"] == delivery["unique_durable_audit_event_ids"] == 1
        assert delivery["second_delivery_count"] == 0
        assert probe["cleanup_inventory"]


def test_crash_verification_accepts_an_event_delivered_before_observation(isolated_storage, monkeypatch):
    from mesh.audit import deliver_mutation_events
    from mesh.client import ConnectionProfile, MeshClient
    from mesh.memory import Mesh
    from mesh.sqlite_runtime import sqlite3

    root = isolated_storage
    mesh_home = root / "mesh"
    monkeypatch.setenv("MESH_DIR", str(mesh_home))
    monkeypatch.setenv("MESH_FORCE_HASH_EMBED", "1")
    actor = "benchmark-agent-0"
    mesh = Mesh(bench.NAMESPACE, agent_id=actor)
    client = MeshClient(ConnectionProfile(bench.NAMESPACE, mesh_home, agent_id=actor,
                                        producer_scope="local-user"), transport="sdk")
    result = client.call("mesh_learn", {"content": bench.fixture_record(-1)["content"],
                         "tags": ["production-crash-probe"]}, "crash-after")
    deliver_mutation_events(mesh.store, limit=32)
    with mesh.store._connect() as connection:
        assert connection.execute("SELECT count(*) FROM mutation_outbox").fetchone()[0] == 0
    connection = sqlite3.connect((mesh_home / f"audit_{bench.NAMESPACE}.db").as_uri() + "?mode=ro", uri=True)
    try:
        assert connection.execute("SELECT count(*) FROM audit_events WHERE event_id IS NOT NULL").fetchone()[0] == 1
    finally:
        connection.close()
    probe = bench._verify_crash(mesh, {"root": str(root), "actor": actor, "after_commit": True,
                             "staged": {"operation_id": "crash-after", "memory_id": result["memory_id"],
                                        "revision": result["revision"]}})
    assert probe["integrity_ok"] is True and probe["replay_confirmed"] is True
    assert probe["counts_before_replay"]["mutation_outbox"] == 0
    assert probe["audit_before_replay"]["durable_audit_events"] == 1
    assert probe["audit_delivery_after_restart"]["durable_audit_events"] == 1
    assert probe["audit_delivery_after_restart"]["second_delivery_count"] == 0
    mesh.close()


def test_crash_observation_does_not_create_an_absent_audit_database(tmp_path):
    assert bench._durable_audit_counts(tmp_path) == (0, 0)
    assert not (tmp_path / f"audit_{bench.NAMESPACE}.db").exists()
