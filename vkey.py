import ssl
import torch

# Bypass SSL global untuk urllib bawaan Python
ssl._create_default_https_context = ssl._create_unverified_context

# Patch kompatibilitas PyTorch 2.6+ (weights_only=False)
_original_torch_load = torch.load
def patched_torch_load(*args, **kwargs):
    kwargs['weights_only'] = False
    return _original_torch_load(*args, **kwargs)
torch.load = patched_torch_load

import requests
import base64
import urllib3
import sys
import time
import os
import subprocess
import threading
from pynput import keyboard
from dotenv import load_dotenv
from PIL import Image

# Library Audio
import sounddevice as sd
import numpy as np
from scipy.io.wavfile import write
import whisper

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================= KONFIGURASI =================
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
AI_MODEL = os.getenv("VKEY_MODEL")
API_KEY = os.getenv("VKEY_API_KEY")
API_URL = os.getenv("VKEY_URL")
TEMP_IMG = "temp_vkey_screenshot.png"
COMPRESS_IMG = "temp_vkey_compressed.jpg"
TEMP_AUDIO = "temp_vkey_voice.wav"

AUDIO_SAMPLE_RATE = 44100

IS_PROCESSING = False
IS_RECORDING = False
AUDIO_STREAM = None
RECORDED_FRAMES = []
LAST_RUN = 0.0
LOCK = threading.Lock()

try:
    from ocrmac import ocrmac
except ImportError:
    sys.exit("[ERROR] Install ocrmac terlebih dahulu: pip install ocrmac")

print("🔄 Memuat model Whisper...")
WHISPER_MODEL = whisper.load_model("base", download_root=os.path.expanduser("~/.cache/whisper"))
print("✅ Model Whisper siap digunakan!")

# ================= HELPER FUNCTIONS =================
def kirim_telegram(pesan, parse_mode="Markdown"):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": pesan, "parse_mode": parse_mode},
            timeout=10
        )
        if resp.status_code != 200:
            # Fallback: kirim tanpa parse_mode jika Markdown gagal
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": pesan},
                timeout=10
            )
    except Exception as e:
        print(f"⚠️ Gagal kirim Telegram: {e}")

def kirim_voice_telegram(audio_path, caption=""):
    try:
        with open(audio_path, 'rb') as audio_file:
            files = {'voice': audio_file}
            data = {'chat_id': TG_CHAT_ID, 'caption': caption}
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendVoice",
                data=data,
                files=files,
                timeout=30
            )
    except Exception as e:
        print(f"❌ Gagal mengirim voice: {e}")

def callback_audio(indata, frames, time_info, status):
    global RECORDED_FRAMES
    if status:
        print(f"⚠️ Status Audio: {status}")
    RECORDED_FRAMES.append(indata.copy())

def mulai_rekam():
    global IS_RECORDING, AUDIO_STREAM, RECORDED_FRAMES
    RECORDED_FRAMES = []
    try:
        AUDIO_STREAM = sd.InputStream(samplerate=AUDIO_SAMPLE_RATE, channels=1, dtype='int16', callback=callback_audio)
        AUDIO_STREAM.start()
        IS_RECORDING = True
        kirim_telegram("🎙️ *Sedang Merekam Audio*\nTekan *0* jika ingin berhenti & proses.")
        print("\n🎙️ [AUDIO] Merekam suara... (Tekan '0' untuk berhenti dan memproses)")
    except Exception as e:
        print(f"❌ Gagal memulai rekaman: {e}")
        unlock()

def berhenti_rekam_dan_proses():
    global IS_RECORDING, AUDIO_STREAM, RECORDED_FRAMES
    try:
        if AUDIO_STREAM:
            AUDIO_STREAM.stop()
            AUDIO_STREAM.close()
            AUDIO_STREAM = None
        IS_RECORDING = False
        print("🎙️ [AUDIO] Rekaman berhenti. Menyimpan file audio...")

        if RECORDED_FRAMES:
            audio_data = np.concatenate(RECORDED_FRAMES, axis=0)
            write(TEMP_AUDIO, AUDIO_SAMPLE_RATE, audio_data)
        
        threading.Thread(target=eksekusi_tugas, args=(True,), daemon=True).start()
    except Exception as e:
        print(f"❌ Gagal menghentikan rekaman: {e}")
        unlock()

def transkripsi_audio(audio_path):
    try:
        print("🗣️ [STT] Menerjemahkan suara ke teks dengan Whisper...")
        result = WHISPER_MODEL.transcribe(audio_path)
        teks_hasil = result.get("text", "").strip()
        print(f"🗣️ [STT Hasil]: '{teks_hasil}'")
        return teks_hasil
    except Exception as e:
        print(f"❌ Gagal transkripsi audio: {e}")
        return ""

def buat_payload(teks, img_b64, model):
    content = [{"type": "text", "text": teks}]
    if img_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})
    return {"model": model, "messages": [{"role": "user", "content": content}], "temperature": 0.1}

def tembak_api(payload):
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    t0 = time.time()
    try:
        res = requests.post(API_URL, headers=headers, json=payload, verify=False, timeout=60)
        if res.status_code == 200:
            data = res.json()
            content = data['choices'][0]['message']['content']
            actual_model = data.get("model", AI_MODEL)
            return content, time.time() - t0, actual_model
        return None, time.time() - t0, AI_MODEL
    except Exception:
        return None, time.time() - t0, AI_MODEL

