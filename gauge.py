#!/usr/bin/env python3
"""gauge -- provable, plain-text unit conversion and dimensional analysis.

You keep a measurement document as plain text: an optional title, any units you
want to define, and then the conversions and equivalence checks you care about.
gauge parses it, resolves every unit into base dimensions, refuses unknown units
and dimension mismatches, converts values exactly, checks two quantities agree
within a declared tolerance, draws a self-contained report, and can *seal* the
document -- recording the document's hash, a digest of the results, and the
counts in a hash-chained log so a calculation cannot be quietly rewritten.

Conversion is deterministic and fail-closed: a unit that is not defined, or a
conversion between different dimensions, is refused, never guessed. Only
multiplicative units are supported; temperatures use kelvin, never offsets.

Single file, Python 3.9+, standard library only.

    python gauge.py selftest
    python gauge.py check   measurements.gauge
    python gauge.py convert measurements.gauge
    python gauge.py render  measurements.gauge --out report.html
    python gauge.py seal    measurements.gauge --note "beam check"
    python gauge.py verify
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import tempfile
from datetime import datetime

TOOL = "gauge"
VERSION = "1.0.0"
GENESIS = "0" * 64

RULES = ("no-queries", "duplicate-unit", "unknown-unit", "bad-expression",
         "bad-number", "incompatible-units", "mismatch")

# Base dimensions are named, not lettered, so a dimension string never looks
# like a unit ("mass*length/time^2", not "M L T^-2").
LENGTH = {"length": 1}
MASS = {"mass": 1}
TIME = {"time": 1}
CURRENT = {"current": 1}
TEMPERATURE = {"temperature": 1}
AMOUNT = {"amount": 1}
LUMINOUS = {"luminous": 1}


def _d(**dims):
    return {k: v for k, v in dims.items() if v}


BUILTINS = {
    "1": (1.0, {}),

    "m": (1.0, LENGTH),
    "km": (1000.0, LENGTH),
    "cm": (0.01, LENGTH),
    "mm": (0.001, LENGTH),
    "in": (0.0254, LENGTH),
    "ft": (0.3048, LENGTH),
    "yd": (0.9144, LENGTH),
    "mi": (1609.344, LENGTH),

    "kg": (1.0, MASS),
    "g": (0.001, MASS),
    "mg": (1e-6, MASS),
    "t": (1000.0, MASS),
    "lb": (0.45359237, MASS),
    "oz": (0.028349523125, MASS),

    "s": (1.0, TIME),
    "ms": (0.001, TIME),
    "min": (60.0, TIME),
    "h": (3600.0, TIME),
    "day": (86400.0, TIME),

    "A": (1.0, CURRENT),
    "K": (1.0, TEMPERATURE),
    "mol": (1.0, AMOUNT),
    "cd": (1.0, LUMINOUS),

    "N": (1.0, _d(mass=1, length=1, time=-2)),
    "J": (1.0, _d(mass=1, length=2, time=-2)),
    "W": (1.0, _d(mass=1, length=2, time=-3)),
    "Pa": (1.0, _d(mass=1, length=-1, time=-2)),
    "Hz": (1.0, _d(time=-1)),
    "C": (1.0, _d(current=1, time=1)),
    "V": (1.0, _d(mass=1, length=2, time=-3, current=-1)),
    "ohm": (1.0, _d(mass=1, length=2, time=-3, current=-2)),
    "L": (0.001, _d(length=3)),
}


class GaugeError(Exception):
    """A refusal: bad syntax, bad store, bad record. Always exits non-zero."""


class UnitError(Exception):
    """A semantic unit problem, reported as a finding rather than a refusal."""


class UnknownUnit(UnitError):
    def __init__(self, name):
        super().__init__("unknown unit %r" % name)
        self.name = name


class BadExpression(UnitError):
    pass


class BadNumber(UnitError):
    pass


def fail(message: str):
    raise GaugeError(message)


# --------------------------------------------------------------------------
# canonical hashing (the family discipline)
# --------------------------------------------------------------------------

def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_hash(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return sha256_hex(canonical(body))


def now_iso(now=None) -> str:
    if now:
        return now
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# parsing (shape only; units and numbers are resolved during validation)
# --------------------------------------------------------------------------

TO_SPLIT = re.compile(r"\s+to\s+", re.IGNORECASE)
EQ_SPLIT = re.compile(r"\s*=\s*", re.IGNORECASE)


def parse_document(text: str) -> dict:
    if text.startswith("\ufeff"):
        text = text[1:]
    title = None
    tolerance = None
    units = []
    queries = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith(";"):
            continue
        low = s.lower()

        if low == "title":
            fail("line %d: title needs a value" % lineno)
        if low.startswith("title "):
            title = s[len("title "):].strip().strip('"')
            continue

        if low == "tolerance":
            fail("line %d: tolerance needs a value" % lineno)
        if low.startswith("tolerance "):
            raw_tol = s[len("tolerance "):].strip()
            try:
                value = float(raw_tol)
            except ValueError:
                fail("line %d: tolerance is not a number: %r" % (lineno, raw_tol))
            if math.isnan(value) or math.isinf(value) or value < 0:
                fail("line %d: tolerance must be a finite, non-negative number" % lineno)
            tolerance = value
            continue

        if low == "unit":
            fail("line %d: a unit needs 'NAME = SCALE EXPRESSION'" % lineno)
        if low.startswith("unit "):
            rest = s[len("unit "):].strip()
            if "=" not in rest:
                fail("line %d: a unit needs 'NAME = SCALE EXPRESSION'" % lineno)
            name, rhs = rest.split("=", 1)
            name = name.strip()
            rhs = rhs.strip()
            if not name or len(name.split()) != 1:
                fail("line %d: a unit name must be a single token" % lineno)
            pieces = rhs.split(None, 1)
            if len(pieces) != 2:
                fail("line %d: a unit needs a scale and an expression" % lineno)
            units.append({"name": name, "scale": pieces[0], "expr": pieces[1],
                          "line": lineno})
            continue

        if low == "convert":
            fail("line %d: convert needs 'VALUE UNIT to UNIT'" % lineno)
        if low.startswith("convert "):
            rest = s[len("convert "):].strip()
            parts = TO_SPLIT.split(rest, maxsplit=1)
            if len(parts) != 2:
                fail("line %d: convert needs 'VALUE UNIT to UNIT'" % lineno)
            lhs = parts[0].strip()
            to = parts[1].strip()
            lp = lhs.split(None, 1)
            if len(lp) != 2:
                fail("line %d: convert needs a value and a unit" % lineno)
            queries.append({"kind": "convert", "line": lineno, "text": s,
                            "value": lp[0], "unit": lp[1], "to": to})
            continue

        if low == "check":
            fail("line %d: check needs 'VALUE UNIT = VALUE UNIT'" % lineno)
        if low.startswith("check "):
            rest = s[len("check "):].strip()
            parts = EQ_SPLIT.split(rest, maxsplit=1)
            if len(parts) != 2:
                fail("line %d: check needs 'VALUE UNIT = VALUE UNIT'" % lineno)
            lhs = parts[0].strip()
            rhs = parts[1].strip()
            lp = lhs.split(None, 1)
            rp = rhs.split(None, 1)
            if len(lp) != 2 or len(rp) != 2:
                fail("line %d: check needs a value and a unit on each side" % lineno)
            queries.append({"kind": "check", "line": lineno, "text": s,
                            "value": lp[0], "unit": lp[1],
                            "rhs_value": rp[0], "rhs_unit": rp[1]})
            continue

        fail("line %d: not a directive: %r" % (lineno, s))

    return {"title": title, "tolerance": tolerance, "units": units,
            "queries": queries}


# --------------------------------------------------------------------------
# units and dimensions
# --------------------------------------------------------------------------

def parse_number(text: str) -> float:
    s = text.strip()
    if not s:
        raise BadNumber("empty number")
    try:
        value = float(s)
    except ValueError:
        raise BadNumber("not a number: %r" % text)
    if math.isnan(value) or math.isinf(value):
        raise BadNumber("not a finite number: %r" % text)
    return value


def _parse_term(term: str):
    if "^" in term:
        name, _, exp_text = term.partition("^")
        try:
            exponent = int(exp_text)
        except ValueError:
            raise BadExpression("bad exponent %r" % exp_text)
    else:
        name, exponent = term, 1
    name = name.strip()
    if not name:
        raise BadExpression("empty unit name")
    return name, exponent


def parse_unit_expr(text: str, registry: dict):
    """Resolve a unit expression to (factor_to_base, dimensions)."""
    text = text.strip()
    if not text:
        raise BadExpression("empty unit expression")

    tokens = []
    current = ""
    op = "*"
    for ch in text:
        if ch in "*/":
            tokens.append((op, current.strip()))
            op = ch
            current = ""
        else:
            current += ch
    tokens.append((op, current.strip()))

    factor = 1.0
    dims = {}
    for index, (op, term) in enumerate(tokens):
        if not term:
            raise BadExpression("empty term in unit expression")
        name, exponent = _parse_term(term)
        if name not in registry:
            raise UnknownUnit(name)
        unit_factor, unit_dims = registry[name]
        if index == 0:
            power = exponent
        else:
            power = exponent if op == "*" else -exponent
        factor *= unit_factor ** power
        for key, value in unit_dims.items():
            dims[key] = dims.get(key, 0) + value * power

    dims = {k: v for k, v in dims.items() if v != 0}
    return factor, dims


def dim_string(dims: dict) -> str:
    if not dims:
        return "dimensionless"
    positive = [(k, v) for k, v in sorted(dims.items()) if v > 0]
    negative = [(k, -v) for k, v in sorted(dims.items()) if v < 0]
    parts = [k if v == 1 else "%s^%d" % (k, v) for k, v in positive]
    text = "*".join(parts) if parts else "1"
    for key, value in negative:
        text += "/" + (key if value == 1 else "%s^%d" % (key, value))
    return text


def fmt(value: float) -> str:
    if math.isfinite(value) and value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return "%.12g" % value


# --------------------------------------------------------------------------
# validation and computation
# --------------------------------------------------------------------------

def F(rule: str, line: int, message: str) -> dict:
    return {"rule": rule, "severity": "error", "line": line, "message": message}


def resolve(document: dict):
    findings = []
    results = {"conversions": [], "checks": []}

    if not document["queries"]:
        findings.append(F("no-queries", 0,
                          "no conversions or checks are declared"))

    registry = dict(BUILTINS)

    for unit in document["units"]:
        name = unit["name"]
        line = unit["line"]
        if name in registry:
            findings.append(F("duplicate-unit", line,
                              "unit %r is already defined" % name))
            continue
        try:
            scale = parse_number(unit["scale"])
        except BadNumber as exc:
            findings.append(F("bad-number", line, str(exc)))
            continue
        try:
            factor, dims = parse_unit_expr(unit["expr"], registry)
        except UnknownUnit as exc:
            findings.append(F("unknown-unit", line,
                              "unit %r uses %s" % (name, exc)))
            continue
        except BadExpression as exc:
            findings.append(F("bad-expression", line, str(exc)))
            continue
        registry[name] = (scale * factor, dims)

    tolerance = document["tolerance"]
    if tolerance is None:
        tolerance = 1e-9

    for query in document["queries"]:
        line = query["line"]
        try:
            lhs_value = parse_number(query["value"])
        except BadNumber as exc:
            findings.append(F("bad-number", line, str(exc)))
            continue
        try:
            lhs_factor, lhs_dims = parse_unit_expr(query["unit"], registry)
        except UnknownUnit as exc:
            findings.append(F("unknown-unit", line, str(exc)))
            continue
        except BadExpression as exc:
            findings.append(F("bad-expression", line, str(exc)))
            continue

        if query["kind"] == "convert":
            try:
                rhs_factor, rhs_dims = parse_unit_expr(query["to"], registry)
            except UnknownUnit as exc:
                findings.append(F("unknown-unit", line, str(exc)))
                continue
            except BadExpression as exc:
                findings.append(F("bad-expression", line, str(exc)))
                continue
            if lhs_dims != rhs_dims:
                findings.append(F(
                    "incompatible-units", line,
                    "%s is %s but %s is %s"
                    % (query["unit"], dim_string(lhs_dims),
                       query["to"], dim_string(rhs_dims))))
                continue
            base = lhs_value * lhs_factor
            results["conversions"].append({
                "line": line,
                "text": query["text"],
                "result": base / rhs_factor,
                "dimension": dim_string(lhs_dims),
            })

        else:
            try:
                rhs_value = parse_number(query["rhs_value"])
            except BadNumber as exc:
                findings.append(F("bad-number", line, str(exc)))
                continue
            try:
                rhs_factor, rhs_dims = parse_unit_expr(query["rhs_unit"], registry)
            except UnknownUnit as exc:
                findings.append(F("unknown-unit", line, str(exc)))
                continue
            except BadExpression as exc:
                findings.append(F("bad-expression", line, str(exc)))
                continue
            if lhs_dims != rhs_dims:
                findings.append(F(
                    "incompatible-units", line,
                    "%s is %s but %s is %s"
                    % (query["unit"], dim_string(lhs_dims),
                       query["rhs_unit"], dim_string(rhs_dims))))
                continue
            left = lhs_value * lhs_factor
            right = rhs_value * rhs_factor
            spread = max(1.0, abs(left), abs(right))
            ok = abs(left - right) <= tolerance * spread
            results["checks"].append({
                "line": line,
                "text": query["text"],
                "left": left,
                "right": right,
                "ok": ok,
                "dimension": dim_string(lhs_dims),
            })
            if not ok:
                findings.append(F(
                    "mismatch", line,
                    "%s does not hold: %s vs %s (%s)"
                    % (query["text"], fmt(left), fmt(right),
                       dim_string(lhs_dims))))

    findings.sort(key=lambda finding: (finding["line"], finding["rule"]))
    return findings, results


def summarize(document: dict, results: dict) -> dict:
    return {
        "units": len(document["units"]),
        "conversions": len(results["conversions"]),
        "checks": len(results["checks"]),
    }


# --------------------------------------------------------------------------
# the seal ledger (hash-chained)
# --------------------------------------------------------------------------

def resolve_store(explicit):
    if explicit:
        return pathlib.Path(explicit)
    env = os.environ.get("GAUGE_STORE")
    if env:
        return pathlib.Path(env)
    return pathlib.Path.home() / ".gauge" / "ledger.jsonl"


def load_records(store: pathlib.Path) -> list:
    if not store.exists():
        return []
    records = []
    with open(store, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                fail("store %s: line %d is not valid JSON" % (store, lineno))
    return records


def append_record(store: pathlib.Path, rec: dict) -> dict:
    records = load_records(store)
    prev = records[-1]["hash"] if records else GENESIS
    rec = dict(rec)
    rec["seq"] = len(records)
    rec["prev"] = prev
    rec["hash"] = record_hash(rec)
    store.parent.mkdir(parents=True, exist_ok=True)
    with open(store, "a", encoding="utf-8") as fh:
        fh.write(canonical(rec) + "\n")
    return rec


def verify_chain(records: list) -> list:
    problems = []
    expected_prev = GENESIS
    for i, rec in enumerate(records):
        if rec.get("seq") != i:
            problems.append("record %d: seq is %r, expected %d"
                            % (i, rec.get("seq"), i))
        if rec.get("prev") != expected_prev:
            problems.append("record %d: prev link is broken" % i)
        if rec.get("hash") != record_hash(rec):
            problems.append("record %d: content hash mismatch" % i)
        expected_prev = rec.get("hash")
    return problems


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_html(name: str, document: dict, findings: list, results, title: str) -> str:
    summary = summarize(document, results) if results is not None else None
    units = document["units"]

    if summary:
        cards = "".join(
            "<div class='card'><div class='n'>%s</div><div class='l'>%s</div></div>"
            % (_esc(value), label)
            for value, label in [
                (summary["conversions"], "conversions"),
                (summary["checks"], "checks"),
                (summary["units"], "units")])
    else:
        cards = ("<div class='card'><div class='n'>%d</div><div class='l'>errors</div></div>"
                 % len(findings))

    if results is not None:
        rows = []
        for conv in results["conversions"]:
            rows.append(
                "<tr><td>convert</td><td>%s</td><td class='num'>%s</td>"
                "<td>%s</td></tr>"
                % (_esc(conv["text"]), _esc(fmt(conv["result"])),
                   _esc(conv["dimension"])))
        for chk in results["checks"]:
            verdict = "<span class='ok'>holds</span>" if chk["ok"] else \
                      "<span class='sev'>mismatch</span>"
            rows.append(
                "<tr><td>check</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (_esc(chk["text"]), verdict, _esc(chk["dimension"])))
        table_html = ("<table><thead><tr><th>kind</th><th>declaration</th>"
                      "<th>result</th><th>dimension</th></tr></thead>"
                      "<tbody>%s</tbody></table>" % "".join(rows)) if rows else ""
    else:
        table_html = ""

    if not document["queries"]:
        verdict = "<p class='empty'>Nothing to convert.</p>"
    elif results is not None:
        if summary["checks"]:
            verdict = ("<p class='ok'>%d conversion%s and %d check%s resolved."
                       "</p>" % (summary["conversions"],
                                 "" if summary["conversions"] == 1 else "s",
                                 summary["checks"],
                                 "" if summary["checks"] == 1 else "s"))
        else:
            verdict = ("<p class='ok'>%d conversion%s resolved.</p>"
                       % (summary["conversions"],
                          "" if summary["conversions"] == 1 else "s"))
    else:
        verdict = ""

    if findings:
        items_html = "".join(
            "<li><span class='sev'>%s</span> line %d: %s</li>"
            % (_esc(f["rule"]), f["line"], _esc(f["message"])) for f in findings)
        finding_html = "<h2>Findings</h2><ul class='findings'>%s</ul>" % items_html
    else:
        finding_html = ("<p class='ok'>Every unit is defined and every "
                        "dimension agrees.</p>")

    unit_rows = "".join(
        "<tr><td>%s</td><td class='num'>%s</td><td>%s</td></tr>"
        % (_esc(u["name"]), _esc(u["scale"]), _esc(u["expr"])) for u in units)
    unit_table = ("<h2>Units</h2><table><thead><tr><th>unit</th>"
                  "<th class='num'>scale</th><th>expression</th></tr></thead>"
                  "<tbody>%s</tbody></table>" % unit_rows) if units else ""

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title>
<style>
:root { color-scheme: dark; }
body { margin:0; background:#1a1b26; color:#c0caf5;
  font-family:system-ui,-apple-system,Segoe UI,sans-serif; }
header { padding:28px 32px 8px; }
h1 { margin:0 0 6px; font-size:22px; }
.sub { color:#565f89; font-size:14px; }
main { padding:16px 32px 40px; }
.cards { display:flex; flex-wrap:wrap; gap:10px; margin:16px 0; }
.card { background:#1f2335; border:1px solid #3b4261; border-radius:8px; padding:10px 14px; min-width:110px; }
.card .n { font-size:20px; font-weight:600; }
.card .l { color:#565f89; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
h2 { font-size:15px; color:#565f89; text-transform:uppercase; letter-spacing:.05em; margin:26px 0 8px; }
table { border-collapse:collapse; width:100%%; max-width:820px; }
th, td { text-align:left; padding:7px 10px; border-bottom:1px solid #292e42; font-size:14px; }
th { color:#565f89; font-weight:600; font-size:12px; text-transform:uppercase; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
.findings { list-style:none; padding:0; }
.findings li { background:#1f2335; border:1px solid #3b4261; border-radius:8px; padding:10px 14px; margin:8px 0; font-size:14px; }
.sev { color:#f7768e; font-weight:700; font-size:11px; text-transform:uppercase; margin-right:8px; }
.ok { color:#9ece6a; }
.warn { color:#e0af68; }
.empty { color:#565f89; }
</style></head><body>
<header>
  <h1>%s</h1>
  <div class="sub">%d queries</div>
</header>
<main>
  <div class="cards">%s</div>
  %s
  <h2>Results</h2>
  %s
  %s
  %s
</main></body></html>
""" % (_esc(title), _esc(title), len(document["queries"]), cards, verdict,
       table_html, unit_table, finding_html)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def read_file(path: str):
    if path == "-":
        data = sys.stdin.buffer.read()
        return data.decode("utf-8"), "<stdin>", sha256_bytes(data)
    p = pathlib.Path(path)
    if not p.exists():
        fail("no such file: %s" % path)
    data = p.read_bytes()
    return data.decode("utf-8"), p.name, sha256_bytes(data)


