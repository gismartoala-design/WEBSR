import copy
import csv
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests
import yaml
from google.api_core.exceptions import PreconditionFailed
from openpyxl import load_workbook

from cloudrun.shared import RateGate, Halted, retry_after_seconds, split_gs, update
from cloudrun.prepare import prepare, assert_local_stopped
from cloudrun.render import render
from cloudrun.worker import PoliteSession, TemporaryPortalError, process_rows, ShardLease
from cloudrun.collect import consolidate, write_reports
from cloudrun.pause_local import pause


class MemoryStore:
    def __init__(self):
        self.values = {}
        self.bucket = SimpleNamespace(name="test-bucket")

    def read(self, key):
        data, gen = self.values.get(key, (None, 0))
        return copy.deepcopy(data), gen

    def write(self, key, value, generation):
        if self.read(key)[1] != generation:
            raise PreconditionFailed("Cambió la generación")
        self.values[key] = (copy.deepcopy(value), generation + 1)

    def put(self, key, value, content_type=None):
        self.values[key] = (value, self.read(key)[1] + 1)

    def exists(self, key):
        return key in self.values

    def uri(self, key):
        return f"gs://{self.bucket.name}/{key}"


class Clock:
    now = 1000.
    def time(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class GateTests(unittest.TestCase):
    def setUp(self):
        self.store, self.clock = MemoryStore(), Clock()
        self.gate = RateGate(self.store, stable_after=10,
                             clock=self.clock.time, sleep=self.clock.sleep)

    def test_workers_share_start_interval(self):
        other = RateGate(self.store, clock=self.clock.time, sleep=self.clock.sleep)
        self.gate.acquire()
        start = self.clock.now
        other.acquire()
        self.assertGreaterEqual(self.clock.now - start, 4.)

    def test_rate_ramps_only_after_healthy_window(self):
        self.gate.acquire()
        for _ in range(9): self.gate.success()
        self.assertEqual(self.store.read(self.gate.key)[0]["interval"], 4)
        self.gate.success()
        self.assertAlmostEqual(self.store.read(self.gate.key)[0]["interval"], 3.4)

    def test_retry_after_pauses_every_worker_and_third_failure_halts(self):
        self.gate.failure("HTTP 429", retry_after=120)
        self.gate.acquire()
        self.assertGreaterEqual(self.clock.now, 1120)
        self.gate.failure("HTTP 503")
        self.gate.failure("HTTP 429")
        with self.assertRaises(Halted): self.gate.acquire()

    def test_conditional_retry_preserves_concurrent_change(self):
        store = self.store
        original = store.write
        calls = []
        def collide(key, value, generation):
            if not calls:
                calls.append(1)
                original(key, {"concurrent": True}, generation)
                raise PreconditionFailed("conflicto")
            original(key, value, generation)
        with patch.object(store, "write", side_effect=collide):
            state = update(store, "key", lambda old: dict(old or {}, own=True))
        self.assertEqual(state, {"concurrent": True, "own": True})

    def test_retry_after_date_and_invalid_value(self):
        self.assertEqual(retry_after_seconds("120", now=0), 120)
        self.assertEqual(retry_after_seconds("Thu, 01 Jan 1970 00:02:00 GMT", now=0), 120)
        self.assertEqual(retry_after_seconds("invalid", now=0), 0)

    def test_forbidden_and_captcha_halt_without_retry(self):
        for code, body in [(403, "Forbidden"), (200, "Please complete CAPTCHA")]:
            with self.subTest(code=code):
                store = MemoryStore()
                gate = RateGate(store, clock=self.clock.time, sleep=self.clock.sleep)
                response = requests.Response()
                response.status_code = code
                response._content = body.encode()
                with patch.object(requests.Session, "request", return_value=response) as send:
                    with PoliteSession(gate, lambda: None) as s:
                        with self.assertRaises(Halted): s.get("https://www.iess.gob.ec/test")
                    self.assertEqual(send.call_count, 1)
                self.assertTrue(store.read(gate.key)[0]["halted"])

    def test_http_429_applies_server_delay(self):
        response = requests.Response(); response.status_code = 429
        response._content = b"Too many requests"; response.headers["Retry-After"] = "180"
        with patch.object(requests.Session, "request", return_value=response):
            with PoliteSession(self.gate, lambda: None) as s:
                with self.assertRaises(TemporaryPortalError): s.post("https://www.iess.gob.ec/test")
        self.assertGreaterEqual(self.store.read(self.gate.key)[0]["pause_until"], 1180)

    def test_no_external_domain_or_uncontrolled_redirect(self):
        with PoliteSession(self.gate, lambda: None) as s:
            with self.assertRaises(Halted): s.get("https://example.com")

    def test_busy_lease_prevents_second_execution(self):
        first, second = ShardLease(self.store, "lease"), ShardLease(self.store, "lease")
        first.touch(initial=True)
        with self.assertRaises(Halted): second.touch(initial=True)


class DataTests(unittest.TestCase):
    def test_preparation_skips_existing_and_hashes_each_shard(self):
        import hashlib
        with TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "rucs").mkdir(); (base / "salida").mkdir()
            columns = ["ruc", "razon_social"]
            for phase, rucs in [("companias", ["0990000000001", "0990000001001"]), ("personas", ["0900000000001"])]:
                with (base / "rucs" / f"rucs_{phase}.csv").open("w", newline="") as f:
                    w = csv.DictWriter(f, columns); w.writeheader()
                    for r in rucs: w.writerow(dict(ruc=r, razon_social="Ejemplo"))
            (base / "salida/checkpoint_companias.csv").write_text("ruc,razon_social,estado,capital,pdf,nota,fecha\n0990000000001,Ejemplo,AL_DIA,,,,2026-11-20\n")
            out = base / "out"
            prepare(base, out, "test", 1)
            manifest = json.loads((out / "manifest_companias.json").read_text())
            self.assertEqual(manifest["pending_rows"], 1)
            self.assertEqual(manifest["baseline_rows"], 1)
            shard = manifest["shards"][0]
            raw = (out / shard["path"]).read_bytes()
            self.assertEqual(shard["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertIn(b"0990000001001", raw)
            self.assertNotIn(b"0990000000001", raw)

    def test_local_active_run_refuses_migration(self):
        import fcntl
        with TemporaryDirectory() as temp:
            base = Path(temp); (base / "salida").mkdir()
            with (base / "salida/ejecucion.lock").open("w") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                with self.assertRaises(RuntimeError): assert_local_stopped(base)

    def test_local_pause_saves_child_checkpoint_without_restarting_it(self):
        import subprocess
        import sys
        import time
        with TemporaryDirectory() as temp:
            base = Path(temp); (base / "salida").mkdir()
            (base / "consultar_iess.py").write_text(
                "import signal,time\nfrom pathlib import Path\n"
                "def stop(*args):\n    Path('saved').write_text('checkpoint')\n    raise SystemExit(0)\n"
                "signal.signal(signal.SIGINT,stop)\nPath('ready').touch()\n"
                "while True: time.sleep(.1)\n")
            (base / "ejecutar_consultas.py").write_text(
                "import subprocess,sys\nfrom pathlib import Path\n"
                "subprocess.call([sys.executable,'consultar_iess.py'])\n"
                "Path('restarted').touch()\n")
            proc = subprocess.Popen([sys.executable, str(base / "ejecutar_consultas.py")],
                                    cwd=base, start_new_session=True)
            try:
                (base / "salida/estado_ejecucion.json").write_text(json.dumps(dict(pid=proc.pid)))
                deadline = time.monotonic() + 5
                while not (base / "ready").exists():
                    if time.monotonic() > deadline: self.fail("No inició la fixture")
                    time.sleep(.02)
                pause(base)
                proc.wait(timeout=5)
                self.assertTrue((base / "saved").exists())
                self.assertFalse((base / "restarted").exists())
                status = json.loads((base / "salida/estado_ejecucion.json").read_text())
                self.assertEqual(status["estado"], "detenido_para_migracion")
            finally:
                if proc.poll() is None:
                    import os,signal
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()

    def test_yaml_has_correct_count_and_conservative_limits(self):
        manifest = dict(phase="companias", dataset="test", shards=[{}, {}, {}])
        for pilot, expected in [(False, 3), (True, 1)]:
            doc = yaml.safe_load(render(manifest, "test-bucket", "us-central1-docker.pkg.dev/project/repo/img:v1", "worker@project.iam.gserviceaccount.com", pilot=pilot))
            spec = doc["spec"]["template"]["spec"]
            self.assertEqual(spec["taskCount"], expected)
            self.assertEqual(spec["parallelism"], 1 if pilot else 2)
            task = spec["template"]["spec"]
            self.assertEqual(task["maxRetries"], 0)
            env = {e["name"]: e["value"] for e in task["containers"][0]["env"]}
            self.assertEqual(env["MIN_INTERVAL"], "1")
            self.assertEqual(env["MAX_RECORDS"], "20" if pilot else "0")

    def test_idempotent_retry_uses_persisted_result_without_network(self):
        store = MemoryStore()
        result = dict(ruc="0990000000001", estado="AL_DIA", razon_social="Ejemplo", capital="", pdf="", nota="", fecha="2026-11-20")
        store.write("run/results/companias/0990000000001.json", result, 0)
        records = []
        with patch("cloudrun.worker.iess.abrir", side_effect=AssertionError("No se debe consultar")):
            status = process_rows([dict(ruc=result["ruc"])], store, None, "run", "companias", lambda: None, records=records)
        self.assertEqual(status["existentes"], 1)
        self.assertEqual(records, [result])

    def test_checkpoint_committed_before_unknown_response_stops_every_worker(self):
        store, clock, records = MemoryStore(), Clock(), []
        gate = RateGate(store, clock=clock.time, sleep=clock.sleep)
        rows = [dict(ruc="0990000000001", razon_social="Primera"),
                dict(ruc="0990000001001", razon_social="Segunda")]
        answers = [dict(ruc=rows[0]["ruc"], estado="AL_DIA", capital="", pdf="", nota=""),
                   dict(ruc=rows[1]["ruc"], estado="DESCONOCIDO", capital="", pdf="", nota="No verificable")]
        with patch("cloudrun.worker.iess.abrir", return_value={}), \
             patch("cloudrun.worker.iess.pedir", return_value=object()), \
             patch("cloudrun.worker.iess.interpretar", side_effect=answers):
            with self.assertRaises(Halted):
                process_rows(rows, store, gate, "run", "companias", lambda: None, records=records)
        self.assertTrue(store.exists("run/results/companias/0990000000001.json"))
        self.assertFalse(store.exists("run/results/companias/0990000001001.json"))
        self.assertEqual(len(records), 1)
        self.assertTrue(store.read(gate.key)[0]["halted"])

    def test_debt_pdf_is_uploaded_before_checkpoint_and_local_copy_is_removed(self):
        store, clock = MemoryStore(), Clock()
        gate = RateGate(store, clock=clock.time, sleep=clock.sleep)
        with TemporaryDirectory() as temp:
            out = Path(temp); (out / "pdf").mkdir()
            pdf = out / "pdf/0990000000001.pdf"; pdf.write_bytes(b"test-pdf")
            result = dict(ruc="0990000000001", estado="EN_MORA", capital="432.00", pdf=str(pdf), nota="")
            with patch("cloudrun.worker.iess.SALIDA", out), \
                 patch("cloudrun.worker.iess.abrir", return_value={}), \
                 patch("cloudrun.worker.iess.pedir", return_value=object()), \
                 patch("cloudrun.worker.iess.interpretar", return_value=result):
                status = process_rows([dict(ruc=result["ruc"], razon_social="Ejemplo")], store, gate, "run", "companias", lambda: None)
            saved, _ = store.read("run/results/companias/0990000000001.json")
            self.assertEqual(saved["pdf"], "gs://test-bucket/run/pdf/companias/0990000000001.pdf")
            self.assertEqual(store.read("run/pdf/companias/0990000000001.pdf")[0], b"test-pdf")
            self.assertFalse(pdf.exists())
            self.assertEqual(status["nuevos"], 1)

    def test_consolidation_rejects_duplicates_and_excel_preserves_names_as_text(self):
        row = dict(ruc="0990000000001", razon_social="=1+1", estado="EN_MORA", capital="432.00", pdf="gs://test/pdf", nota="", fecha="2026-11-20")
        manifest = dict(source_rows=1)
        with self.assertRaises(ValueError): consolidate(manifest, [row], [[row]])
        rows = consolidate(manifest, [row], [])
        with TemporaryDirectory() as temp:
            write_reports(rows, "companias", Path(temp))
            wb = load_workbook(Path(temp) / "deudores_companias.xlsx")
            self.assertEqual(wb.active["B2"].data_type, "s")
            self.assertEqual(wb.active["D2"].value, 432.)
            self.assertEqual(wb.active["A2"].value, "0990000000001")
            wb.close()

    def test_bad_storage_path_is_rejected(self):
        for value in ["https://example.com", "gs://bucket/../foo", "gs://bucket"]:
            with self.assertRaises(ValueError): split_gs(value)


if __name__ == "__main__":
    unittest.main()
