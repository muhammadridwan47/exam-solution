import base64
import io
import os
import subprocess
import requests
import re
import time
from PIL import Image
from pynput import keyboard
from ocrmac import ocrmac  # Library untuk Apple Native OCR
from dotenv import load_dotenv

# Muat variabel dari file .env
load_dotenv()

# ================= KONFIGURASI =================
IS_DEBUG = False  # Disarankan True agar Anda bisa melihat log JSON jika terjadi error

KETON_URL = os.getenv("KETON_URL")
KETON_API_KEY = os.getenv("KETON_API_KEY")
KETON_MODEL = os.getenv("KETON_MODEL")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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

TEMP_IMAGE = "temp_keton_capture.png"
# ===============================================

is_processing = False


def log(message):
    if IS_DEBUG:
        print(message)


def send_telegram_message(text, parse_mode="Markdown"):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        res = requests.post(url, json=payload, timeout=10)
        # Fallback Plain Text jika pengiriman Markdown gagal
        if res.status_code != 200 and parse_mode:
            payload.pop("parse_mode", None)
            requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log(f"[-] Telegram Error: {e}")


def extract_text_locally(image_path):
    """Mengekstrak teks 100% akurat & gratis menggunakan Apple Vision API."""
    try:
        annotations = ocrmac.OCR(image_path).recognize()
        # Menggunakan '\n' agar struktur baris (opsi A, B, C, D) tidak menyatu menjadi 1 baris
        raw_text = "\n".join([item[0] for item in annotations if item[1] > 0.5])
        
        # Bersihkan spasi ganda, tapi biarkan struktur baris (enter) tetap ada
        clean_text = re.sub(r'[ \t]+', ' ', raw_text)
        return clean_text.strip()
    except Exception as e:
        log(f"[-] Gagal OCR Lokal: {e}")
        return ""


def prepare_compressed_image(image_path, max_dim=1000):
    """
    Mengompresi gambar menjadi thumbnail kecil. 
    Memangkas Vision Token secara ekstrem karena teks sudah dibaca OCR.
    """
    with Image.open(image_path) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")
        
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=80, optimize=True)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")