def report_text(name, document, findings, results) -> str:
    lines = ["document: %s" % name,
             "%d queries, %d units" % (len(document["queries"]),
                                       len(document["units"])),
             ""]
    if results is not None:
        for conv in results["conversions"]:
            lines.append("  convert  %s = %s  [%s]"
                         % (conv["text"], fmt(conv["result"]),
                            conv["dimension"]))
        for chk in results["checks"]:
            lines.append("  check    %s  [%s]  %s"
                         % (chk["text"], chk["dimension"],
                            "holds" if chk["ok"] else "MISMATCH"))
    if findings:
        lines.append("")
        lines.append("%d findings:" % len(findings))
        for finding in findings:
            lines.append("  [%s] line %d: %s"
                         % (finding["rule"], finding["line"],
                            finding["message"]))
    else:
        lines.append("")
        lines.append("no findings")
    return "\n".join(lines)


def cmd_check(path, as_json):
    text, name, _ = read_file(path)
    document = parse_document(text)
    findings, _ = resolve(document)
    if as_json:
        print(json.dumps({
            "document": name,
            "findings": findings,
            "summary": {"queries": len(document["queries"]),
                        "units": len(document["units"])},
        }, indent=2, sort_keys=True))
    else:
        print(report_text(name, document, findings, None))
    return 1 if findings else 0


