# gauge

Provable, plain-text unit conversion and dimensional analysis. One file, Python 3.9+, standard library only.

You keep a measurement document as plain text: an optional title, any units you want to define, and the conversions and equivalence checks you care about. `gauge` parses it, resolves every unit into base dimensions, refuses unknown units and dimension mismatches, converts values exactly, checks that two quantities agree within a declared tolerance, draws a self-contained HTML report, and can *seal* the document — recording its hash, a digest of the results, and the counts in a hash-chained log so a calculation cannot be quietly rewritten.

## Quick start

```bash
python gauge.py selftest
python gauge.py check    measurement.gauge
python gauge.py convert  measurement.gauge
python gauge.py render   measurement.gauge --out report.html
python gauge.py seal     measurement.gauge --note "beam check"
python gauge.py history
python gauge.py verify
```

## Document grammar

```text
# comment
; comment
blank line

title "Beam check"
tolerance 1e-9

unit furlong = 201.168 m

convert 5 km to m
convert 100 km/h to m/s
check   1 N = 1 kg*m/s^2
```

- `title "..."` — optional report title
- `tolerance NUMBER` — relative tolerance for checks (default `1e-9`)
- `unit NAME = SCALE EXPRESSION` — define a unit as a scale factor times an expression
- `convert VALUE UNIT to UNIT` — convert between compatible units
- `check VALUE UNIT = VALUE UNIT` — assert two quantities agree

Unit expressions are products and quotients of unit names, with optional integer
exponents: `kg*m/s^2`, `m^3`, `km/h`. Keywords are case-insensitive; unit names
are case-sensitive. Only multiplicative units are supported, so temperatures use
kelvin — `degC` and other affine scales are deliberately not included.

## Built-in units

| Kind | Units |
|---|---|
| length | `m`, `km`, `cm`, `mm`, `in`, `ft`, `yd`, `mi` |
| mass | `kg`, `g`, `mg`, `t`, `lb`, `oz` |
| time | `s`, `ms`, `min`, `h`, `day` |
| base | `A`, `K`, `mol`, `cd`, `1` (dimensionless) |
| derived | `N`, `J`, `W`, `Pa`, `Hz`, `C`, `V`, `ohm`, `L` |

## Validation (fail-closed)

| Rule | Meaning |
|---|---|
| `no-queries` | No conversions or checks declared |
| `duplicate-unit` | A unit redefines a built-in or an earlier unit |
| `unknown-unit` | A unit is used but never defined |
| `bad-expression` | A unit expression is malformed |
| `bad-number` | A value is not a finite number |
| `incompatible-units` | A conversion/check mixes different dimensions |
| `mismatch` | A check fails outside the tolerance |

All findings are `error` severity. `check`/`convert` exit `1` if any findings
exist, and `seal` refuses to record a document with findings. A syntactically
malformed line exits `2`.

## Exit codes

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `check` | Clean | Findings | Parse/usage/missing |
| `convert` | Clean results | Findings | Parse/usage/missing |
| `render` | Always | — | Parse/usage/missing |
| `seal` | Sealed | Findings/refused | Parse/usage/missing |
| `history`/`verify` | OK | Chain broken (verify) | Usage/store error |
| `selftest` | All passed | Failures | — |

## Seal ledger

Seals are recorded in a hash-chained JSONL ledger:

- Default: `~/.gauge/ledger.jsonl`
- Override: `GAUGE_STORE` env var or `--store`
- Deterministic timestamps: pass `--now ISO8601`

Each record links to the previous record's hash (`prev`), includes the document
hash, a canonical digest of the computed results, the unit/conversion/check
counts, and a `hash` over the canonicalized record. `verify` detects edits,
deletions, and reordering.

## Gates (CI)

- `ci/import_gate.py` — stdlib-only imports (local module exempt)
- `ci/check_counts.py` — enforces `selftest >= 68`, `e2e >= 44`
- `ci/prove_suites_can_fail.py` — proves suites fail on negative controls
- `ci/mutation.py` — mutation sweep; requires named check failures, kills real mutants, canary must survive
- 5-cell CI matrix + gate job

## Files

- `gauge.py` — single-file implementation
- `tests/test_gauge.py` — e2e tests (subprocess)
- `ci/*.py` — gates
- `.github/workflows/ci.yml` — CI
- `examples/beam.gauge` — worked example
- `REPORT.md` — design notes and verification

## License

MIT (c) 2026 Hema
