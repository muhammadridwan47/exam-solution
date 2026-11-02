"""Client untuk endpoint /chat/completions bergaya OpenAI."""

import json
import time
from dataclasses import dataclass
from typing import Optional

import requests

# Beberapa gateway (mis. labs.playdev.online) memblokir User-Agent python bawaan
# requests/urllib3 dengan 403 "banned permanently". Pakai UA eksplisit.
USER_AGENT = "exam-solution/1.0"


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    elapsed: float


class OpenAICompatibleProvider:
    def __init__(
        self,
        host: str,
        key: str,
        model: str,
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        timeout: float = 120.0,
    ):
        self.host = host.rstrip("/")
        self.key = key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        if self.host.endswith("/chat/completions"):
            return self.host
        return f"{self.host}/chat/completions"

    def _payload(self, prompt: str, image_b64: Optional[str], system: Optional[str]) -> dict:
        content = [{"type": "text", "text": prompt}]
        if image_b64:
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
            )

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        return payload

    def complete(
        self,
        prompt: str,
        image_b64: Optional[str] = None,
        system: Optional[str] = None,
    ) -> Completion:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "text/event-stream, application/json",
            "User-Agent": USER_AGENT,
        }
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"

        started = time.time()
        try:
            response = requests.post(
                self.endpoint,
                headers=headers,
                json=self._payload(prompt, image_b64, system),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"Koneksi ke {self.host} gagal: {exc}") from exc

        elapsed = time.time() - started
        response.encoding = "utf-8"

        if response.status_code != 200:
            raise ProviderError(
                f"HTTP {response.status_code} dari {self.endpoint}: {response.text[:200]}"
            )

        data = _parse_body(response.text, response.headers.get("content-type", ""))
        text = _extract_text(data)
        if not text:
            raise ProviderError(f"Respons tanpa teks: {json.dumps(data)[:200]}")

        return Completion(text=text, model=data.get("model") or self.model, elapsed=elapsed)


def _parse_body(body: str, content_type: str) -> dict:
    """Sebagian server selalu membalas SSE walau stream tidak diminta, jadi
    body JSON tunggal maupun stream `data: {...}` harus sama-sama terbaca."""
    body = body.strip()
    if "text/event-stream" in content_type or body.startswith("data:"):
        return _merge_sse(body)

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass

    merged = _merge_sse(body)
    if merged["choices"] or "response" in merged or "message" in merged:
        return merged
    raise ProviderError(f"Respons bukan JSON valid: {body[:200]}")


def _merge_sse(body: str) -> dict:
    """Gabungkan seluruh chunk `data: {...}` menjadi satu objek gaya completion."""
    model = ""
    content = []
    thinking = []
    empty_chunks = 0

    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            payload = json.loads(chunk)
        except json.JSONDecodeError:
            continue

        model = model or payload.get("model") or ""
        empty_chunks += 1
        for choice in payload.get("choices") or []:
            delta = choice.get("message") or choice.get("delta") or {}
            content.append(delta.get("content") or "")
            # reasoning kadang menempel di level choice, bukan di dalam delta
            thinking.append(_thinking(delta) or _thinking(choice))

    if not empty_chunks:
        raise ProviderError(f"Stream tanpa data: {body[:200]}")

    return {
        "model": model,
        "choices": [
            {"message": {"content": "".join(content), "thinking": "".join(thinking)}}
        ],
    }


def _extract_text(data: dict) -> str:
    """Mendukung variasi respons: choices[].message.content, choices[].text,
    response, message.content, serta reasoning/thinking sebagai fallback."""
    text = ""
    thinking = ""

    choices = data.get("choices") or []
    if choices:
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or choice.get("text") or ""
        thinking = _thinking(message)
    elif "response" in data:
        text = data["response"]
    elif "message" in data:
        message = data["message"]
        text = message.get("content", "")
        thinking = _thinking(message)

    text = str(text or "").strip()
    if not text:
        text = str(thinking or "").strip()
    return text


def _thinking(message: dict) -> str:
    """Model reasoning bisa menaruh hasil berpikir di field berbeda."""
    direct = message.get("thinking") or message.get("reasoning_content")
    if direct:
        return direct
    extra = message.get("provider_specific_fields") or {}
    return extra.get("reasoning_content") or ""