def cmd_convert(path, as_json):
    text, name, _ = read_file(path)
    document = parse_document(text)
    findings, results = resolve(document)
    if findings:
        if as_json:
            print(json.dumps({"document": name,
                              "findings": findings,
                              "results": results},
                             indent=2, sort_keys=True))
        else:
            print(report_text(name, document, findings, None))
        return 1
    if as_json:
        print(json.dumps({
            "document": name,
            "units": len(document["units"]),
            "conversions": results["conversions"],
            "checks": results["checks"],
            "summary": summarize(document, results),
        }, indent=2, sort_keys=True))
    else:
        print(report_text(name, document, findings, results))
    return 0


def cmd_render(path, out, title):
    text, name, _ = read_file(path)
    document = parse_document(text)
    findings, results = resolve(document)
    html = render_html(name, document, findings, results, title or name)
    out_path = pathlib.Path(out)
    if out_path.parent and str(out_path.parent) not in ("", "."):
        out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    print("wrote report to %s" % out)
    return 0


def cmd_seal(path, note, store, now):
    text, name, document_hash = read_file(path)
    document = parse_document(text)
    findings, results = resolve(document)
    if findings:
        count = len(findings)
        print("refusing to seal: the document has %d error%s"
              % (count, "" if count == 1 else "s"))
        for finding in findings:
            print("  [%s] line %d: %s"
                  % (finding["rule"], finding["line"], finding["message"]))
        return 1
    summary = summarize(document, results)
    results_hash = sha256_hex(canonical(results))
    rec = append_record(store, {
        "ts": now_iso(now), "tool": TOOL, "version": VERSION, "kind": "seal",
        "document": name, "document_hash": document_hash,
        "results_hash": results_hash,
        "units": summary["units"],
        "conversions": summary["conversions"],
        "checks": summary["checks"],
        "note": note or "",
    })
    print("sealed %s" % name)
    print("  document hash %s" % document_hash[:12])
    print("  results hash  %s" % results_hash[:12])
    print("  conversions   %d" % summary["conversions"])
    print("  checks        %d" % summary["checks"])
    print("  recorded as seq %d" % rec["seq"])
    return 0


