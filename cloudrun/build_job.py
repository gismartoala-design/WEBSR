"""Lanza Cloud Build usando cloudbuild.yaml para desplegar un Job, sin ejecutarlo."""
import argparse
import json
from pathlib import Path
import subprocess

from cloudrun.render import render
from cloudrun.settings import settings

BASE = Path(__file__).resolve().parents[1]


def build_substitutions(config, manifest, phase, mode):
    if mode not in {"pilot", "full"} or phase not in {"companias", "personas"}:
        raise ValueError("Fase o modo de despliegue no válido")
    dataset = config.get("DATASET", "guayas-v1")
    if manifest.get("dataset") != dataset or manifest.get("phase") != phase:
        raise ValueError("El manifiesto no coincide con el dataset y la fase configurados")
    count = len(manifest["shards"]) if "shards" in manifest else manifest.get("task_count", 0)
    backend = config.get("STORAGE_BACKEND", config.get("STORAGE_TYPE", "sftp"))
    project = config["PROJECT_ID"]
    region = config.get("REGION", "us-central1")
    repository = config.get("REPOSITORY", "iess")
    account = config.get("SERVICE_ACCOUNT_NAME", "iess-worker")
    secret = config.get("SFTP_PASSWORD_SECRET", "iess-sftp-password")
    parallelism = int(config.get("PARALLELISM", "2"))
    # Valida toda la configuración antes de enviar el build a Google Cloud.
    render(manifest, config.get("BUCKET"),
           f"{region}-docker.pkg.dev/{project}/{repository}/worker:validation",
           f"{account}@{project}.iam.gserviceaccount.com", parallelism,
           mode == "pilot", backend, config, secret)
    values = dict(_REGION=region, _REPOSITORY=repository,
                  _SERVICE_ACCOUNT_NAME=account, _PHASE=phase, _DATASET=dataset,
                  _MODE=mode, _TASK_COUNT=str(count), _PARALLELISM=str(parallelism),
                  _STORAGE_BACKEND=backend, _GCS_BUCKET=config.get("BUCKET", ""),
                  _SFTP_PASSWORD_SECRET=secret)
    for name, default in [("FTP_HOST", ""), ("FTP_PORT", "65002"), ("FTP_USER", ""),
                          ("SFTP_ROOT", ""), ("SFTP_HOST_KEY_SHA256", ""),
                          ("SFTP_PHP_BIN", "/usr/bin/php")]:
        target = "_SFTP_" + name[4:] if name.startswith("FTP_") else "_" + name
        values[target] = config.get(name, default) if backend == "sftp" else default
    return values


def submit_command(project, values):
    if any("|" in str(value) for value in values.values()):
        raise ValueError("Los valores de configuración no deben contener |")
    flag = "^|^" + "|".join(f"{key}={value}" for key, value in values.items())
    return ["gcloud", "builds", "submit", ".", "--config=cloudbuild.yaml",
            f"--project={project}", "--substitutions=" + flag]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", choices=["companias", "personas"], default="companias")
    p.add_argument("--mode", choices=["pilot", "full"], default="pilot")
    p.add_argument("--print-substitutions", action="store_true", help="Imprime solo metadatos para configurar el trigger; no incluye contraseñas")
    a = p.parse_args()
    config = settings()
    path = BASE / "cloudrun/staging" / config.get("DATASET", "guayas-v1") / f"manifest_{a.phase}.json"
    if path.exists():
        manifest = json.loads(path.read_text())
    elif a.mode == "pilot":
        manifest = dict(dataset=config.get("DATASET", "guayas-v1"), phase=a.phase, task_count=1)
    else:
        p.error("La corrida completa requiere preparar antes los lotes con deploy.sh prepare")
    values = build_substitutions(config, manifest, a.phase, a.mode)
    if a.print_substitutions:
        print(json.dumps(values, ensure_ascii=False, indent=2))
        return
    print("Se construirá y desplegará un Cloud Run Job. Este paso no ejecuta consultas.", flush=True)
    subprocess.run(submit_command(config["PROJECT_ID"], values), cwd=BASE, check=True)


if __name__ == "__main__":
    main()
