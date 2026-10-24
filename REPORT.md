# REPORT: gauge

## Goal
Ship a sixth non-security tool `gauge` (provable, plain-text unit conversion and
dimensional analysis) to the same family bar as `calibra`, `argue`, `slate`,
`tally`, and `loom`, published as a public repository under `happymode-25`.

## Design
- **Single file**: `gauge.py`, Python 3.9+, standard library only.
- **Model**: every unit is `(factor_to_SI, {base_dimension: integer_exponent})`.
  Dimensions are named (`length`, `mass`, `time`, `current`, `temperature`,
  `amount`, `luminous`) so a dimension string never looks like a unit.
- **Grammar**: `title "..."`, `tolerance NUMBER`, `unit NAME = SCALE EXPRESSION`,
  `convert VALUE UNIT to UNIT`, `check VALUE UNIT = VALUE UNIT`. Comments `#`/`;`.
  Unit expressions support `*`, `/`, and integer `^` exponents, evaluated
  left-to-right with no parentheses.
- **Validation (fail-closed)**: `no-queries`, `duplicate-unit`, `unknown-unit`,
  `bad-expression`, `bad-number`, `incompatible-units`, `mismatch`. All `error`.
- **Computation**: convert via base units (`value * lhs_factor / rhs_factor`);
  check with a relative tolerance (`|a-b| <= tol * max(1, |a|, |b|)`).
- **Rendering**: self-contained HTML table of conversions/checks, no external
  URLs or scripts. A broken document still renders, with findings.
- **Seal ledger**: hash-chained JSONL at `~/.gauge/ledger.jsonl` (env
  `GAUGE_STORE`, `--store`, `--now`). Records the document hash, a canonical
  digest of the results, and the unit/conversion/check counts. `verify` checks
  seq/prev/hash.
- **Exit codes**: `0` clean/sealed; `1` findings/refused; `2` parse/usage/missing.
  `render` always exits `0`.

## Scope decision: multiplicative units only
Affine units (Celsius, Fahrenheit) require an offset as well as a factor. Rather
than pretend, `gauge` supports only multiplicative units and documents that
temperatures use kelvin. This keeps conversion a pure ratio and makes every
result reproducible from the declared units alone.

## Verification
- **Selftest**: 68 checks — all pass.
- **E2E tests**: 44 tests (`tests/test_gauge.py`) — all pass.
- **Gates**: `import_gate` OK (stdlib only); `check_counts` OK (68/44 floors
  met); `prove_suites_can_fail` OK (behavioural break and injected negative
  control both fail both suites as named checks); `mutation` 13/13 real mutants
  killed with named check failures, canary survived.
- **CLI smoke**: `check`/`convert`/`render`/`seal`/`history`/`verify`/`selftest`
  all behave as specified.

## Notes
- Distinct from the family: measurement calibration (`calibra`), arguments
  (`argue`), money (`slate`), votes (`tally`), scheduling/dependencies (`loom`),
  dimensional arithmetic (`gauge`).
- The selftest was hardened so a disappeared conversion reports a named failure
  instead of raising `KeyError`; a mutation sweep found that hole.
- `seal` refuses if any findings exist, and a parse error exits `2`.
