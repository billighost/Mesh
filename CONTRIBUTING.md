# Contributing to Mesh

Thanks for helping improve Mesh. Mesh is a Beta project (`mesh-context-layer`, currently `0.8.1b2`), so behavior and interfaces can change between minor versions. Contributions are welcome as bug reports, documentation fixes, tests, and focused pull requests.

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- **Security issues:** do not open a public issue. Follow [SECURITY.md](SECURITY.md).
- **Usage questions and setup problems:** open a GitHub issue using the templates in `.github/ISSUE_TEMPLATE/` and include your Mesh version (`mesh version`), OS, Python version, and the exact command or tool call.
- **Large features or behavior changes:** open an issue first so the design and privacy implications can be discussed before you write code. Changes that affect agent behavior, the rules block, or the MCP tool surface need extra review.

## Development setup

The full setup, isolated test environment, and extension guides are in [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md). In short:

```bash
git clone https://github.com/billighost/mesh.git
cd mesh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -e ".[test]"
```

Always test with a temporary `MESH_HOME`, temporary home/config directories, and `MESH_NO_AUTO_DAEMON=1`. The exact PowerShell and POSIX recipes are in [DEVELOPER_GUIDE.md §2](DEVELOPER_GUIDE.md#2-isolated-development-environment). Never point a test at your real `~/.mesh` or your real agent configuration, and never call a cloud provider from a test.

## What to run before opening a pull request

```bash
ruff check .
python scripts/check_skill.py
python scripts/check_docs.py
pytest -q
```

CI runs these on Ubuntu, macOS, and Windows with Python 3.10–3.13. A pull request should not weaken an existing check to make it pass; fix the underlying inconsistency.

## Pull request expectations

- Keep the change focused. Separate refactors from behavior changes and from documentation updates where practical.
- Add or update tests for behavior changes, including privacy, namespace, and error paths.
- Update the user-facing documentation when behavior changes: [DOCS.md](DOCS.md), [README.md](README.md) when the change is prominent, and [CHANGELOG.md](CHANGELOG.md) under `Unreleased`.
- Do not edit `mesh/skills/mesh-context-layer/SKILL.md`, its `references/`, or `rules_block.md` to make a documentation check pass. Those files define agent behavior; report a discrepancy instead and change them deliberately, with the installer and README rules block updated in the same commit.
- Do not include secrets, tokens, personal paths, real memory contents, or raw audit logs in code, tests, issues, or pull requests. Use obviously fake placeholders.
- Do not add a new network destination or a cloud call without an explicit consent gate, documentation, and tests that prove the default path stays local.
- Fill in the repository pull request template.

Commit messages should be short and descriptive. Maintainers may squash commits when merging.

## Documentation changes

Documentation is part of the product, especially for AI agents that read [README.md](README.md) first. Follow the source-of-truth files and the facts in the code:

- Describe only behavior that exists on the current branch. Mark planned work as **Roadmap (not implemented)**.
- Separate measured numbers from estimates. Quote performance only from `benchmarks/RESULTS.md` or the field-test archive, and state the tested version, hardware, and method next to the number.
- Keep `scripts/check_docs.py` green. If it reports a false positive, narrow the checker rather than deleting a real reference.
- Relative links and anchors must resolve.

## Reporting a bug

Include:

1. `mesh version`, OS and version, Python version, and install method (PyPI wheel or source).
2. Whether the ONNX model or the `hash-384` fallback was active (`mesh doctor`).
3. The exact command or MCP tool call, expected behavior, and actual behavior.
4. Relevant logs with tokens, memory contents, and personal paths removed.

If the bug involves a secret being stored or echoed, say so explicitly in the issue without reproducing the secret value. See the open field-test issue T-002 in [docs/field-test/issues.md](docs/field-test/issues.md).

## License

By contributing, you agree that your contribution is licensed under the project's [MIT License](LICENSE).
