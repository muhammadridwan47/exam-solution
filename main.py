#!/usr/bin/env python3
"""exam-solution: screenshot layar -> OCR lokal -> LLM -> notifikasi Telegram.

Semua konfigurasi model ada di .env sebagai array LLM.<n>.*, lihat .env.example.
Jalankan `python main.py --list` untuk melihat binding hotkey yang aktif.
"""

import argparse
import base64
import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import List, Optional

from PIL import Image
from pynput import keyboard

from config import ConfigError, LLMConfig, load_llms
from providers import build_provider
from providers.telegram import TelegramNotifier

SYSTEM_PROMPT = (
    "Kamu adalah sistem pemecah soal ujian presisi tinggi. "
    "ATURAN MUTLAK:\n"
    "1. Gunakan teks soal yang dilampirkan. Gambar hanya untuk referensi diagram/tabel jika ada.\n"
    "2. Berikan format jawaban persis 4 baris tanpa basa-basi pengantar/penutup:\n"
    "📌 **Nomor:** [Nomor / '-']\n"
    "✅ **Jawaban Benar:** [Opsi huruf / nilai akhir]\n"
    "💡 **Alasan:** [Fakta inti kenapa opsi ini benar, maksimal 2 kalimat]\n"
    "📝 **Simpulan:** [1 kalimat rumus/kaidah utama]"
)

MAX_IMAGE_DIM = 1000
JPEG_QUALITY = 80
AUDIO_SAMPLE_RATE = 44100
WHISPER_MODEL = "base"


# ================= PIPELINE =================
class Solver:
    """Satu instance per binding LLM; menjalankan satu tugas pada satu waktu."""

    def __init__(
        self,
        config: LLMConfig,
        notifier: Optional[TelegramNotifier],
        dry_run: bool = False,
    ):
        self.config = config
        self.notifier = notifier
        self.dry_run = dry_run
        self._lock = threading.Lock()
        self._busy = False

    def capture(self, workdir: str) -> str:
        path = os.path.join(workdir, "screenshot.png")
        subprocess.run(["/usr/sbin/screencapture", "-x", path], check=True)
        if not os.path.exists(path):
            raise RuntimeError("screencapture tidak menghasilkan file")
        return path

    def extract_text(self, image_path: str) -> str:
        from ocrmac import ocrmac

        annotations = ocrmac.OCR(image_path).recognize()
        raw = "\n".join(text for text, confidence, _ in annotations if confidence > 0.5)
        return re.sub(r"[ \t]+", " ", raw).strip()

    def compress_image(self, image_path: str) -> str:
        with Image.open(image_path) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            return base64.b64encode(buffer.getvalue()).decode("ascii")

    def build_prompt(self, ocr_text: str, transcript: str) -> str:
        parts = [
            "Berikut adalah teks soal yang berhasil diekstrak:",
            f'"""\n{ocr_text}\n"""',
        ]
        if transcript:
            parts.append(f"[AUDIO / APA YANG DIDENGAR]: {transcript}")
        parts.append(
            "Selesaikan soal tersebut berdasarkan teks di atas. "
            "Cek gambar lampiran jika terdapat diagram atau tabel pelengkap."
        )
        return "\n\n".join(parts)

    def ask_llm(self, prompt: str, image_b64: str):
        provider = build_provider(self.config)
        return provider.complete(prompt=prompt, image_b64=image_b64, system=SYSTEM_PROMPT)

    def run(self, audio_path: Optional[str] = None) -> bool:
        with self._lock:
            if self._busy:
                print("⏳ Masih memproses, permintaan diabaikan.")
                return False
            self._busy = True

        started = time.time()
        workdir = tempfile.mkdtemp(prefix="exam-solution-")
        try:
            print("📸 [1/4] Mengambil screenshot...")
            image_path = self.capture(workdir)

            # Notifikasi & STT sengaja SETELAH screenshot: keduanya memblokir
            # (jaringan/waktu), dan layar bisa sudah berubah sebelum capture.
            transcript = transcribe(audio_path) if audio_path else ""
            if transcript:
                print(f"🗣️ [STT] '{transcript}'")

            self._notify(
                f"⏳ *Sedang Diproses* ({self.config.label})\nAI sedang menganalisis soal..."
            )

            print("🔍 [2/4] OCR lokal & kompresi gambar...")
            ocr_started = time.time()
            ocr_text = self.extract_text(image_path)
            image_b64 = self.compress_image(image_path)
            ocr_elapsed = time.time() - ocr_started
            print(f"     OCR {len(ocr_text)} karakter, {len(image_b64) // 1024} KiB base64")

            prompt = self.build_prompt(ocr_text, transcript)

            if self.dry_run:
                print("\n--- DRY RUN (tanpa LLM/Telegram) ---")
                print(f"endpoint : {build_provider(self.config).endpoint}")
                print(f"model    : {self.config.model}")
                print(f"prompt   :\n{prompt}\n")
                print(f"--- selesai dalam {time.time() - started:.1f}s ---")
                return True

            print(f"🤖 [3/4] Menghubungi {self.config.name} ({self.config.model})...")
            completion = self.ask_llm(prompt, image_b64)

            print("✅ [4/4] Mengirim hasil ke Telegram...")
            footer = (
                f"\n\n---\n🤖 **Model:** `{completion.model}`"
                f"\n⏱ **Waktu:** `{time.time() - started:.1f} detik`"
                f" (OCR {ocr_elapsed:.1f}s, AI {completion.elapsed:.1f}s)"
            )
            self._notify(completion.text + footer)

            if audio_path and self.notifier:
                self.notifier.send_voice(audio_path, caption=f"🎙️ Terbaca: {transcript}")
            return True

        except Exception as exc:  # pipeline latar: error apa pun harus dilaporkan, bukan mematikan listener
            print(f"❌ Error: {exc}")
            self._notify(f"❌ *Gagal* ({self.config.label}): {exc}")
            return False
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            with self._lock:
                self._busy = False

    def _notify(self, text: str) -> None:
        if self.notifier and not self.dry_run:
            self.notifier.send_message(text)