def cmd_history(as_json, store):
    records = load_records(store)
    if as_json:
        print(json.dumps(records, indent=2, sort_keys=True))
        return 0
    if not records:
        print("no seals recorded")
        return 0
    for rec in records:
        print("%4d  %s  %-18s  %d conversions  %d checks  %s"
              % (rec["seq"], rec["ts"], rec["document"],
                 rec.get("conversions", 0), rec.get("checks", 0),
                 rec.get("note", "")))
    return 0


def cmd_verify(as_json, store):
    records = load_records(store)
    problems = verify_chain(records)
    if as_json:
        print(json.dumps({"ok": not problems, "records": len(records),
                          "problems": problems}, indent=2, sort_keys=True))
        return 0 if not problems else 1
    if not problems:
        print("chain intact: %d seals" % len(records))
        return 0
    print("chain broken: %d problems" % len(problems))
    for problem in problems:
        print("  - %s" % problem)
    return 1


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

class Checks:
    def __init__(self):
        self.total = 0
        self.failed = []

    def ok(self, condition, name):
        self.total += 1
        if not condition:
            self.failed.append(name)

    def eq(self, got, want, name):
        self.total += 1
        if got != want:
            self.failed.append("%s (got %r, want %r)" % (name, got, want))

    def close(self, got, want, name, tolerance=1e-9):
        self.total += 1
        if got is None or not (abs(got - want) <= tolerance * max(1.0, abs(want))):
            self.failed.append("%s (got %r, want %r)" % (name, got, want))

    def report(self):
        if not self.failed:
            print("all checks passed: %d" % self.total)
            return 0
        print("FAIL: %d of %d checks failed" % (len(self.failed), self.total))
        for name in self.failed:
            print("  - %s" % name)
        return 1


