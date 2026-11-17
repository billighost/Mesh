# Review and Validation Record

## Human code and security review

The repository does not currently record signed human code or security review
reports for Mesh 0.9.0, with reviewed commits, scope, findings and consent to name
the reviewers. Human review is therefore unverified from the published record.
Automated qualification and coding-agent reviews do not establish human signoff.

## Source-bound qualification and agent reviews

The [qualification record](CI_VALIDATION.md) and
[benchmark evidence](../benchmarks/RESULTS.md) identify the tested source,
environments and retained failures. Normal CI and both full qualification suites
passed at `b55c980`; subsequent changes need their own qualification. Experimental
retrieval profiles remain default off and unqualified across the tested datasets.

The [production plan](PRODUCTION_PLAN.md) records source-review work by the primary
coding agent and specialist coding agents. Those reviews informed implementation
and regression checks; they are agent reviews, with the source and scope stated
in the plan. They are not external human security audits.

The twenty-cell native comparison at `2db6229` received separate coding-agent artifact-integrity and numerical review: twenty original ZIPs, 52 Git sources, fixed inputs, official environments and accounting were checked, then separate standard-library arithmetic recomputed graded quality, session metrics and latency from closed exports. [Comparison provenance](../benchmarks/results/comparison_20261117_2db6229_provenance.json) retains the exact review/source digests and scope. This review does not establish human signoff or an independent held-out dataset evaluation.

## Historical independent field test

The [field-test archive](field-test/README.md) reports an independent black-box
evaluation of Mesh **0.8.1b2**, dated October 20, 2026. The archive retains its
tested package hashes, reported findings, reproduction materials and limitations,
including the small sample and simulated agent-behavior probe. It remains
historical evidence for that version and does not qualify Mesh 0.9.0.
