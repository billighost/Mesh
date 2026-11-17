# Inspected regression fixtures

These are frozen, small regression inputs. Corpus and label bytes match the
earlier direct, positional and inspected LongMemEval evaluations. The portable
manifests have new hashes because they omit gold IDs and annotations from
metadata opened by ranking. Labels remain in `labels.json` for offline scoring.

| Fixture | Corpus documents | All queries | Answered queries |
| --- | ---: | ---: | ---: |
| `prospective` | 50 | 22 | 16 |
| `longtext-prospective` | 15 | 9 | 6 |
| `longmemeval` | 191 selected turns | 6 | 6 |

The first two fixtures are Mesh synthetic fact regressions. They include
answerless lexical guides, conflicting environments or values, direct
affirmative and negative facts, and early/middle/late evidence positions.
Their filenames preserve the original prospective preparation names; they
have since been inspected and scored.

The LongMemEval inputs derive from
[LongMemEval](https://github.com/xiaowu0162/LongMemEval), using the first six
questions from the previously inspected single-session-user export. Each
frozen corpus is the union of 16 evenly spaced original turns, 16 turns with
literal query overlap, and the exact annotated gold turns, sorted by original
position. This gold-inclusive subset is an inspected hard-negative regression.
It does not represent full LongMemEval accuracy. The manifest retains the source
dataset and selected-query fingerprints, and the original corpus and label
fingerprints. Exact evidence-turn hits and gold-session hits have separate
denominators; a session hit cannot satisfy an exact-turn gate.

No new score selected these fixture members. No fixture is evidence of broad
quality, competitor advantage or statistical superiority. Answerless cases
measure candidate emission; these checks do not generate answers.
