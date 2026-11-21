"""Genera YAML de piloto o corrida completa sin desplegar ni ejecutar nada."""
import argparse
import json
from pathlib import Path
import re
from string import Template


def render(manifest, bucket, image, account, parallelism=2, pilot=False,
           backend="gcs", config=None, password_secret="iess-sftp-password"):
    if backend not in {"gcs", "sftp"}:
        raise ValueError("Almacenamiento no reconocido")
    if backend == "gcs" and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", bucket or ""):
        raise ValueError("Nombre de bucket no válido")
    if not re.fullmatch(r"[A-Za-z0-9@/._:-]+", image) or not re.fullmatch(r"[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+", account):
        raise ValueError("Imagen o cuenta de servicio no válida")
    if not 1 <= parallelism <= 5:
        raise ValueError("Paralelismo inicial permitido: 1..5")
    count = len(manifest["shards"]) if "shards" in manifest else manifest.get("task_count", 0)
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 10_000:
        raise ValueError("No hay lotes pendientes para esta fase")
    phase, dataset = manifest["phase"], manifest["dataset"]
    if phase not in {"companias", "personas"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", dataset):
        raise ValueError("Manifiesto no válido")
    env = {"STORAGE_BACKEND": backend}
    secret_block = ""
    if backend == "sftp":
        config = config or {}
        for name in ["FTP_HOST", "FTP_USER", "SFTP_ROOT", "SFTP_HOST_KEY_SHA256"]:
            if not config.get(name): raise ValueError(f"Falta {name}; ejecuta hostinger.admin init")
            env[name] = config[name]
        if not re.fullmatch(r"[A-Za-z0-9.-]+", env["FTP_HOST"]):
            raise ValueError("Host SFTP no válido")
        if not env["SFTP_ROOT"].startswith("/home/") or "public_html" in env["SFTP_ROOT"].split("/"):
            raise ValueError("SFTP_ROOT debe ser privado")
        if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", env["SFTP_HOST_KEY_SHA256"]):
            raise ValueError("Huella SFTP no válida")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", password_secret):
            raise ValueError("Nombre de secreto no válido")
        env.update(FTP_PORT=str(int(config.get("FTP_PORT", "65002"))),
                   SFTP_PHP_BIN=config.get("SFTP_PHP_BIN", "php"), EXPORT_INTERVAL="900")
        if not 1 <= int(env["FTP_PORT"]) <= 65535: raise ValueError("Puerto no válido")
        secret_block = ("\n                - name: FTP_PASSWORD\n"
                        "                  valueFrom:\n"
                        "                    secretKeyRef:\n"
                        f"                      name: {password_secret}\n"
                        "                      key: latest")
    storage_env = "\n".join(f"                - name: {key}\n                  value: {json.dumps(str(value))}" for key, value in env.items()) + secret_block
    manifest_uri = (f"sftp:///runs/{dataset}/manifest_{phase}.json" if backend == "sftp"
                    else f"gs://{bucket}/runs/{dataset}/manifest_{phase}.json")
    values = dict(JOB_NAME=f"iess-{phase}" + ("-piloto" if pilot else ""),
                  TASK_COUNT=1 if pilot else count,
                  PARALLELISM=1 if pilot else min(parallelism, count),
                  SERVICE_ACCOUNT=account, IMAGE=image,
                  MANIFEST_URI=manifest_uri, STORAGE_ENV=storage_env,
                  MAX_RECORDS=20 if pilot else 0)
    return Template(Path(__file__).with_name("job.yaml").read_text()).substitute(values)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--task-count", type=int)
    p.add_argument("--phase", choices=["companias", "personas"], default="companias")
    p.add_argument("--dataset")
    p.add_argument("--bucket")
    p.add_argument("--storage", choices=["gcs", "sftp"], default="gcs")
    p.add_argument("--password-secret", default="iess-sftp-password")
    p.add_argument("--image", required=True)
    p.add_argument("--service-account", required=True)
    p.add_argument("--parallelism", type=int, default=2)
    p.add_argument("--pilot", action="store_true")
    p.add_argument("--mode", choices=["pilot", "full"])
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.pilot and a.mode == "full":
        p.error("--pilot y --mode full son incompatibles")
    if a.manifest:
        manifest = json.loads(a.manifest.read_text())
    else:
        if not a.dataset: p.error("--task-count requiere --dataset")
        manifest = dict(phase=a.phase, dataset=a.dataset, task_count=a.task_count)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    from cloudrun.settings import settings
    a.output.write_text(render(manifest, a.bucket, a.image,
                               a.service_account, a.parallelism, a.pilot or a.mode == "pilot",
                               a.storage, settings(), a.password_secret))
    print(a.output)


if __name__ == "__main__":
    main()
