"""Tiny stdlib-only isolation, accounting and supervisor regression contracts."""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import stat
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

BENCHMARKS = Path(__file__).absolute().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCHMARKS))
import prepare_retrieval_ci as preparation
import production_retrieval as production
import replay_retrieval as replay
import retrieval_ci as ci


class IsolationContracts(unittest.TestCase):
    def test_all_profiles_and_provider_credentials_are_isolated_before_runtime_import(self):
        inherited = {"OPENAI_API_KEY": "YOUR_TOKEN_HERE", "ANTHROPIC_AUTH_TOKEN": "YOUR_TOKEN_HERE",
                     "AWS_PROFILE": "FAKE_PROFILE", "CLAUDE_CODE_OAUTH_TOKEN": "YOUR_TOKEN_HERE",
                     "CODEX_HOME": "FAKE_CONFIG", "CLAUDE_CONFIG_DIR": "FAKE_CONFIG",
                     "MESH_NAMESPACE": "fake-real-profile", "MESH_AUTO_APPROVE": "1", "MESH_NO_IMPLICIT_CLOUD": "0"}
        with tempfile.TemporaryDirectory(prefix="mesh-retrieval-ci-contract-") as temporary, mock.patch.dict(os.environ, inherited, clear=True):
            root = Path(temporary)
            replay.configure_isolation(root)
            for name in ("MESH_HOME", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME",
                         "XDG_CACHE_HOME", "XDG_DATA_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_HOME"):
                self.assertTrue(Path(os.environ[name]).is_relative_to(root), name)
                self.assertTrue(Path(os.environ[name]).is_dir(), name)
            for name in ("OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_PROFILE", "CLAUDE_CODE_OAUTH_TOKEN", "MESH_NAMESPACE"):
                self.assertNotIn(name, os.environ)
            self.assertEqual(os.environ["MESH_AUTO_APPROVE"], "0")
            self.assertEqual(os.environ["MESH_NO_IMPLICIT_CLOUD"], "1")
            self.assertEqual(os.environ["MESH_NO_AUTO_DAEMON"], "1")

    def test_manifest_and_source_export_filenames_cannot_escape(self):
        with tempfile.TemporaryDirectory(prefix="mesh-retrieval-ci-contract-") as temporary:
            root = Path(temporary)
            for name in ("../escape", "/absolute", "C:/FAKE/escape", "a\\escape", "a//escape", "./escape", "a/../escape"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    replay.safe_member(root, name, must_exist=False)
                with mock.patch.object(replay, "digest_file") as digest, self.assertRaises(ValueError):
                    replay.verify_exports(root, {"output_sha256": {name: "a" * 64}})
                digest.assert_not_called()

    def test_dataset_manifest_traversal_is_rejected_before_reading_other_files(self):
        with tempfile.TemporaryDirectory(prefix="mesh-retrieval-ci-contract-") as temporary:
            root = Path(temporary)
            path = root / "manifest.json"
            path.write_text(json.dumps({"files": {"../other-profile": "a" * 64}}), encoding="utf-8")
            with mock.patch.object(production, "digest_file") as digest, self.assertRaises(ValueError):
                production.verify_manifest(path)
            digest.assert_not_called()

    def test_reparse_ancestor_is_rejected_before_resolve(self):
        with tempfile.TemporaryDirectory(prefix="mesh-retrieval-ci-contract-") as temporary:
            root = Path(temporary)
            ancestor = root / "linked"
            ancestor.mkdir()
            path = ancestor / "model.onnx"
            path.write_bytes(b"fake")
            original = Path.lstat

            def metadata(value):
                if value == ancestor:
                    return types.SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
                return original(value)

            with mock.patch.object(Path, "lstat", metadata), mock.patch.object(Path, "resolve") as resolve:
                with self.assertRaisesRegex(ValueError, "reparse"):
                    replay.checked_path(path)
                resolve.assert_not_called()

    def test_output_roots_are_confined_to_named_temp_or_repository_work(self):
        with tempfile.TemporaryDirectory(prefix="mesh-other-contract-") as temporary:
            repository = Path(temporary)
            (repository / ".work").mkdir()
            self.assertEqual(replay.isolated_artifact_path(repository / ".work/models", repository), repository / ".work/models")
            with self.assertRaises(ValueError):
                replay.isolated_artifact_path(repository / "outside", repository)

    def test_cached_static_data_never_downloads_in_local_only_mode(self):
        with tempfile.TemporaryDirectory(prefix="mesh-retrieval-ci-contract-") as temporary:
            path = Path(temporary) / "static.bin"
            path.write_bytes(b"fake fixture")
            with mock.patch.object(preparation.urllib.request, "urlopen") as network:
                preparation.verified_download("https://example.org/static", path, preparation.digest(path), maximum_bytes=32, local_only=True)
                with self.assertRaises(ValueError):
                    preparation.verified_download("https://example.org/static", path.with_name("missing.bin"), "a" * 64, maximum_bytes=32, local_only=True)
                network.assert_not_called()

    def test_archive_traversal_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory(prefix="mesh-retrieval-ci-contract-") as temporary:
            root = Path(temporary)
            (root / "archives").mkdir()
            archive = root / "archives/nfcorpus.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../escape", "fake")
            pin = dict(preparation.DATASETS["nfcorpus"], archive_sha256=preparation.digest(archive))
            with mock.patch.dict(preparation.DATASETS, {"nfcorpus": pin}), self.assertRaisesRegex(ValueError, "Unsafe"):
                preparation.prepare_dataset("nfcorpus", root, local_only=True)
            self.assertFalse((root / "escape").exists())


