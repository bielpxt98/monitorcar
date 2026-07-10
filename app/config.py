"""Configuração via variáveis de ambiente / arquivo .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# carrega .env manualmente (sem pydantic)
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _load_dotenv(path: Path = _ENV_PATH) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "sim")


@dataclass
class Settings:
    sitrax_url: str = os.getenv(
        "SITRAX_URL",
        "https://sitrax.sitacom.com.br/site/login/?l=01339796492",
    )
    sitrax_cliente: str = os.getenv("SITRAX_CLIENTE", "")
    sitrax_usuario: str = os.getenv("SITRAX_USUARIO", "")
    sitrax_senha: str = os.getenv("SITRAX_SENHA", "")
    sitrax_headless: bool = _bool(os.getenv("SITRAX_HEADLESS"), True)
    port: int = int(os.getenv("PORT", "8000"))


settings = Settings()
