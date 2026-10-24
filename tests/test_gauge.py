import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TOOL = Path(__file__).parent.parent / "gauge.py"


def run(*args, store=None, env=None):
    cmd = [sys.executable, str(TOOL), *args]
    e = os.environ.copy()
    if env:
        e.update(env)
    if store:
        e["GAUGE_STORE"] = str(store)
    return subprocess.run(cmd, capture_output=True, text=True, env=e)


CLEAN = textwrap.dedent("""\
    title "Beam check"
    unit furlong = 201.168 m
    convert 5 km to m
    convert 100 km/h to m/s
    convert 1 mi to km
    convert 1 L to m^3
    convert 1 furlong to m
    check 1 N = 1 kg*m/s^2
    check 1 J = 1 N*m
    check 1 W = 1 J/s
    """)


class Base(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="gauge-e2e-"))
        self.store = self.td / "ledger.jsonl"
        self.env_store = {"GAUGE_STORE": str(self.store)}

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def doc(self, text, name="measurement.gauge"):
        p = self.td / name
        p.write_text(text, encoding="utf-8")
        return p


class TestSelfTest(Base):
    def test_selftest_passes(self):
        proc = run("selftest")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("all checks passed", proc.stdout)


class TestCLI(Base):
    def test_version(self):
        proc = run("--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("gauge", proc.stdout)

    def test_subcommand_version(self):
        proc = run("convert", "--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("1.0.0", proc.stdout)

    def test_help(self):
        self.assertEqual(run("--help").returncode, 0)

    def test_unknown_command_exits_two(self):
        proc = subprocess.run([sys.executable, str(TOOL), "frobnicate"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)

    def test_no_command_exits_two(self):
        proc = subprocess.run([sys.executable, str(TOOL)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)


class TestCheck(Base):
    def test_clean_exits_zero(self):
        proc = run("check", str(self.doc(CLEAN)))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_unknown_unit_exits_one(self):
        proc = run("check", str(self.doc("convert 5 zz to m\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown-unit", proc.stdout)

    def test_no_queries_exits_one(self):
        proc = run("check", str(self.doc("; empty\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no-queries", proc.stdout)

    def test_duplicate_unit_exits_one(self):
        proc = run("check", str(self.doc("unit m = 1 m\nconvert 1 m to m\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("duplicate-unit", proc.stdout)

    def test_incompatible_units_exits_one(self):
        proc = run("check", str(self.doc("convert 5 m to s\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("incompatible-units", proc.stdout)

    def test_mismatch_exits_one(self):
        proc = run("check", str(self.doc("check 1 m = 2 m\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("mismatch", proc.stdout)

    def test_bad_number_exits_one(self):
        proc = run("check", str(self.doc("convert x m to m\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("bad-number", proc.stdout)

    def test_parse_error_exits_two(self):
        proc = run("check", str(self.doc("frobnicate x\n")))
        self.assertEqual(proc.returncode, 2)

    def test_json_shape(self):
        proc = run("check", str(self.doc(CLEAN)), "--json")
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIn("findings", data)
        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["queries"], 8)
        self.assertEqual(data["summary"]["units"], 1)

    def test_reads_stdin(self):
        p = subprocess.Popen([sys.executable, str(TOOL), "check", "-"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        out, err = p.communicate(input=CLEAN)
        self.assertEqual(p.returncode, 0, err)

    def test_tolerates_utf8_bom(self):
        proc = run("check", str(self.doc("\ufeff" + CLEAN)))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_comments_and_blanks_are_skipped(self):
        proc = run("check", str(self.doc("; comment\n\n# another\n" + CLEAN)))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class TestConvert(Base):
    def test_convert_exits_zero_on_clean(self):
        proc = run("convert", str(self.doc(CLEAN)))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("5000", proc.stdout)
        self.assertIn("holds", proc.stdout)

    def test_convert_with_findings_exits_one(self):
        proc = run("convert", str(self.doc("convert 5 m to s\n")))
        self.assertEqual(proc.returncode, 1)

    def test_convert_json_has_results(self):
        proc = run("convert", str(self.doc(CLEAN)), "--json")
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(len(data["conversions"]), 5)
        self.assertEqual(len(data["checks"]), 3)
        self.assertTrue(all(c["ok"] for c in data["checks"]))
        results = {c["text"]: c["result"] for c in data["conversions"]}
        self.assertEqual(results["convert 5 km to m"], 5000.0)
        self.assertAlmostEqual(results["convert 1 mi to km"], 1.609344)

    def test_compound_unit_converts(self):
        proc = run("convert", str(self.doc("convert 100 km/h to m/s\n")))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("27.777", proc.stdout)

    def test_user_unit_converts(self):
        proc = run("convert", str(self.doc(
            "unit furlong = 201.168 m\nconvert 2 furlong to m\n")))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("402.336", proc.stdout)

    def test_scientific_notation(self):
        proc = run("convert", str(self.doc("convert 1e3 m to km\n")))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("= 1", proc.stdout)

    def test_tolerance_directive_is_honoured(self):
        proc = run("convert", str(self.doc(
            "tolerance 1e-12\ncheck 1 m = 1.0000000001 m\n")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("mismatch", proc.stdout)


class TestRender(Base):
    def test_render_writes_file(self):
        out = self.td / "report.html"
        proc = run("render", str(self.doc(CLEAN)), "--out", str(out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(out.exists())
        content = out.read_text(encoding="utf-8")
        self.assertIn("convert 5 km to m", content)
        self.assertIn("5000", content)
        self.assertIn("length*mass/time^2", content)
        self.assertNotIn("http://", content)
        self.assertNotIn("https://", content)
        self.assertNotIn("<script", content)

    def test_render_empty_document(self):
        out = self.td / "report.html"
        proc = run("render", str(self.doc("; empty\n")), "--out", str(out))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Nothing to convert", out.read_text(encoding="utf-8"))

    def test_render_broken_document_still_exits_zero(self):
        out = self.td / "report.html"
        proc = run("render", str(self.doc("convert 5 m to s\n")),
                   "--out", str(out))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("incompatible-units", out.read_text(encoding="utf-8"))

    def test_render_title_override(self):
        out = self.td / "report.html"
        proc = run("render", str(self.doc(CLEAN)), "--out", str(out),
                   "--title", "Beam Review")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Beam Review", out.read_text(encoding="utf-8"))

    def test_render_creates_parent_directory(self):
        out = self.td / "nested" / "deeper" / "report.html"
        proc = run("render", str(self.doc(CLEAN)), "--out", str(out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(out.exists())


class TestSealLedger(Base):
    def test_seal_clean(self):
        proc = run("seal", str(self.doc(CLEAN)), store=self.store)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.store.exists())
        self.assertIn("recorded as seq 0", proc.stdout)
        recs = self.store.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(recs), 1)
        rec = json.loads(recs[0])
        self.assertEqual(rec["document"], "measurement.gauge")
        self.assertEqual(rec["units"], 1)
        self.assertEqual(rec["conversions"], 5)
        self.assertEqual(rec["checks"], 3)
        self.assertEqual(len(rec["document_hash"]), 64)
        self.assertEqual(len(rec["results_hash"]), 64)

    def test_seal_refuses_findings(self):
        proc = run("seal", str(self.doc("convert 5 m to s\n")),
                   store=self.store)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing to seal", proc.stdout)
        self.assertFalse(self.store.exists())

    def test_seal_refuses_parse_error(self):
        proc = run("seal", str(self.doc("frobnicate\n")), store=self.store)
        self.assertEqual(proc.returncode, 2)

    def test_second_seal_is_seq_one(self):
        run("seal", str(self.doc(CLEAN, "a.gauge")), store=self.store)
        proc = run("seal", str(self.doc("convert 1 m to cm\n", "b.gauge")),
                   store=self.store)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("recorded as seq 1", proc.stdout)

    def test_seal_deterministic_timestamp(self):
        run("seal", str(self.doc(CLEAN)), "--now", "2026-01-01T00:00:00",
            store=self.store)
        rec = json.loads(self.store.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["ts"], "2026-01-01T00:00:00")

    def test_seal_uses_env_store(self):
        proc = subprocess.run(
            [sys.executable, str(TOOL), "seal", str(self.doc(CLEAN))],
            capture_output=True, text=True,
            env={**os.environ, "GAUGE_STORE": str(self.store)})
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_seal_explicit_store_wins(self):
        other = self.td / "other.jsonl"
        run("seal", str(self.doc(CLEAN)), "--store", str(other),
            store=self.store)
        self.assertTrue(other.exists())
        self.assertFalse(self.store.exists())

    def test_history_lists_seals(self):
        run("seal", str(self.doc(CLEAN)), "--note", "beam review",
            store=self.store)
        proc = run("history", store=self.store)
        self.assertIn("beam review", proc.stdout)

    def test_history_json(self):
        run("seal", str(self.doc(CLEAN)), store=self.store)
        proc = run("history", "--json", store=self.store)
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["conversions"], 5)


class TestVerify(Base):
    def test_verify_clean(self):
        run("seal", str(self.doc(CLEAN)), store=self.store)
        run("seal", str(self.doc(CLEAN)), store=self.store)
        proc = run("verify", store=self.store)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("chain intact", proc.stdout)

    def test_verify_empty(self):
        proc = run("verify", store=self.store)
        self.assertEqual(proc.returncode, 0)

    def test_verify_json(self):
        run("seal", str(self.doc(CLEAN)), store=self.store)
        proc = run("verify", "--json", store=self.store)
        data = json.loads(proc.stdout)
        self.assertTrue(data["ok"])
        self.assertEqual(data["records"], 1)

    def test_verify_detects_an_edit(self):
        run("seal", str(self.doc(CLEAN)), store=self.store)
        text = self.store.read_text(encoding="utf-8").replace(
            '"note":""', '"note":"forged"')
        self.store.write_text(text, encoding="utf-8")
        proc = run("verify", store=self.store)
        self.assertNotEqual(proc.returncode, 0)

    def test_verify_detects_a_deleted_middle(self):
        run("seal", str(self.doc(CLEAN)), store=self.store)
        run("seal", str(self.doc(CLEAN)), store=self.store)
        lines = self.store.read_text(encoding="utf-8").splitlines()
        self.store.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
        proc = run("verify", store=self.store)
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
