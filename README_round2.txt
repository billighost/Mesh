Mesh round 2 - unzip into the repo root (the folder that contains mesh/), then run:

    python build_and_verify.py                    # patches -> gcc (64-bit) -> parity/fuzz -> pytest -> benchmark
    python build_and_verify.py --bench-realistic  # additionally: persistent ASGI portal + separate-process daemon

Only the patch (safe to run any number of times, any order with apply_mesh_patches.py):
    python apply_round2.py [--check | --revert]       (backups: *.pre_round2; CRLF preserved)

What is in here
  apply_round2.py            49 exact-text edits (store/memory/crypto/http_server/cli/io/native_cli/benchmark)
  c_cli/mesh_fast.c          native client: --version, and `ask ... --json` via verified daemon forwarding
  build_and_verify.py        runs both patchers, builds C (core + client), verifies, pytest, benchmark
  tests/                     test_native_cli_golden, test_round2_http_gate, test_round2_stage2_worker,
                             test_patchers_idempotent, test_round2_init_lock_retry, test_round2_fs_memo
  make_bundles.py            unchanged behaviour (skips the new report files)
  BEFORE_AFTER.md            per-operation table (sandbox numbers)

Environment knobs added
  MESH_BG_ONNX=0                    disable the stage-2 background ONNX worker
  MESH_BG_ONNX_IDLE_MS=150          worker stays idle until no foreground embed ran for this long
  MESH_BG_ONNX_DRAIN_ON_EXIT=auto|1|0   drain at close()/exit: auto = only if the model is already loaded
  MESH_BG_ONNX_DRAIN_TIMEOUT=10     seconds
  MESH_MODEL_PAIR_RECHECK_SECONDS=1 re-stat a found model pair at most this often (0 = always)
  MESH_FS_MEMO_MS=10                a path/marker verified this recently is not re-checked inside the same operation (0 = always re-check)
  MESH_QUIET=1                      silence status lines (benchmark sets it)
  MESH_NATIVE_TRACE=1               native client prints why it fell back
  MESH_NATIVE_DISABLE=1             native client always hands over to Python
  MESH_PYTHON=<path>                interpreter the native client falls back to
Benchmark options: --persistent-asgi  --separate-server

Measuring on your own machine (this is the next step):
    python profile_windows.py            # 3-6 minutes -> writes mesh_profile_report.txt (send it back)
    python profile_windows.py --quick    # 1-2 minutes
    python profile_windows.py --home D:\meshprof   # same, with the throwaway data on another folder/drive
It uses a throwaway data folder, never your real ~/.mesh.