# ================= AUDIO =================
_whisper = None
_whisper_lock = threading.Lock()
def load_wav(path: str, target_rate: int = 16000):
    """Baca WAV mono 16-bit apa pun lalu resample ke 16 kHz (tanpa ffmpeg)."""
    import numpy as np
    from scipy.io.wavfile import read as read_wav
    from scipy.signal import resample_poly

    sample_rate, data = read_wav(path)
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float32) / 32768.0
    if sample_rate != target_rate:
        gcd = np.gcd(sample_rate, target_rate)
        data = resample_poly(data, target_rate // gcd, sample_rate // gcd)
    return data.astype(np.float32)


def transcribe(audio_path: str) -> str:
    global _whisper
    import whisper

    with _whisper_lock:
        if _whisper is None:
            print("🔄 Memuat model Whisper...")
            _whisper = whisper.load_model(
                WHISPER_MODEL, download_root=os.path.expanduser("~/.cache/whisper")
            )
    result = _whisper.transcribe(load_wav(audio_path), fp16=False)
    return result.get("text", "").strip()

class AudioRecorder:
    """Toggle rekam suara: start() saat hotkey pertama, stop() saat kedua."""

    def __init__(self, notifier: Optional[TelegramNotifier]):
        self.notifier = notifier
        self._stream = None
        self._frames: List = []
        self._path: Optional[str] = None

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def start(self) -> bool:
        import sounddevice as sd

        self._frames = []
        handle = tempfile.NamedTemporaryFile(
            prefix="exam-solution-", suffix=".wav", delete=False
        )
        handle.close()
        self._path = handle.name

        def callback(indata, frames, time_info, status):
            if status:
                print(f"⚠️ Status audio: {status}")
            self._frames.append(indata.copy())

        stream = sd.InputStream(
            samplerate=AUDIO_SAMPLE_RATE, channels=1, dtype="int16", callback=callback
        )
        stream.start()
        self._stream = stream
        print("🎙️ Merekam suara... tekan hotkey lagi untuk berhenti & memproses.")
        if self.notifier:
            self.notifier.send_message(
                "🎙️ *Sedang Merekam Audio*\nTekan hotkey lagi untuk memproses."
            )
        return True

    def stop(self) -> Optional[str]:
        if not self.recording:
            return None

        import numpy as np
        from scipy.io.wavfile import write

        self._stream.stop()
        self._stream.close()
        self._stream = None

        if self._frames:
            write(self._path, AUDIO_SAMPLE_RATE, np.concatenate(self._frames, axis=0))
        self._frames = []
        print("🎙️ Rekaman berhenti.")
        return self._path


class AudioToggle:
    """State rekaman global; hanya satu rekaman aktif untuk semua binding."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: Optional[Solver] = None
        self._recorder: Optional[AudioRecorder] = None

    def press(self, solver: Solver) -> None:
        with self._lock:
            if self._recorder is None:
                recorder = AudioRecorder(solver.notifier)
                try:
                    recorder.start()
                except Exception as exc:
                    print(f"❌ Gagal mulai rekaman: {exc}")
                    return
                self._owner = solver
                self._recorder = recorder
                return
            if self._owner is not solver:
                print("⏳ Rekaman lain sedang berjalan.")
                return
            recorder = self._recorder
            self._recorder = None
            self._owner = None

        audio_path = recorder.stop()

        def process():
            try:
                solver.run(audio_path)
            finally:
                if audio_path:
                    os.remove(audio_path)

        threading.Thread(target=process, daemon=True).start()


# ================= HOTKEY =================
_MODIFIER_ALIASES = {
    "ctrl": "<ctrl>",
    "control": "<ctrl>",
    "alt": "<alt>",
    "option": "<alt>",
    "opt": "<alt>",
    "shift": "<shift>",
    "cmd": "<cmd>",
    "command": "<cmd>",
    "super": "<cmd>",
    "win": "<cmd>",
}


def normalize_binding(binding: str) -> str:
    """'ctrl+1' -> '<ctrl>+1', 'f6' -> '<f6>', '6' -> '6' (format HotKey.parse)."""
    raw_parts = binding.split("+")
    if any(not part.strip() for part in raw_parts):
        raise ConfigError(f"BINDING_KEY tidak valid: {binding!r}")
    parts = [part.strip().lower() for part in raw_parts]

    normalized = []
    for part in parts:
        if part in _MODIFIER_ALIASES:
            normalized.append(_MODIFIER_ALIASES[part])
        elif len(part) == 1:
            normalized.append(part)
        elif part in keyboard.Key.__members__:
            normalized.append(f"<{part}>")
        else:
            raise ConfigError(f"BINDING_KEY tidak dikenal: {part!r} pada {binding!r}")

    candidate = "+".join(normalized)
    try:
        keyboard.HotKey.parse(candidate)
    except ValueError as exc:
        raise ConfigError(f"BINDING_KEY tidak valid: {binding!r} -> {candidate!r}") from exc
    return candidate


# ================= CLI =================
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Screenshot -> OCR -> LLM -> Telegram.")
    parser.add_argument("--list", action="store_true", help="tampilkan binding dari .env lalu keluar")
    parser.add_argument("--run", type=int, metavar="N", help="jalankan pipeline sekali untuk LLM.N")
    parser.add_argument("--audio", metavar="FILE", help="transkripsi file audio lalu jalankan pipeline")
    parser.add_argument("--dry-run", action="store_true", help="jalankan OCR/kompresi tanpa LLM & Telegram")
    parser.add_argument("--env", default=".env", help="path file .env (default: .env)")
    return parser.parse_args(argv)


_COLUMNS = (("name", "NAME", 12), ("binding", "BINDING", 16),
           ("action", "AKSI", 12), ("model", "MODEL", 28), ("host", "ENDPOINT", 0))



def describe(config: LLMConfig) -> List[str]:
    values = {
        "name": config.name,
        "binding": normalize_binding(config.binding),
        "action": "rekam suara" if config.audio else "screenshot",
        "model": config.model,
        "host": config.host,
    }
    return [values[key] for key, _, _ in _COLUMNS]


def format_row(cells: List[str]) -> str:
    return "  ".join(value.ljust(width) for value, (_, _, width) in zip(cells, _COLUMNS)).rstrip()



def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        configs = load_llms(args.env)
        for config in configs:
            normalize_binding(config.binding)
    except ConfigError as exc:
        print(f"❌ Konfigurasi: {exc}")
        return 2

    if args.list:
        print(format_row([header for _, header, _ in _COLUMNS]))
        for config in configs:
            print(format_row(describe(config)))
        return 0

    notifier = None
    if not args.dry_run:
        try:
            notifier = TelegramNotifier.from_env()
        except RuntimeError as exc:
            print(f"❌ {exc}")
            return 2

    solvers = {config.index: Solver(config, notifier, dry_run=args.dry_run) for config in configs}

    if args.audio and args.run is None:
        print("❌ --audio hanya berlaku bersama --run N")
        return 2

    if args.run is not None:
        solver = solvers.get(args.run)
        if solver is None:
            available = ", ".join(str(index) for index in solvers)
            print(f"❌ LLM.{args.run} tidak ada. Tersedia: {available}")
            return 2
        return 0 if solver.run(args.audio) else 1

    os.system("clear" if os.name == "posix" else "cls")
    print("=" * 70)
    print("🚀 EXAM SOLVER — screenshot, OCR, LLM, Telegram")
    print("=" * 70)
    for config in configs:
        print(f"  {format_row(describe(config))}")
    print("=" * 70)
    print("Tekan binding di atas. CTRL+C untuk keluar.")

    toggle = AudioToggle()
    hotkeys = {}
    for config in configs:
        solver = solvers[config.index]
        audio = config.audio

        def handler(solver=solver, audio=audio):
            if audio:
                toggle.press(solver)
            else:
                threading.Thread(target=solver.run, daemon=True).start()

        hotkeys[normalize_binding(config.binding)] = handler

    with keyboard.GlobalHotKeys(hotkeys) as listener:
        listener.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
