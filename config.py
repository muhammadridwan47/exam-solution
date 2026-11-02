"""Konfigurasi LLM berbasis array dari .env.

Format:
    LLM.<n>.HOST          base URL (…/v1) atau endpoint penuh (…/v1/chat/completions)
    LLM.<n>.KEY           bearer token (opsional untuk endpoint lokal)
    LLM.<n>.MODEL         nama model
    LLM.<n>.BINDING_KEY   hotkey global, mis. 6, ctrl+1, cmd+shift+2
    LLM.<n>.NAME          label opsional
    LLM.<n>.AUDIO         true = hotkey toggle rekam suara -> Whisper -> pipeline
    LLM.<n>.TEMPERATURE   opsional, default 0.1
    LLM.<n>.MAX_TOKENS    opsional, default tidak dikirim
    LLM.<n>.TIMEOUT       opsional, default 120 detik
"""

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from dotenv import load_dotenv

_PATTERN = re.compile(r"^LLM\.(\d+)\.([A-Z_]+)$")
_REQUIRED = ("HOST", "MODEL", "BINDING_KEY")
_TRUE = {"1", "true", "yes", "on"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    index: int
    host: str
    model: str
    binding: str
    name: str
    key: str = ""
    audio: bool = False
    temperature: float = 0.1
    max_tokens: Optional[int] = None
    timeout: float = 120.0

    @property
    def label(self) -> str:
        return f"LLM.{self.index} ({self.name})"


def _as_bool(value: str) -> bool:
    return value.strip().lower() in _TRUE


def _as_float(value: str, default: float, field: str) -> float:
    if not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        raise ConfigError(f"{field} harus angka, bukan {value!r}")


def _as_optional_int(value: str, field: str) -> Optional[int]:
    if not value.strip():
        return None
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{field} harus integer, bukan {value!r}")


def load_llms(env_path: str = ".env") -> List[LLMConfig]:
    load_dotenv(env_path, override=False)

    raw: Dict[int, Dict[str, str]] = {}
    for name, value in os.environ.items():
        match = _PATTERN.match(name)
        if match:
            raw.setdefault(int(match.group(1)), {})[match.group(2)] = value.strip()

    if not raw:
        raise ConfigError(
            f"Tidak ada variabel LLM.<n>.* di {env_path}. "
            "Lihat .env.example untuk formatnya."
        )

    configs: List[LLMConfig] = []
    for index in sorted(raw):
        fields = raw[index]
        missing = [field for field in _REQUIRED if not fields.get(field)]
        if missing:
            raise ConfigError(f"LLM.{index}: kosong/tidak ada -> {', '.join(missing)}")

        configs.append(
            LLMConfig(
                index=index,
                host=fields["HOST"],
                key=fields.get("KEY", ""),
                model=fields["MODEL"],
                binding=fields["BINDING_KEY"],
                name=fields.get("NAME") or f"llm{index}",
                audio=_as_bool(fields.get("AUDIO", "")),
                temperature=_as_float(
                    fields.get("TEMPERATURE", ""), 0.1, f"LLM.{index}.TEMPERATURE"
                ),
                max_tokens=_as_optional_int(
                    fields.get("MAX_TOKENS", ""), f"LLM.{index}.MAX_TOKENS"
                ),
                timeout=_as_float(fields.get("TIMEOUT", ""), 120.0, f"LLM.{index}.TIMEOUT"),
            )
        )

    _reject_duplicates(configs, "binding", [c.binding.lower() for c in configs])
    _reject_duplicates(configs, "name", [c.name.lower() for c in configs])
    return configs


def _reject_duplicates(configs: List[LLMConfig], field: str, values: List[str]) -> None:
    seen: Dict[str, int] = {}
    for config, value in zip(configs, values):
        if value in seen:
            raise ConfigError(
                f"LLM.{seen[value]} dan LLM.{config.index} memakai {field} yang sama: {value!r}"
            )
        seen[value] = config.index
