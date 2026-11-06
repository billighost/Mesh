# Mesh Troubleshooting & Operations (v0.9.0)

## Missing tools or failed transport

Distinguish unavailable MCP transport from a rejected operation or weak search.
Follow [the verified fallback sequence](transport_fallbacks.md) before reporting
Mesh unavailable. Do not retry in a loop or change stores to hide the failure.
Use `mesh doctor` for SQLite/FTS, storage, embeddings, installed agent
configuration, and daemon diagnostics. Run it in the intended environment.

If HTTP reads work but reviewed writes report unsupported receipt capability,
check that the equivalent server advertises signed `operation_receipt_version=1`
through `/identity`. Use a compatible installed runtime; do not bypass the check
with a bare REST mutation or another store. A write that already dispatched and
then lost its receipt is unknown, rather than unavailable. Preserve the original
operation ID and follow the fallback guide's reconciliation rules.

## Weak natural-language recall

1. Confirm the active namespace, data root, and privacy scope. A valid empty
   namespace or a hidden private/quarantined item is not a transport outage.
2. Use a natural-language subject plus a distinctive service, command, file,
   configuration key, or identifier. Read actual result content and IDs.
3. Make one focused follow-up with exact identifiers or synonyms. Browse
   `mesh_inspect` with a type/tag filter if search still misses known facts.
4. Check whether current facts supersede old versions and whether expiry,
   confidence, or quarantine explains missing content. Preserve those controls.
5. Check the actual embedding backend with diagnostics. Hash fallback is useful
   offline but has limited semantic understanding. Missing local model files
   and deliberate offline settings do not justify an unapproved download.

When an answer remains uncertain, use current repository evidence and state the
gap. Do not invent facts or treat weak similarity scores as proof.

## Brief omissions and continuation

A startup brief requests up to 20 useful current facts within an explicit
estimated budget. Inspect fact counts, selected IDs, omission notices,
`next_fact_offset`, and `selection_revision`. Retrieve more only when useful.
The Markdown token estimate does not cover arbitrary transport framing; hooks
budget their own complete serialized output. A tiny budget may include only
notices rather than complete facts.
If the continuation offset does not advance, do not repeat that page indefinitely;
use a larger justified budget or targeted recall.

## Namespace suggestions

Preserve existing case and punctuation in namespace markers and profiles.
Names match ASCII letters/digits plus dots, hyphens, and underscores, start
with a letter/digit, and are at most 64 characters. Path separators and `..`
are rejected.

A nonexistent namespace may produce `NamespaceNotFoundError` or
`did_you_mean` suggestions. Verify the intended project before writes.
An empty result is insufficient evidence that a new namespace should be made.
Do not fall back to `shared` or rewrite the project's existing marker.

## Contradictions and quarantine

If a new fact conflicts with an old one, inspect `mesh_contradictions` and
compare evidence. Update or supersede the outdated fact, then resolve the
contradiction with `mesh_resolve_contradiction`. Do not blindly delete another
agent's fact or accept a new value only because it was written most recently.

Quarantined entries are hidden from ordinary recall and context. Review them
with `mesh_inspect(quarantined_only=true)`; use `mesh_approve` only for
content the user has authorized and whose evidence has been checked.
Do not disable quarantine or private-access limits to improve recall scores.

## Startup hooks

Project hooks use an encoded profile pointer rather than shell-interpolated
namespace or store paths. User hooks discover the nearest project profile and
stop at an inner Git repository boundary. Missing or rejected profiles inject
no context. Hooks use public reads, a bounded output budget, and no automatic
daemon, model download, or cloud path.