def proses_gambar(img_path):
    try:
        with Image.open(img_path) as img:
            img = img.convert("RGB") if img.mode in ("RGBA", "P") else img
            img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            img.save(COMPRESS_IMG, "JPEG", quality=80)
        with open(COMPRESS_IMG, "rb") as f:
            return base64.b64encode(f.read()).decode('utf-8')
    except Exception:
        return None

def bersihkan_file():
    for f in [TEMP_IMG, COMPRESS_IMG, TEMP_AUDIO]:
        if os.path.exists(f):
            os.remove(f)

# ================= LOGIKA UTAMA AI =================
def dapatkan_jawaban_ai(teks_ocr, img_b64, transkripsi_suara=""):
    info_suara = f"\n[AUDIO / APA YANG DIDENGAR]: {transkripsi_suara}" if transkripsi_suara else ""

    prompt = f"""Jawab soal berikut secara akurat berdasarkan gambar, teks OCR, dan transkripsi suara audio yang terdengar.
[TEKS OCR DARI LAYAR]: {teks_ocr}
{info_suara}

Format Jawaban:
🔢 **Nomor Soal:** [No]
✅ **Jawaban Benar:** [Pilihan teks/karakter yang tepat sesuai audio]
📝 **Soal:** [Tulis ulang soal dengan rapi]
💡 **Penjelasan:** [Alasan singkat mencocokkan audio yang terdengar dengan pilihan yang ada]"""

    print(f"  → Mencoba {AI_MODEL}...")
    jawaban, durasi, actual_model = tembak_api(buat_payload(prompt, img_b64, AI_MODEL))
    return jawaban, durasi, actual_model

# ================= PROSES UTAMA =================
def eksekusi_tugas(pakai_audio=False):
    print("\n📸 [1/4] Mengambil screenshot...")
    subprocess.run(["screencapture", "-x", TEMP_IMG])
    
    if not os.path.exists(TEMP_IMG):
        print("❌ Gagal mengambil screenshot!")
        bersihkan_file()
        unlock()
        return

    status_teks = "Merekam, Menerjemahkan Audio & Menganalisis..." if pakai_audio else "Menganalisis soal..."
    kirim_telegram(f"⏳ *Sedang Diproses*\n{status_teks}")

    try:
        teks_suara = ""
        if pakai_audio and os.path.exists(TEMP_AUDIO):
            teks_suara = transkripsi_audio(TEMP_AUDIO)

        print("🔍 [2/4] Ekstraksi OCR & Kompresi Gambar...")
        t0 = time.time()
        ocr_text = "\n".join([t for t, _, _ in ocrmac.OCR(TEMP_IMG).recognize()])
        img_b64 = proses_gambar(TEMP_IMG)
        durasi_ocr = time.time() - t0

        if not img_b64:
            raise Exception("Gagal melakukan kompresi gambar")

        print("🤖 [3/4] Menganalisis dengan AI...")
        jawaban, durasi_api, model_dipakai = dapatkan_jawaban_ai(ocr_text, img_b64, transkripsi_suara=teks_suara)

        if not jawaban:
            raise Exception(f"Model {AI_MODEL} gagal memproses soal.")

        print("✅ [4/4] Selesai! Mengirim hasil ke Telegram...")
        meta = f"\n\n📊 *Meta:* `{model_dipakai}` | OCR: {durasi_ocr:.1f}s | AI: {durasi_api:.1f}s"
        
        kirim_telegram(f"✅ *HASIL ANALISIS*\n\n{jawaban}{meta}")
        
        if pakai_audio and os.path.exists(TEMP_AUDIO):
            kirim_voice_telegram(TEMP_AUDIO, caption=f"🎙️ Audio Asli (Terbaca: {teks_suara})")

    except Exception as e:
        print(f"❌ Error: {e}")
        kirim_telegram(f"❌ *Gagal:* {e}")
    finally:
        bersihkan_file()
        unlock()
        print("-" * 40 + "\n🟢 Standby. Tekan '8' (Screenshot saja) atau '9' & '0' (Rekam suara).")

def unlock():
    global IS_PROCESSING
    with LOCK:
        IS_PROCESSING = False

def on_press(key):
    global IS_PROCESSING, IS_RECORDING, LAST_RUN
    try:
        if key.char == '8':
            with LOCK:
                if IS_PROCESSING or IS_RECORDING or (time.time() - LAST_RUN < 1.0): 
                    return
                IS_PROCESSING = True
                LAST_RUN = time.time()
            threading.Thread(target=eksekusi_tugas, args=(False,), daemon=True).start()

        elif key.char == '9':
            with LOCK:
                if IS_PROCESSING or IS_RECORDING or (time.time() - LAST_RUN < 1.0): 
                    return
                IS_PROCESSING = True
                LAST_RUN = time.time()
            mulai_rekam()

        elif key.char == '0':
            with LOCK:
                if not IS_RECORDING: 
                    return
            berhenti_rekam_dan_proses()

    except AttributeError:
        pass
    except Exception: 
        pass

if __name__ == "__main__":
    os.system('clear' if os.name == 'posix' else 'cls')
    print("=" * 60 + f"\n🚀 STEALTH SOLVER + WHISPER STT AKTIF\n🤖 Model Utama: {AI_MODEL}\n" + "=" * 60)
    print("🟢 Tombol Kontrol:")
    print("   • Tekan '8'  -> Screenshot saja & Langsung Proses AI")
    print("   • Tekan '9'  -> Mulai Merekam Suara")
    print("   • Tekan '0'  -> Berhenti Rekam, Terjemahkan Audio & Proses AI")
    print("   • CTRL+C     -> Keluar Program")
    print("=" * 60)
    
    with keyboard.Listener(on_press=on_press) as listener: 
        listener.join()