class IdentityContracts(unittest.TestCase):
    def test_ingest_worker_and_readiness_bind_full_identity(self):
        seen = []
        generation, incarnation = "a" * 32, "b" * 32
        row = {"id": "m", "content": "synthetic fact", "revision": 1, "incarnation": incarnation,
               "store_generation": generation, "local_only": False, "quarantined": False, "embedding_model": production.MODEL_TAG}

        class Store:
            db_path = Path("FAKE_TEMP/mesh-home/mesh_fixture.db")
            store_generation = generation
            pending = 1

            def embedding_job_stats(self):
                return {"pending_count": self.pending}

            def list_embedding_jobs(self, **_kwargs):
                return [{"memory_id": "m", "revision": 1, "target_model": production.MODEL_TAG,
                         "attempts": 0, "incarnation": incarnation, "store_generation": generation}]

        class Mesh:
            store, namespace = Store(), "fixture"

            def learn_many(self, _inputs, **_kwargs):
                return [{"memory_id": "m", "revision": 1, "status": "stored"}]

            def get(self, _identifier, **_kwargs):
                return row

            def wait_for_indexed(self, ids, **kwargs):
                seen.append(kwargs)
                return {"status": "indexed", "indexed": ids}

        mesh = Mesh()

        def hint(*args, **kwargs):
            self.assertEqual(kwargs, {"incarnation": incarnation, "store_generation": generation})
            return (args, kwargs)

        def process(_hints):
            mesh.store.pending = 0

        module = types.SimpleNamespace(_ReembedHint=hint, _process_reembed_batch=process)
        with tempfile.TemporaryDirectory(prefix="mesh-retrieval-ci-contract-") as temporary:
            path = Path(temporary) / "corpus.jsonl"
            path.write_text(json.dumps({"id": "d", "content": "synthetic fact"}) + "\n", encoding="utf-8")
            records, _summary = production.ingest_corpus(mesh, path, "prospective", "complete", module)
        self.assertEqual(seen[0]["expected_incarnations"], {"m": incarnation})
        self.assertEqual(seen[0]["expected_store_generation"], generation)
        self.assertEqual(records[0]["current_incarnation"], incarnation)
        self.assertEqual(records[0]["store_generation"], generation)

    def test_default_sdk_encodes_each_case_and_accumulates_lme_observations(self):
        calls = []
        module = types.SimpleNamespace(_embed_query_with_tag=lambda _query: ([0.0] * 384, production.MODEL_TAG),
                                       _encode_texts_with_tag=lambda _texts: None, _process_reembed_batch=lambda _hints: None)
        original = module._embed_query_with_tag

        class Mesh:
            def recall(self, query, **kwargs):
                calls.append(kwargs)
                module._embed_query_with_tag(query)
                return [{"id": "m", "content": "FAKE_BODY_NOT_EXPORTED", "revision": 1}]

        metadata, output = {}, io.StringIO()
        records = [{"acceptance": {"memory_id": "m"}, "document_id": "d", "faithful_public": True}]
        for index in range(2):
            production.fresh_queries(Mesh(), [{"id": str(index), "query": "synthetic"}], records, {96: output}, metadata, module)
        self.assertEqual(metadata["default_query_backend_observations"]["normal_sdk_query_encoding_calls"], 2)
        self.assertEqual([row["query_ordinal"] for row in metadata["first_six_gross_diagnostics"]], [1, 2])
        self.assertIs(module._embed_query_with_tag, original)
        self.assertTrue(all("_precomputed_embedding" not in call and "record_access" not in call for call in calls))
        self.assertNotIn("FAKE_BODY_NOT_EXPORTED", output.getvalue())
        self.assertTrue(all(json.loads(line)["candidate_cap"] is None for line in output.getvalue().splitlines()))


