#!/bin/bash
# Octane Labs Bot — double-click this file to start
# To make it runnable: open Terminal, run: chmod +x start_bot.command

cd "$(dirname "$0")"

echo "======================================"
echo "  Octane Labs Discord Bot"
echo "  $(date '+%A %d %B %Y, %H:%M')"
echo "======================================"

# Activate virtual environment
if [ -d "venv" ]; then
    source venv/bin/activate
    echo "✅ Virtual environment activated"
else
    echo "⚠️  No venv found. Run setup first:"
    echo "   python3 -m venv venv && source venv/bin/activate"
    echo "   pip install discord.py gspread google-auth"
    read -p "Press Enter to exit..."
    exit 1
fi

# Check credentials file exists
if [ ! -f "credentials.json" ]; then
    echo "⚠️  credentials.json not found — Google Sheets won't update"
fi

echo "🚀 Starting bot... (close this window to stop)"
echo ""

python3 bot.py

echo ""
echo "Bot stopped. Close this window."
read -p "Press Enter to exit..."
