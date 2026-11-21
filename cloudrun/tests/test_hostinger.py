import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import paramiko
import yaml

from cloudrun.sftp_store import HostPolicy, SFTPStore, fingerprint, validate_key, object_location
from cloudrun.settings import read_env
from cloudrun.render import render


class HostingerTests(unittest.TestCase):
    def test_env_values_are_never_executed(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "must-not-exist"
            path = Path(temp) / ".env"
            path.write_text(f"FTP_PASSWORD='$(touch {marker})'\nFTP_PORT=65002\n")
            config = read_env(path)
            self.assertEqual(config["FTP_PASSWORD"], f"$(touch {marker})")
            self.assertFalse(marker.exists())

    def test_host_key_is_pinned_and_required_for_workers(self):
        class Key:
            def asbytes(self): return b"example-server-public-key"
        key = Key()
        expected = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        self.assertEqual(fingerprint(key), expected)
        HostPolicy(expected).missing_host_key(None, "example", key)
        with self.assertRaises(paramiko.SSHException): HostPolicy("SHA256:other").missing_host_key(None, "example", key)
        with self.assertRaises(paramiko.SSHException): HostPolicy("").missing_host_key(None, "example", key)

    def test_private_root_and_object_paths_are_required(self):
        with self.assertRaises(ValueError): SFTPStore({"SFTP_ROOT": "/home/example/public_html/data"})
        for path in ["/absolute", "../outside", "a/../outside", "a//file", "a\\file"]:
            with self.assertRaises(ValueError): validate_key(path)

    def test_sftp_envelope_preserves_conditional_generation(self):
        store = object.__new__(SFTPStore)
        store.bytes = lambda _: json.dumps(dict(_websr_store=1, generation=42, value={"estado": "AL_DIA"})).encode()
        self.assertEqual(store.read("key"), ({"estado": "AL_DIA"}, 42))
        store.bytes = lambda _: b'{"estado":"completado"}'
        self.assertEqual(store.read("key"), ({"estado": "completado"}, 0))

    def test_sftp_yaml_references_secret_without_embedding_password(self):
        config = dict(FTP_HOST="example.test", FTP_USER="example", FTP_PORT="65002",
                      FTP_PASSWORD="never-embed-this-password", SFTP_ROOT="/home/example/websr-data",
                      SFTP_HOST_KEY_SHA256="SHA256:" + "A" * 43)
        text = render(dict(phase="personas", dataset="test", shards=[{}]), None,
                      "us-central1-docker.pkg.dev/test/repo/image:v1", "worker@test.iam.gserviceaccount.com",
                      backend="sftp", config=config)
        self.assertNotIn(config["FTP_PASSWORD"], text)
        spec = yaml.safe_load(text)["spec"]["template"]["spec"]["template"]["spec"]
        env = {item["name"]: item for item in spec["containers"][0]["env"]}
        self.assertEqual(env["MANIFEST_URI"]["value"], "sftp:///runs/test/manifest_personas.json")
        self.assertEqual(env["FTP_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"], "iess-sftp-password")
        self.assertEqual(env["EXPORT_INTERVAL"]["value"], "900")
        self.assertEqual(spec["containers"][0]["resources"]["limits"]["memory"], "1Gi")


@unittest.skipUnless(shutil.which("php") and str(Path(__file__).resolve()).startswith("/home/"), "Requiere PHP CLI y entorno Linux bajo /home")
class PHPStorageTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parents[2]
        (self.base / ".local").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.base / ".local")
        self.root = Path(self.temp.name)
        self.script = self.base / "hostinger/cas.php"

    def tearDown(self): self.temp.cleanup()

    def call(self, key, generation, value):
        result = subprocess.run(["php", str(self.script), str(self.root)],
                                input=json.dumps(dict(key=key, generation=generation, value=value)),
                                text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_only_one_concurrent_create_can_succeed(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.call("control/test.json", 0, {"writer": i}), range(8)))
        self.assertEqual(sum(row.get("ok", False) for row in results), 1)
        self.assertEqual(sum(row.get("conflict", False) for row in results), 7)
        self.assertTrue(self.call("control/test.json", 1, {"saved": True})["ok"])
        state = json.loads((self.root / "objects/control/test.json").read_text())
        self.assertEqual(state["generation"], 2)
        self.assertEqual(state["value"], {"saved": True})

    def test_paths_cannot_escape_private_store(self):
        result = subprocess.run(["php", str(self.script), str(self.root)],
                                input=json.dumps(dict(key="../outside", generation=0, value={})),
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "outside").exists())

    def test_packed_records_preserve_two_concurrent_rucs(self):
        buckets = {}
        pair = None
        for n in range(2000):
            key = f"runs/test/results/companias/{n:013d}.json"
            physical, member = object_location(key)
            if physical in buckets:
                pair = [buckets[physical], key]
                break
            buckets[physical] = key
        self.assertIsNotNone(pair)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda key: self.call(key, 0, {"ruc": key.rsplit('/', 1)[1][:-5]}), pair))
        self.assertTrue(all(result["ok"] for result in results))
        physical, _ = object_location(pair[0])
        packed = json.loads((self.root / "objects" / physical).read_text())
        self.assertEqual(len(packed), 2)
        self.assertEqual(len(list((self.root / "objects/runs/test/results/companias/_packs").glob('*.json'))), 1)


if __name__ == "__main__": unittest.main()
