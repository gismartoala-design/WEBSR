"""Lee configuración local sin ejecutar los valores del archivo .env."""
import os
from pathlib import Path


def read_env(path):
    result = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key.strip()] = value
    return result


def settings():
    base = Path(__file__).resolve().parents[1]
    result = read_env(base / "cloudrun/.env")
    result.update(read_env(base / ".env"))
    result.update(os.environ)
    return result