def _p(*lines) -> str:
    return "\n".join(lines) + "\n"


CLEAN = _p(
    'title "Beam check"',
    "unit furlong = 201.168 m",
    "convert 5 km to m",
    "convert 100 km/h to m/s",
    "convert 1 mi to km",
    "convert 1 L to m^3",
    "convert 1 furlong to m",
    "check 1 N = 1 kg*m/s^2",
    "check 1 J = 1 N*m",
    "check 1 W = 1 J/s",
)


def cmd_selftest():
    checks = Checks()

    def rules_of(text):
        try:
            document = parse_document(text)
        except GaugeError:
            return None
        return [f["rule"] for f in resolve(document)[0]]

    # --- parsing ----------------------------------------------------------
    doc = parse_document(_p('title "Site"', "unit furlong = 201.168 m",
                            "convert 5 km to m", "check 1 N = 1 kg*m/s^2"))
    checks.eq(doc["title"], "Site", "a title directive is unquoted")
    checks.eq(len(doc["units"]), 1, "a unit line parses")
    checks.eq(doc["units"][0]["name"], "furlong", "a unit name parses")
    checks.eq(doc["units"][0]["scale"], "201.168", "a unit scale parses")
    checks.eq(doc["units"][0]["expr"], "m", "a unit expression parses")
    checks.eq(doc["queries"][0]["kind"], "convert", "a convert line parses")
    checks.eq(doc["queries"][0]["value"], "5", "a convert value parses")
    checks.eq(doc["queries"][0]["unit"], "km", "a convert unit parses")
    checks.eq(doc["queries"][0]["to"], "m", "a convert target parses")
    checks.eq(doc["queries"][1]["kind"], "check", "a check line parses")
    checks.eq(doc["queries"][1]["rhs_unit"], "kg*m/s^2",
              "a check right side parses")
    checks.eq(doc["queries"][0]["line"], 3, "a query remembers its line")
    checks.eq(parse_document("; c\n# c\n\n")["queries"], [],
              "comment and blank lines are skipped")
    checks.eq(parse_document("tolerance 1e-6")["tolerance"], 1e-6,
              "a tolerance directive parses")
    checks.eq(parse_document("\ufefftitle \"x\"")["title"], "x",
              "a leading byte-order mark is tolerated")

    for bad, name in [
        (_p("convert 5 km"), "a convert without a target is refused"),
        (_p("convert km to m"), "a convert without a value is refused"),
        (_p("check 1 m"), "a check without an equals sign is refused"),
        (_p("unit x = 1000"), "a unit without an expression is refused"),
        (_p("unit = 1000 m"), "a unit without a name is refused"),
        (_p("title"), "a title with no value is refused"),
        (_p("tolerance abc"), "a bad tolerance is refused"),
        (_p("frobnicate x"), "an unknown line is refused"),
    ]:
        try:
            parse_document(bad)
            checks.ok(False, name)
        except GaugeError:
            checks.ok(True, name)

    # --- units and dimensions --------------------------------------------
    factor, dims = parse_unit_expr("kg*m/s^2", BUILTINS)
    checks.close(factor, 1.0, "a derived expression has factor one")
    checks.eq(dim_string(dims), "length*mass/time^2",
              "a derived expression has the dimension of force")
    factor, dims = parse_unit_expr("km/h", BUILTINS)
    checks.close(factor, 1000.0 / 3600.0, "km/h has the right factor")
    checks.eq(dim_string(dims), "length/time", "km/h is a speed")
    factor, dims = parse_unit_expr("1", BUILTINS)
    checks.eq(dim_string(dims), "dimensionless", "'1' is dimensionless")
    checks.close(parse_unit_expr("m^2", BUILTINS)[0], 1.0,
                 "an exponent applies to a unit")

    # --- validation -------------------------------------------------------
    checks.eq(resolve(parse_document(CLEAN))[0], [],
              "a clean document has no findings")
    checks.ok("no-queries" in (rules_of("; nothing\n") or []),
              "a document with no queries is reported")
    checks.ok("unknown-unit" in (rules_of(_p("convert 5 zz to m")) or []),
              "an unknown unit in a query is reported")
    checks.ok("unknown-unit" in (rules_of(_p("unit foo = 1 zz")) or []),
              "an unknown unit in a definition is reported")
    checks.ok("duplicate-unit" in (rules_of(_p("unit m = 1 m",
                                               "convert 1 m to m")) or []),
              "redefining a builtin unit is reported")
    checks.ok("duplicate-unit" in (rules_of(_p("unit zz = 1 m",
                                               "unit zz = 2 m",
                                               "convert 1 m to m")) or []),
              "redefining a user unit is reported")
    checks.ok("bad-number" in (rules_of(_p("convert x m to m")) or []),
              "a non-numeric value is reported")
    checks.ok("bad-expression" in (rules_of(_p("convert 5 m^ to m")) or []),
              "a malformed expression is reported")
    checks.ok("incompatible-units" in (rules_of(_p("convert 5 m to s")) or []),
              "a dimension mismatch is reported")
    checks.ok("mismatch" in (rules_of(_p("check 1 m = 2 m")) or []),
              "a failing check is reported")
    checks.eq(rules_of(_p("check 1 m = 100 cm")), [],
              "an equivalent check holds")
    checks.eq(rules_of(_p("check 1 m = 1.0000000001 m")), [],
              "a check within tolerance holds")
    checks.ok("mismatch" in (rules_of(_p("tolerance 1e-12",
                                         "check 1 m = 1.0000000001 m")) or []),
              "a tighter tolerance can fail a check")
    both = rules_of(_p("convert 5 zz to m", "convert 5 m to s")) or []
    checks.ok("unknown-unit" in both and "incompatible-units" in both,
              "several findings are all reported")

    # --- conversions ------------------------------------------------------
    findings, results = resolve(parse_document(CLEAN))
    checks.eq(findings, [], "the worked document resolves cleanly")
    conv = {c["text"]: c for c in results["conversions"]}

    def result_of(text):
        entry = conv.get(text)
        return None if entry is None else entry["result"]

    checks.close(result_of("convert 5 km to m"), 5000.0,
                 "kilometres convert to metres")
    checks.close(result_of("convert 100 km/h to m/s"), 100.0 * 1000.0 / 3600.0,
                 "a compound unit converts")
    checks.close(result_of("convert 1 mi to km"), 1.609344,
                 "miles convert to kilometres")
    checks.close(result_of("convert 1 L to m^3"), 0.001,
                 "litres convert to cubic metres")
    checks.close(result_of("convert 1 furlong to m"), 201.168,
                 "a user-defined unit converts")
    checks.eq(len(results["checks"]), 3, "every check is reported")
    checks.ok(all(c["ok"] for c in results["checks"]),
              "every check in the worked document holds")
    checks.eq(len(results["conversions"]), 5, "every conversion is reported")

    sci = resolve(parse_document(_p("convert 1e3 m to km")))[1]
    checks.close(sci["conversions"][0]["result"], 1.0,
                 "scientific notation parses")
    negative = resolve(parse_document(_p("convert -5 m to cm")))[1]
    checks.close(negative["conversions"][0]["result"], -500.0,
                 "a negative value converts")

    # --- exit codes -------------------------------------------------------
    def safe_code(text):
        try:
            document = parse_document(text)
        except GaugeError:
            return 2
        return 1 if resolve(document)[0] else 0

    checks.eq(safe_code(CLEAN), 0, "a clean document exits 0")
    checks.eq(safe_code(_p("convert 5 zz to m")), 1,
              "a document with a finding exits 1")
    checks.eq(safe_code(_p("convert 5 km")), 2,
              "an unparseable document exits 2")

    # --- chain ------------------------------------------------------------
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="gauge-selftest-"))
    try:
        store = tmp / "ledger.jsonl"
        first = append_record(store, {"ts": "2026-01-01T00:00:00",
                                      "kind": "seal", "document": "a"})
        checks.eq(first["seq"], 0, "the first seal has seq 0")
        checks.eq(first["prev"], GENESIS, "the first seal links to genesis")
        second = append_record(store, {"ts": "2026-01-02T00:00:00",
                                       "kind": "seal", "document": "b"})
        checks.eq(second["prev"], first["hash"],
                  "the second seal links to the first hash")
        checks.eq(verify_chain(load_records(store)), [],
                  "a clean seal chain verifies")
        tampered = load_records(store)
        tampered[0]["document"] = "forged"
        checks.ok(any("hash" in p for p in verify_chain(tampered)),
                  "editing a seal is caught")
        deleted = load_records(store)
        del deleted[0]
        checks.ok(len(verify_chain(deleted)) > 0,
                  "deleting the oldest seal is caught")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # --- render -----------------------------------------------------------
    demo = parse_document(CLEAN)
    findings, results = resolve(demo)
    html = render_html("demo", demo, findings, results, "demo")
    checks.ok("convert 5 km to m" in html, "render shows a conversion")
    checks.ok("5000" in html, "render shows a computed result")
    checks.ok("http://" not in html and "https://" not in html,
              "render contains no external URLs")
    checks.ok("<script" not in html, "render contains no script")
    empty_html = render_html("empty", parse_document(""), [], None, "empty")
    checks.ok("Nothing to convert" in empty_html,
              "render handles an empty document")

    return checks.report()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def extract_now(argv):
    now = None
    out = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--now":
            if i + 1 >= len(argv):
                fail("--now needs a value")
            now = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--now="):
            now = arg.split("=", 1)[1]
            i += 1
            continue
        out.append(arg)
        i += 1
    return now, out


