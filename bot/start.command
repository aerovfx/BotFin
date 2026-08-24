#!/bin/bash
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Lan dau tien: dang cai dat thu vien..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

( sleep 2 && open "http://127.0.0.1:8787" ) &

echo "=========================================="
echo "  Bot Tin Tuc Kinh Te - Dashboard"
echo "  http://127.0.0.1:8787"
echo "  Dong cua so nay = tat bot"
echo "=========================================="
exec .venv/bin/python app.py