class SupervisorContracts(unittest.TestCase):
    @staticmethod
    def fake_psutil(rss=1, cpu=0):
        class Missing(Exception):
            pass

        process = types.SimpleNamespace(pid=9876, create_time=lambda: 1.0, children=lambda **_kwargs: [],
                                        memory_info=lambda: types.SimpleNamespace(rss=rss),
                                        cpu_times=lambda: types.SimpleNamespace(user=cpu, system=0.0))
        return types.SimpleNamespace(Process=lambda _pid: process, NoSuchProcess=Missing, ZombieProcess=Missing), process

    def test_rss_cpu_and_wall_caps_stop_owned_process(self):
        for expected, rss, cpu, deadline in (("rss_cap", 11, 0, 100), ("cpu_cap", 1, 11, 100), ("wall_cap", 1, 0, 1.5)):
            with self.subTest(bound=expected):
                module, _ = self.fake_psutil(rss, cpu)
                child = types.SimpleNamespace(pid=9876, poll=lambda: None)
                stopped = mock.Mock()
                times = iter((1.0, 2.0, 2.0))
                result = ci.supervise(["FAKE_PYTHON"], repository=Path("."), environment={}, deadline=deadline,
                                      rss_bytes=10, cpu_seconds=10, psutil=module, popen=lambda *_args, **_kwargs: child,
                                      clock=lambda: next(times), sleep=lambda _delay: None, stopper=stopped)
                self.assertEqual(result["reason"], expected)
                self.assertEqual(result["status"], "failed")
                stopped.assert_called_once()

    def test_exited_descendant_cpu_remains_in_cumulative_budget(self):
        module, parent = self.fake_psutil(cpu=1)
        child = types.SimpleNamespace(pid=9877, create_time=lambda: 2.0, memory_info=lambda: types.SimpleNamespace(rss=3),
                                      cpu_times=lambda: types.SimpleNamespace(user=4.0, system=0.0))
        parent.children = lambda **_kwargs: [child]
        usage = ci.TreeUsage(module, parent.pid)
        self.assertEqual(usage.sample(), (4, 5.0))
        parent.children = lambda **_kwargs: []
        parent.cpu_times = lambda: types.SimpleNamespace(user=2.0, system=0.0)
        self.assertEqual(usage.sample(), (1, 6.0))

    def test_phase_failure_preserves_sanitized_artifact_and_unrun_conditions(self):
        with tempfile.TemporaryDirectory(prefix="mesh-retrieval-ci-contract-") as temporary, mock.patch.dict(os.environ, {}, clear=True):
            repository = Path(temporary)
            benchmark = repository / "benchmarks"
            benchmark.mkdir()
            for name in ci.HARNESS_FILES:
                (benchmark / name).write_text("# fake stdlib script\n", encoding="utf-8")
            root = repository / ".work/run"
            root.mkdir(parents=True)
            args = argparse.Namespace(dataset="prospective", qualification_directory=root, floor_qualification_directory=root,
                                      model_directory=repository / ".work/models", wall_seconds=10, rss_mib=64, cpu_seconds=10)
            with mock.patch.object(ci, "hashes", return_value={"mesh/store.py": "a" * 64}), \
                    mock.patch.object(ci, "qualify", return_value={"same_runtime_sources": True}), \
                    mock.patch.object(ci, "supervise", side_effect=ValueError("FAKE C:/Users/EXAMPLE SECRET_VALUE")), \
                    mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(ci.run(args, repository, root), 1)
            text = (root / "job-summary.json").read_text(encoding="utf-8")
            report = json.loads(text)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["conditions"], {})
            self.assertTrue(any(phase["status"] == "not_run" for phase in report["phases"]))
            self.assertNotIn("SECRET_VALUE", text)
            self.assertNotIn("C:/Users", text)


