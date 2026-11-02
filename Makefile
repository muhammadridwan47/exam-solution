VENV := venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: run list dry-run venv install test-telegram

run: $(VENV)/bin/activate
	@echo "Menjalankan main.py (hotkey listener)..."
	$(PYTHON) main.py

list: $(VENV)/bin/activate
	$(PYTHON) main.py --list

dry-run: $(VENV)/bin/activate
	$(PYTHON) main.py --run 1 --dry-run

$(VENV)/bin/activate:
	python3 -m venv $(VENV)

venv: $(VENV)/bin/activate

install: $(VENV)/bin/activate
	@echo "Menginstal dependensi dari requirements.txt ke $(VENV)..."
	$(PIP) install -r requirements.txt

test-telegram: $(VENV)/bin/activate
	@echo "Mengirim pesan uji ke Telegram..."
	@$(PYTHON) -c 'import os, requests; from dotenv import load_dotenv; load_dotenv(); token = os.getenv("TELEGRAM_TOKEN"); chat = os.getenv("TELEGRAM_CHAT_ID"); assert token and chat, "TELEGRAM_TOKEN/TELEGRAM_CHAT_ID belum diset di .env"; r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": "connected"}); print(r.status_code, r.json()); r.raise_for_status()'
