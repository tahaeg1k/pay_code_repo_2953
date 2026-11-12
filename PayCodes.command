#!/usr/bin/env bash
# Double-click in Finder to start ScopePay and open the wallet + demo store in your browser.
cd "$(dirname "$0")"
PORT=8001

if [ ! -f .env ]; then
  echo "No .env found. Copy .env.example to .env and add your PayPal sandbox Client ID + Secret."
  read -n 1 -s -r -p "Press any key to close."; exit 1
fi

[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
python -c "import fastapi, uvicorn, requests, dotenv, cryptography" 2>/dev/null || { echo "Installing dependencies…"; pip install -q -r requirements.txt; }

if lsof -ti tcp:$PORT >/dev/null 2>&1; then
  echo "ScopePay already running on port $PORT. Opening it."
  open "http://localhost:$PORT"; exit 0
fi

( sleep 3; open "http://localhost:$PORT" ) &
echo "ScopePay running at http://localhost:$PORT  (close this window or press Ctrl+C to stop)"
exec uvicorn app:app --port $PORT --reload