class QualityContracts(unittest.TestCase):
    @staticmethod
    def conditions(candidate=1.0, original=1.0, fresh=1.0):
        def metrics(value):
            return {"unconditional": {name: {"mean": value} for name in ("hit_at_1", "ndcg_at_10", "document_recall_at_10")}}
        return {"replay": {"exact_rank_continuity": True}, "passages": {"metrics": {
            "results-C64": {"final_sdk_top10": metrics(candidate), "original_sdk_top10": metrics(original)},
            "results-C96": {"final_sdk_top10": metrics(candidate), "original_sdk_top10": metrics(original)}}},
                "fresh-default": {"metrics": {"results-default": {"final_sdk_top10": metrics(fresh)}}}}

    def test_completed_scoring_cannot_hide_direct_or_positional_failures(self):
        for dataset in ci.QUALITY_TARGETS:
            gates = ci.quality_gates(dataset, self.conditions(candidate=0.5, fresh=0.5))
            self.assertFalse(all(gate["passed"] for gate in gates))

    def test_exact_turn_lme_gate_cannot_be_replaced_by_perfect_session_hits(self):
        conditions = self.conditions(candidate=0.5, fresh=0.5)
        conditions["passages"]["metrics"]["results-C64"]["gold_session_hits"] = {"hit_at_1": {"mean": 1.0}}
        self.assertFalse(all(gate["passed"] for gate in ci.quality_gates("longmemeval", conditions)))

    def test_paired_public_quality_tolerance_has_a_meaningful_boundary(self):
        self.assertTrue(all(gate["passed"] for gate in ci.quality_gates("scifact", self.conditions(candidate=0.39, original=0.4))))
        self.assertFalse(all(gate["passed"] for gate in ci.quality_gates("scifact", self.conditions(candidate=0.389, original=0.4))))

    def test_counter_projection_rejects_strings_and_unknown_secret_keys(self):
        for value in ({"submitted": "C:/Users/EXAMPLE"}, {"YOUR_TOKEN_HERE": 1}, {"accepted": float("nan")}):
            with self.subTest(value=value), self.assertRaises(ci.GateError):
                ci.counters(value)

    def test_optional_c4_missing_measurements_cannot_pass_resource_gates(self):
        self.assertFalse(all(gate["passed"] for gate in ci.c4_resource_gates({})))
        reported = {name: {"observed": maximum, "maximum": maximum} for name, maximum in ci.C4_RESOURCE_LIMITS.items()}
        self.assertTrue(all(gate["passed"] for gate in ci.c4_resource_gates({"resource_gates": reported})))
        reported["delta_rss_mib"]["maximum"] = 1024
        self.assertFalse(all(gate["passed"] for gate in ci.c4_resource_gates({"resource_gates": reported})))

    def test_bundled_fixtures_preserve_counts_hashes_and_all_exact_gold_ids(self):
        expected = {"prospective": (50, 22), "longtext-prospective": (15, 9), "longmemeval": (191, 6)}
        for name, (corpus_count, query_count) in expected.items():
            directory = BENCHMARKS / "retrieval_fixtures" / name
            manifest = production.verify_manifest(directory / "manifest.json")
            self.assertEqual(manifest["inspection_status"], "previously_inspected")
            self.assertNotIn("gold_turn_ids", json.dumps(manifest))
            self.assertNotIn("exact_gold_annotations", manifest)
            self.assertNotIn("needle_positions", manifest)
            queries = list(replay.read_jsonl(directory / "queries-selected.jsonl"))
            corpus_names = {query.get("corpus", "corpus.jsonl") for query in queries}
            rows = [row for filename in corpus_names for row in replay.read_jsonl(directory / filename)]
            labels = json.loads((directory / "labels.json").read_text(encoding="utf-8"))
            self.assertEqual((len(rows), len(queries)), (corpus_count, query_count))
            self.assertEqual(len({query["id"] for query in queries}), query_count)
            identifiers = {row["id"] for row in rows}
            self.assertTrue(all(set(golds).issubset(identifiers) for golds in labels["qrels"].values()))