def build_parser():
    parser = argparse.ArgumentParser(prog="gauge",
                                     description="Plain-text unit conversion and dimensional analysis.")
    parser.add_argument("--version", action="version",
                        version="%s %s" % (TOOL, VERSION))
    sub = parser.add_subparsers(dest="command")

    def add(name, help_text):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--store", default=None)
        sp.add_argument("--version", action="version",
                        version="%s %s" % (TOOL, VERSION))
        return sp

    sp = add("check", "Validate units and dimensions.")
    sp.add_argument("document")
    sp.add_argument("--json", action="store_true")

    sp = add("convert", "Resolve conversions and checks.")
    sp.add_argument("document")
    sp.add_argument("--json", action="store_true")

    sp = add("render", "Write a self-contained HTML report.")
    sp.add_argument("document")
    sp.add_argument("--out", required=True)
    sp.add_argument("--title", default=None)

    sp = add("seal", "Record the results in the seal ledger.")
    sp.add_argument("document")
    sp.add_argument("--note", default="")

    sp = add("history", "List recorded seals.")
    sp.add_argument("--json", action="store_true")

    sp = add("verify", "Verify the seal ledger hash chain.")
    sp.add_argument("--json", action="store_true")

    add("selftest", "Run the built-in checks.")
    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    now, argv = extract_now(argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_usage(sys.stderr)
        return 2
    store = resolve_store(getattr(args, "store", None))
    try:
        if args.command == "check":
            return cmd_check(args.document, args.json)
        if args.command == "convert":
            return cmd_convert(args.document, args.json)
        if args.command == "render":
            return cmd_render(args.document, args.out, args.title)
        if args.command == "seal":
            return cmd_seal(args.document, args.note, store, now)
        if args.command == "history":
            return cmd_history(args.json, store)
        if args.command == "verify":
            return cmd_verify(args.json, store)
        if args.command == "selftest":
            return cmd_selftest()
        fail("unknown command %r" % args.command)
    except GaugeError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
