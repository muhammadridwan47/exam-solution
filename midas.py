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

# Muat variabel & Sembunyikan warning
load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================= KONFIGURASI =================
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
AI_MODEL = os.getenv("MIDAS_MODEL")
API_KEY = os.getenv("API_KEY")
API_URL = os.getenv("MIDAS_URL")
TEMP_IMG = "temp_midas_screenshot.png"
COMPRESS_IMG = "temp_midas_compressed.jpg"

IS_PROCESSING = False
LAST_RUN = 0.0  # Penanda waktu (cooldown)
LOCK = threading.Lock()

try:
    from ocrmac import ocrmac
except ImportError:
    sys.exit("[ERROR] Install ocrmac dulu: pip install ocrmac")

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
    except:
        return None, time.time() - t0, AI_MODEL

def proses_gambar(img_path):
    try:
        with Image.open(img_path) as img:
            img = img.convert("RGB") if img.mode in ("RGBA", "P") else img
            img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            img.save(COMPRESS_IMG, "JPEG", quality=80)
        with open(COMPRESS_IMG, "rb") as f:
            return base64.b64encode(f.read()).decode('utf-8')
    except: return None

def bersihkan_file():
    for f in [TEMP_IMG, COMPRESS_IMG]:
        if os.path.exists(f): os.remove(f)

# ================= LOGIKA UTAMA AI =================
def dapatkan_jawaban_ai(teks_ocr, img_b64):
    prompt = f"""Jawab soal berikut HANYA berdasarkan gambar dan teks OCR ini. Jangan mengarang.
[TEKS OCR]: {teks_ocr}

Format Jawaban:
🔢 **Nomor Soal:** [No]
✅ **Jawaban Benar:** [Pilihan A/B/C/D]
📝 **Soal:** [Tulis ulang soal dengan rapi]
💡 **Penjelasan:** [Alasan singkat]"""

    print(f"  → Mencoba {AI_MODEL}...")
    jawaban, durasi, actual_model = tembak_api(buat_payload(prompt, img_b64, AI_MODEL))
    return jawaban, durasi, actual_model

# ================= PROSES UTAMA =================
def eksekusi_tugas():
    global IS_PROCESSING
    print("\n📸 [1/4] Mengambil screenshot...")
    subprocess.run(["screencapture", "-x", TEMP_IMG])
    
    if not os.path.exists(TEMP_IMG):
        print("❌ Gagal screenshot!"); bersihkan_file(); unlock(); return

    kirim_telegram("⏳ *Sedang Diproses*\nAI sedang menganalisis soal...")

    try:
        print("🔍 [2/4] Ekstraksi OCR & Kompresi...")
        t0 = time.time()
        ocr_text = "\n".join([t for t, _, _ in ocrmac.OCR(TEMP_IMG).recognize()])
        img_b64 = proses_gambar(TEMP_IMG)
        durasi_ocr = time.time() - t0

        if not img_b64: raise Exception("Gagal kompresi gambar")

        print("🤖 [3/4] Menganalisis dengan AI...")
        jawaban, durasi_api, model_dipakai = dapatkan_jawaban_ai(ocr_text, img_b64)

        if not jawaban:
            raise Exception(f"Model {AI_MODEL} gagal memproses soal.")

        print(f"✅ [4/4] Selesai! Mengirim ke Telegram...")
        meta = f"\n\n📊 *Meta:* `{model_dipakai}` | OCR: {durasi_ocr:.1f}s | AI: {durasi_api:.1f}s"
        kirim_telegram(f"✅ *HASIL ANALISIS*\n\n{jawaban}{meta}")

    except Exception as e:
        print(f"❌ Error: {e}")
        kirim_telegram(f"❌ *Gagal:* {e}")
    finally:
        bersihkan_file()
        unlock()
        print("-" * 40 + "\n🟢 Standby. Tekan '6' untuk soal berikutnya.")

def unlock():
    global IS_PROCESSING
    with LOCK: IS_PROCESSING = False

def on_press(key):
    global IS_PROCESSING, LAST_RUN
    try:
        if key.char == '6':
            with LOCK:
                # Cek flag ATAU jika belum lewat 3 detik dari penekanan terakhir (Cooldown)
                if IS_PROCESSING or (time.time() - LAST_RUN < 3.0): 
                    return
                IS_PROCESSING = True
                LAST_RUN = time.time()
            
            threading.Thread(target=eksekusi_tugas, daemon=True).start()
    except: pass

if __name__ == "__main__":
    os.system('clear' if os.name == 'posix' else 'cls')
    print("=" * 45 + f"\n🚀 STEALTH SOLVER AKTIF\n🤖 Model Utama: {AI_MODEL}\n" + "=" * 45)
    print("🟢 Tekan '6' untuk screenshot. CTRL+C untuk keluar.")
    with keyboard.Listener(on_press=on_press) as listener: listener.join()