def process_screen():
    global is_processing
    if is_processing:
        return

    is_processing = True
    start_time = time.time()
    try:
        log("\n[+] Mengambil screenshot (Tombol 0)...")
        subprocess.run(["/usr/sbin/screencapture", "-x", TEMP_IMAGE], check=True)

        if not os.path.exists(TEMP_IMAGE):
            log("[-] Gagal mengambil screenshot.")
            return

        send_telegram_message("⏳ *Ekstraksi teks lokal & analisis diagram...*")

        # 1. OCR Lokal
        log("[+] Menjalankan Apple Native OCR...")
        ocr_text = extract_text_locally(TEMP_IMAGE)
        log(f"[+] Teks terdeteksi:\n{ocr_text}\n")

        # 2. Kompresi Gambar
        image_base64 = prepare_compressed_image(TEMP_IMAGE, max_dim=1000)

        hybrid_prompt = (
            f"Berikut adalah teks soal yang berhasil diekstrak:\n"
            f"\"\"\"\n{ocr_text}\n\"\"\"\n\n"
            f"Selesaikan soal tersebut berdasarkan teks di atas. "
            f"Cek gambar lampiran jika terdapat diagram atau tabel pelengkap."
        )

        # 3. Payload untuk Keton API (OpenAI format)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user", 
                "content": [
                    {"type": "text", "text": hybrid_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}"
                        }
                    }
                ]
            }
        ]

        payload = {
            "model": KETON_MODEL,
            "messages": messages,
            "temperature": 0.1,
            "top_p": 0.5,
            "max_tokens": 1000
        }

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {KETON_API_KEY}"
        }

        log(f"[+] Mengirim ke API Server ({KETON_MODEL})...")
        response = requests.post(KETON_URL, json=payload, headers=headers, timeout=120)
        
        # Set proper encoding
        response.encoding = 'utf-8'
        
        log(f"[DEBUG] Response Status: {response.status_code}")
        log(f"[DEBUG] Response Headers: {dict(response.headers)}")
        log(f"[DEBUG] Raw Response Text: {response.text[:500]}...")  # First 500 chars
        
        if response.status_code != 200:
            send_telegram_message(f"❌ Gagal API (Status {response.status_code})\nResponse: {response.text[:200]}")
            log(f"[-] Error Body: {response.text}")
            return

        # 4. Universal JSON Parsing & Ekstraksi Teks
        try:
            # Clean response text - remove streaming artifacts
            response_text = response.text.strip()
            
            # Remove "data: [DONE]" or similar streaming terminators
            if "data: [DONE]" in response_text:
                response_text = response_text.split("data: [DONE]")[0].strip()
            
            # Remove any trailing newlines or extra data
            lines = response_text.split('\n')
            json_line = ""
            for line in lines:
                line = line.strip()
                if line.startswith('{') and line.endswith('}'):
                    json_line = line
                    break
            
            if json_line:
                response_text = json_line
            
            log(f"[DEBUG] Cleaned response text: {response_text[:200]}...")
            
            import json
            res_json = json.loads(response_text)
            log(f"[DEBUG RAW JSON] {res_json}")
        except Exception as json_err:
            log(f"[-] Gagal parse JSON: {json_err}")
            log(f"[-] Raw response text: {response.text}")
            send_telegram_message(f"⚠️ Server tidak mengembalikan data JSON yang valid.\nStatus: {response.status_code}\nResponse: {response.text[:200]}")
            return

        ai_text = ""
        thinking_text = ""

        # Prioritas parsing untuk format OpenAI (Keton API)
        if "choices" in res_json and len(res_json["choices"]) > 0:
            choice = res_json["choices"][0]
            if "message" in choice and "content" in choice["message"]:
                ai_text = choice["message"]["content"]
                thinking_text = choice["message"].get("thinking", "")
                log(f"[+] Berhasil extract dari choices[0].message.content")
            elif "text" in choice:
                ai_text = choice["text"]
                log(f"[+] Berhasil extract dari choices[0].text")
        elif "response" in res_json:
            ai_text = res_json["response"]
            log(f"[+] Berhasil extract dari response")
        elif "message" in res_json:
            ai_text = res_json["message"].get("content", "")
            thinking_text = res_json["message"].get("thinking", "")
            log(f"[+] Berhasil extract dari message.content")

        ai_text = str(ai_text).strip()
        thinking_text = str(thinking_text).strip()

        log(f"[DEBUG] Extracted ai_text length: {len(ai_text)}")
        log(f"[DEBUG] ai_text preview: {ai_text[:100]}...")

        # Fallback
        if not ai_text and thinking_text:
            log("[-] 'content' kosong, menggunakan data 'thinking' sebagai fallback.")
            ai_text = thinking_text

        if not ai_text:
            log("[-] DATA JSON SERVER: " + str(res_json))
            send_telegram_message("⚠️ AI merespons tetapi tidak ada teks yang dikembalikan.")
            return

        log(f"[+] Output Berhasil:\n{ai_text}")

        # Menambahkan footer waktu dan model
        elapsed_time = round(time.time() - start_time, 2)
        
        # Extract actual model name from API response
        actual_model = res_json.get("model", KETON_MODEL)  # Fallback to env variable if not found
        
        footer = f"\n\n---\n🤖 **Model:** `{actual_model}`\n⏱ **Waktu:** `{elapsed_time} detik`"
        final_message = ai_text + footer

        # 5. Kirim Hasil
        send_telegram_message(final_message)
        log("[✓] Sukses terkirim.")

    except Exception as e:
        log(f"[-] Error: {e}")
        send_telegram_message(f"❌ Error Sistem: {e}")
    finally:
        if os.path.exists(TEMP_IMAGE):
            os.remove(TEMP_IMAGE)
        is_processing = False


def on_press(key):
    # Hanya trigger jika tombol 0 ditekan
    if hasattr(key, "char") and key.char == "7":
        process_screen()


def on_release(key):
    # Tidak perlu melakukan apa-apa saat tombol dilepas, 
    # tapi fungsi ini dibutuhkan oleh keyboard.Listener (opsional)
    pass


if __name__ == "__main__":
    print("=========================================================")
    print("  AUTO SOLVER - HYBRID MODE (OCR Lokal + Gambar Kecil)   ")
    print(f"  Model Target : {KETON_MODEL}")
    print("  Shortcut     : Tekan [7] untuk screenshot")
    print("  Keluar       : Tekan [Ctrl + C]")
    print("=========================================================")
    print("Menunggu input...")
    # Listener sekarang lebih ringan karena hanya mendeteksi tombol 0
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()