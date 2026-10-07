midas:
	@echo "Menjalankan skrip midas.py..."
	python3 midas.py

keton:
	@echo "Menjalankan skrip keton.py..."
	python3 keton.py

vkey:
	@echo "Menjalankan skrip vkey.py..."
	python3 vkey.py

venv:
	source venv/bin/activate

install:
	@echo "Menginstal dependensi dari requirements.txt..."
	pip install -r requirements.txt