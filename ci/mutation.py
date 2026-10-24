"""A mutation sweep for gauge.py.

Every mutant is a one-line change that breaks a specific invariant or check.
The sweep applies each one to a *scratch copy* of the tool (never the working
tree), compiles it, and requires the selftest to notice with a named check
failure. If a mutant survives, the selftest has a hole.

The sweep is canary-guarded: a semantically-identical edit must SURVIVE, which
proves the harness is testing behaviour and not merely diffing text.

    python ci/mutation.py
"""
from __future__ import annotations

import hashlib
import pathlib
import py_compile
import shutil
import subprocess
import sys
import tempfile

LIVE = pathlib.Path("gauge.py")

# (label, search, replace) -- each must make the selftest FAIL with a name.
MUTANTS = [
    ("a title directive is unquoted",
     'title = s[len("title "):].strip().strip(\'"\')',
     'title = s[len("title "):].strip()'),
    ("a duplicate unit is reported",
     '        if name in registry:',
     '        if False:'),
    ("a document with no queries is reported",
     '    if not document["queries"]:',
     '    if False:'),
    ("a non-numeric value is reported",
     '    except ValueError:\n'
     '        raise BadNumber("not a number: %r" % text)',
     '    except ValueError:\n'
     '        return 0.0'),
    ("a dimension mismatch is reported",
     '            if lhs_dims != rhs_dims:',
     '            if False:'),
    ("a failing check is reported",
     '            if not ok:',
     '            if False:'),
    ("an unknown unit is reported",
     '            raise UnknownUnit(name)',
     '            raise BadExpression(name)'),
    ("a malformed expression is reported",
     '        try:\n'
     '            exponent = int(exp_text)\n'
     '        except ValueError:\n'
     '            raise BadExpression("bad exponent %r" % exp_text)',
     '        exponent = 1'),
    ("a derived expression has the dimension of force",
     '            dims[key] = dims.get(key, 0) + value * power',
     '            dims[key] = dims.get(key, 0) + value'),
    ("a compound unit converts",
     '        factor *= unit_factor ** power',
     '        factor *= unit_factor ** abs(power)'),
    ("miles convert to kilometres",
     '                "result": base / rhs_factor,',
     '                "result": base,'),
    ("a clean seal chain verifies",
     '    rec["hash"] = record_hash(rec)',
     '    rec["hash"] = "x"'),
    ("the second seal links to the first hash",
     '    rec["prev"] = prev',
     '    rec["prev"] = GENESIS'),
]

# A behaviour-preserving edit. The selftest must still pass.
CANARY = ("drop a return annotation",
          "def canonical(obj) -> str:",
          "def canonical(obj):")


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_selftest(path: pathlib.Path):
    proc = subprocess.run([sys.executable, str(path), "selftest"],
                          capture_output=True, text=True, timeout=600)
    return proc.returncode, proc.stdout + proc.stderr


def named_check_failure(output: str):
    for line in output.splitlines():
        if line.startswith("  - "):
            return line[4:].strip()
    return None


def mutate_into(scratch_dir: pathlib.Path, label, old, new):
    with open(LIVE, encoding="utf-8", newline="") as fh:
        src = fh.read()
    if old not in src:
        print("  [ERROR] pattern not found for: %s" % label)
        return None
    target = scratch_dir / "gauge_mutant.py"
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(src.replace(old, new, 1))
    try:
        py_compile.compile(str(target), doraise=True)
    except py_compile.PyCompileError as exc:
        print("  [ERROR] mutant does not compile: %s (%s)" % (label, exc))
        return None
    return target


def main() -> int:
    before = digest(LIVE)
    killed = 0
    total = 0
    failed = False

    workdir = pathlib.Path(tempfile.mkdtemp(prefix="gauge-mut-"))
    try:
        for label, old, new in MUTANTS:
            total += 1
            target = mutate_into(workdir, label, old, new)
            if target is None:
                failed = True
                continue
            rc, out = run_selftest(target)
            if rc != 0:
                check = named_check_failure(out)
                if check is None:
                    print("  [ERROR] %s: selftest failed without a named "
                          "check (a crash is not a report)" % label)
                    failed = True
                else:
                    killed += 1
                    print("  [KILLED] %s  <- %s" % (label, check))
            else:
                print("  [ALIVE ] %s  <-- the selftest missed this" % label)
                failed = True

        # The canary must survive: a change with no behavioural effect.
        label, old, new = CANARY
        target = mutate_into(workdir, label, old, new)
        if target is None:
            failed = True
        else:
            rc, out = run_selftest(target)
            if rc == 0:
                print("  [SURVIVED] canary: %s" % label)
            else:
                print("  [DIED   ] canary: %s  <-- harness is not sound" % label)
                failed = True
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    after = digest(LIVE)
    if before != after:
        print("ERROR: the live gauge.py changed during the sweep")
        failed = True

    print("%d/%d real mutants killed, canary survived" % (killed, total))
    if failed or killed != total:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
