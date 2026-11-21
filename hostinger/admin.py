"""Inicializa el almacén privado, sube lotes y configura el secreto de Cloud Run."""
import argparse
import io
import json
from pathlib import Path
import posixpath
import shlex
import subprocess

from cloudrun.settings import settings, read_env
from cloudrun.sftp_store import SFTPStore, connect, mkdirs

BASE = Path(__file__).resolve().parents[1]


def save_local(values):
    path = BASE / ".env"
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(values)
    for index, line in enumerate(lines):
        name = line.split("=", 1)[0].strip() if "=" in line else ""
        if name in remaining:
            lines[index] = name + "=" + shlex.quote(str(remaining.pop(name)))
    lines.extend(name + "=" + shlex.quote(str(value)) for name, value in remaining.items())
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def initialize():
    config = settings()
    client, key = connect(config, first_connection=True)
    try:
        sftp = client.open_sftp()
        home = sftp.normalize(".")
        root = config.get("SFTP_ROOT") or posixpath.join(home, "websr-data")
        if not root.startswith("/home/") or "public_html" in root.split("/") or ".." in root.split("/"):
            raise ValueError("La carpeta IESS debe quedar fuera de public_html y bajo /home")
        stdin, out, err = client.exec_command("command -v php", timeout=20)
        php = out.read().decode().strip()
        if out.channel.recv_exit_status() or not php.startswith("/"):
            raise RuntimeError("El hosting necesita PHP CLI para coordinar escrituras")
        stdin, out, err = client.exec_command(shlex.quote(php) + " -r 'echo PHP_MAJOR_VERSION;'", timeout=20)
        if out.read().decode().strip() not in {"8", "9"}:
            raise RuntimeError("El almacén requiere PHP CLI 8 o superior")
        for directory in [root, root + "/.bin", root + "/.locks", root + "/objects"]:
            mkdirs(sftp, directory)
            sftp.chmod(directory, 0o700)
        payload = (BASE / "hostinger/cas.php").read_bytes()
        destination = root + "/.bin/cas.php"
        sftp.putfo(io.BytesIO(payload), destination + ".tmp", file_size=len(payload))
        sftp.chmod(destination + ".tmp", 0o600)
        sftp.posix_rename(destination + ".tmp", destination)
        save_local(dict(SFTP_ROOT=root, SFTP_HOST_KEY_SHA256=key, SFTP_PHP_BIN=php))
        sftp.close()
        print(json.dumps(dict(estado="listo", carpeta_privada=root, huella=key, php=php), ensure_ascii=False, indent=2))
    finally:
        client.close()


def upload(directory, dataset):
    if not dataset or "/" in dataset or ".." in dataset:
        raise ValueError("Dataset no válido")
    store = SFTPStore()
    try:
        for phase in ["companias", "personas"]:
            json.loads((directory / f"manifest_{phase}.json").read_text())
        paths = sorted(p for p in directory.rglob("*") if p.is_file())
        # Los manifiestos se confirman al final, después de todos los archivos.
        paths.sort(key=lambda p: p.name.startswith("manifest_"))
        for path in paths:
            relative = path.relative_to(directory).as_posix()
            store.put(f"runs/{dataset}/{relative}", path.read_bytes())
        print(f"Subidos {len(paths)} archivos privados para {dataset}")
    finally:
        store.close()


def secret(project, account, name):
    config = settings()
    prefix = ["gcloud", "--project", project]
    exists = subprocess.run(prefix + ["secrets", "describe", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    command = prefix + (["secrets", "versions", "add", name] if exists else ["secrets", "create", name, "--replication-policy=automatic"])
    subprocess.run(command + ["--data-file=-"], input=config["FTP_PASSWORD"].encode(), check=True)
    subprocess.run(prefix + ["secrets", "add-iam-policy-binding", name,
                             "--member", "serviceAccount:" + account,
                             "--role", "roles/secretmanager.secretAccessor"], check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["init", "upload", "secret"])
    p.add_argument("--directory", type=Path)
    p.add_argument("--dataset")
    p.add_argument("--project")
    p.add_argument("--service-account")
    p.add_argument("--secret-name", default="iess-sftp-password")
    a = p.parse_args()
    if a.action == "init":
        initialize()
    elif a.action == "upload":
        if not a.directory or not a.dataset: p.error("upload requiere --directory y --dataset")
        upload(a.directory, a.dataset)
    else:
        if not a.project or not a.service_account: p.error("secret requiere proyecto y cuenta de servicio")
        secret(a.project, a.service_account, a.secret_name)


if __name__ == "__main__":
    main()
