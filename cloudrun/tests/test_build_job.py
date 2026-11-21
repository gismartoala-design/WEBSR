import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

from cloudrun.build_job import build_substitutions, submit_command
from cloudrun.render import render

BASE = Path(__file__).resolve().parents[2]


def config():
    return dict(PROJECT_ID="test-project", REGION="us-central1", DATASET="test",
                STORAGE_BACKEND="sftp", FTP_HOST="example.test", FTP_PORT="65002",
                FTP_USER="example", FTP_PASSWORD="never-publish-this-password",
                SFTP_ROOT="/home/example/websr-data", SFTP_HOST_KEY_SHA256="SHA256:" + "A" * 43,
                PARALLELISM="2")


class BuildJobTests(unittest.TestCase):
    def test_build_substitutions_exclude_password_and_keep_exact_full_task_count(self):
        cfg = config()
        manifest = dict(dataset="test", phase="companias", shards=[{}, {}, {}])
        values = build_substitutions(cfg, manifest, "companias", "full")
        self.assertEqual(values["_TASK_COUNT"], "3")
        self.assertEqual(values["_SFTP_HOST"], "example.test")
        self.assertNotIn(cfg["FTP_PASSWORD"], json.dumps(values))
        command = submit_command(cfg["PROJECT_ID"], values)
        self.assertIn("--config=cloudbuild.yaml", command)
        self.assertNotIn(cfg["FTP_PASSWORD"], " ".join(command))

    def test_manifest_must_match_dataset_and_phase(self):
        with self.assertRaises(ValueError):
            build_substitutions(config(), dict(dataset="other", phase="companias", shards=[{}]), "companias", "full")

    def test_count_based_renderer_validates_task_bounds(self):
        cfg = config()
        for count in [0, -1, 10001, True]:
            with self.subTest(count=count), self.assertRaises(ValueError):
                render(dict(dataset="test", phase="companias", task_count=count), None,
                       "example/image:v1", "worker@test.iam.gserviceaccount.com", backend="sftp", config=cfg)

    def test_cloudbuild_deploys_job_without_services_or_automatic_execution(self):
        build = yaml.safe_load((BASE / "cloudbuild.yaml").read_text())
        deploy = build["steps"][-1]
        self.assertEqual(deploy["entrypoint"], "gcloud")
        self.assertEqual(deploy["args"][:3], ["run", "jobs", "replace"])
        for step in build["steps"]:
            self.assertNotIn("services", step.get("args", []))
            self.assertNotIn("execute", step.get("args", []))
        self.assertEqual(build["substitutions"]["_MODE"], "pilot")

    def test_cloudbuild_render_step_runs_without_data_or_credentials_in_source(self):
        cfg = config()
        build = yaml.safe_load((BASE / "cloudbuild.yaml").read_text())
        values = dict(build["substitutions"])
        values.update(build_substitutions(cfg, dict(dataset="test", phase="companias", task_count=5), "companias", "pilot"))
        values.update(PROJECT_ID=cfg["PROJECT_ID"], BUILD_ID="build-unique-id")
        def replace(text):
            return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)",
                          lambda m: values[m[1] or m[2]], text)
        step = build["steps"][0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "cloudrun").mkdir()
            for name in ["__init__.py", "render.py", "settings.py", "job.yaml"]:
                shutil.copy2(BASE / "cloudrun" / name, root / "cloudrun" / name)
            env = dict(os.environ)
            for item in step["env"]:
                key, value = replace(item).split("=", 1)
                env[key] = value
            result = subprocess.run([sys.executable, *[replace(arg) for arg in step["args"]]],
                                    cwd=root, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            raw = (root / "cloudrun/rendered/job.yaml").read_text()
            job = yaml.safe_load(raw)
            self.assertEqual(job["kind"], "Job")
            self.assertEqual(job["metadata"]["name"], "iess-companias-piloto")
            spec = job["spec"]["template"]["spec"]
            self.assertEqual(spec["taskCount"], 1)
            self.assertEqual(spec["parallelism"], 1)
            container = spec["template"]["spec"]["containers"][0]
            self.assertTrue(container["image"].endswith(":build-unique-id"))
            self.assertNotIn(cfg["FTP_PASSWORD"], raw)


if __name__ == "__main__": unittest.main()