class C4QualityContracts(unittest.TestCase):
    @staticmethod
    def reports(n=4, value=0.5):
        def interval(mean):
            return {"mean": mean, "ci95": [mean, mean], "n": n}

        cells, baseline = {}, {}
        for cell in ("results-C64", "results-C96"):
            metrics = {name: interval(value) for name in ci.C4_PAIRED_METRICS}
            cohorts = {name: copy.deepcopy(metrics) for name in ("unconditional", "any_gold_survived", "all_gold_survived", "retained_gold_denominator")}
            cells[cell] = {"final_sdk_top10": copy.deepcopy(cohorts), "original_sdk_top10": copy.deepcopy(cohorts),
                           "candidate_minus_original_sdk": {name: interval(0.0) for name in ci.C4_PAIRED_METRICS},
                           "candidate_hit_at_1_regressions": {"answered_queries": n, "baseline_hits": round(n * value),
                                                             "candidate_hits": round(n * value), "lost_baseline_hits": 0, "gained_hits": 0}}
            baseline[cell] = {"final_sdk_top10": copy.deepcopy(cohorts)}
        return ({"metrics": cells, "gold_survival": {"answered_queries": n}},
                {"metrics": baseline, "gold_survival": {"answered_queries": n}})

    def test_complete_matched_zero_loss_reports_pass_each_cap(self):
        candidate, baseline = self.reports()
        self.assertTrue(all(gate["passed"] for gate in ci.c4_quality_gates("prospective", candidate, baseline)))

    def test_every_c4_metric_rejects_losses_below_core_tolerance(self):
        for cap in ("results-C64", "results-C96"):
            for metric in ci.C4_PAIRED_METRICS:
                candidate, baseline = self.reports(n=1000)
                report = candidate["metrics"][cap]
                report["candidate_minus_original_sdk"][metric]["mean"] = -0.001
                report["final_sdk_top10"]["unconditional"][metric]["mean"] = 0.499
                gates = ci.c4_quality_gates("scifact", candidate, baseline)
                gate = next(gate for gate in gates if gate["name"] == "c4_paired_zero_loss_" + cap + "_" + metric)
                with self.subTest(cap=cap, metric=metric):
                    self.assertFalse(gate["passed"])
                    self.assertEqual(gate["observed_delta"], -0.001)

    def test_missing_paired_reports_metrics_or_count_report_fail(self):
        for missing in ("candidate_minus_original_sdk", "candidate_hit_at_1_regressions", "ndcg_at_10", "ci95"):
            candidate, baseline = self.reports()
            report = candidate["metrics"]["results-C96"]
            if missing in report:
                report.pop(missing)
            elif missing == "ndcg_at_10":
                report["candidate_minus_original_sdk"].pop(missing)
            else:
                report["candidate_minus_original_sdk"]["hit_at_1"].pop(missing)
            with self.subTest(missing=missing):
                self.assertFalse(all(gate["passed"] for gate in ci.c4_quality_gates("scifact", candidate, baseline)))

    def test_survivor_denominators_and_nonfinite_or_boolean_metrics_cannot_qualify(self):
        for invalid in (float("nan"), float("inf"), True, "FAKE_BODY"):
            candidate, baseline = self.reports()
            candidate["metrics"]["results-C64"]["candidate_minus_original_sdk"]["mrr_at_10"]["mean"] = invalid
            with self.subTest(invalid=invalid):
                gates = ci.c4_quality_gates("scifact", candidate, baseline)
                self.assertFalse(all(gate["passed"] for gate in gates))
                self.assertNotIn("FAKE_BODY", json.dumps(gates))
        for report_name in ("candidate_minus_original_sdk", "final_sdk_top10", "original_sdk_top10"):
            candidate, baseline = self.reports()
            report = candidate["metrics"]["results-C64"][report_name]
            (report if report_name == "candidate_minus_original_sdk" else report["unconditional"])["hit_at_5"]["n"] = 2
            with self.subTest(report=report_name):
                self.assertFalse(all(gate["passed"] for gate in ci.c4_quality_gates("scifact", candidate, baseline)))

    def test_gain_cannot_hide_a_new_bounded_exact_evidence_regression(self):
        candidate, baseline = self.reports(n=20, value=0.95)
        for report in candidate["metrics"].values():
            report["candidate_hit_at_1_regressions"].update(lost_baseline_hits=1, gained_hits=1)
        self.assertTrue(all(gate["passed"] for gate in ci.c4_quality_gates("scifact", candidate, baseline)))
        for dataset in ci.QUALITY_TARGETS:
            with self.subTest(dataset=dataset):
                self.assertFalse(all(gate["passed"] for gate in ci.c4_quality_gates(dataset, candidate, baseline)))

    def test_hit_count_balances_and_types_are_checked(self):
        for key, value in (("answered_queries", 3), ("candidate_hits", 3), ("lost_baseline_hits", 3),
                           ("gained_hits", 3), ("gained_hits", True), ("baseline_hits", 2.0)):
            candidate, baseline = self.reports()
            candidate["metrics"]["results-C64"]["candidate_hit_at_1_regressions"][key] = value
            with self.subTest(key=key, value=value):
                self.assertFalse(all(gate["passed"] for gate in ci.c4_quality_gates("scifact", candidate, baseline)))

    def test_c4_original_metrics_must_match_its_actual_passage_baseline(self):
        candidate, baseline = self.reports(n=20)
        report = candidate["metrics"]["results-C64"]
        report["original_sdk_top10"]["unconditional"]["hit_at_1"]["mean"] = 0.4
        report["candidate_minus_original_sdk"]["hit_at_1"]["mean"] = 0.1
        report["candidate_hit_at_1_regressions"].update(baseline_hits=8, gained_hits=2)
        self.assertFalse(all(gate["passed"] for gate in ci.c4_quality_gates("scifact", candidate, baseline)))

    def test_projection_preserves_only_content_free_paired_metrics_and_counts(self):
        candidate, _baseline = self.reports()
        report = candidate["metrics"]["results-C64"]
        report["candidate_hit_at_1_regressions"]["content"] = "FAKE_BODY"
        report["candidate_minus_original_sdk"]["unknown"] = "FAKE_BODY"
        projected = ci.metric_projection(candidate["metrics"])
        self.assertEqual(set(projected["results-C64"]["candidate_minus_original_sdk"]), set(ci.C4_PAIRED_METRICS))
        self.assertEqual(set(projected["results-C64"]["candidate_hit_at_1_regressions"]), set(ci.C4_HIT_COUNT_FIELDS))
        self.assertNotIn("FAKE_BODY", json.dumps(projected))


if __name__ == "__main__":
    unittest.main()
