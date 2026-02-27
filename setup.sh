#!/bin/bash
# ============================================
# JAPAN 225 TRADING BOT - SETUP & VERIFICATION
# ============================================
# Run this after cloning the repo and setting up .env
# It tests each component independently.

set -e
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "============================================"
echo "  JAPAN 225 BOT - SETUP & VERIFICATION"
echo "============================================"
echo ""

# --- Step 1: Python ---
echo -e "${YELLOW}[1/8] Checking Python...${NC}"
python3 --version || { echo -e "${RED}Python 3 not found. Install it first.${NC}"; exit 1; }
echo -e "${GREEN}OK${NC}"

# --- Step 2: Dependencies ---
echo -e "${YELLOW}[2/8] Installing dependencies...${NC}"
pip install -r requirements.txt --quiet
echo -e "${GREEN}OK${NC}"

# --- Step 3: .env file ---
echo -e "${YELLOW}[3/8] Checking .env file...${NC}"
if [ ! -f .env ]; then
    echo -e "${RED}.env file not found!${NC}"
    echo "Copy .env.example to .env and fill in your credentials:"
    echo "  cp .env.example .env"
    echo "  nano .env"
    exit 1
fi
echo -e "${GREEN}OK${NC}"

# --- Step 4: Unit tests (no credentials needed) ---
echo -e "${YELLOW}[4/8] Running unit tests...${NC}"
python -m pytest tests/test_indicators.py -v --tb=short
echo -e "${GREEN}OK${NC}"

# --- Step 5: Database init ---
echo -e "${YELLOW}[5/8] Initializing database...${NC}"
python3 -c "
from storage.database import Storage
s = Storage()
print('  Database created at:', s.db_path)
print('  Account state:', s.get_account_state())
print('  Position state:', s.get_position_state())
"
echo -e "${GREEN}OK${NC}"

# --- Step 6: IG API connection ---
echo -e "${YELLOW}[6/8] Testing IG API connection...${NC}"
python3 -c "
from core.ig_client import IGClient
ig = IGClient()
if ig.connect():
    print('  Connected to IG API')
    market = ig.get_market_info()
    if market:
        print(f'  Market status: {market[\"market_status\"]}')
        print(f'  Current price: {market[\"bid\"]}')
        print(f'  Spread: {market[\"spread\"]:.1f} points')
        print(f'  Trailing stops: {market[\"trailing_stops_available\"]}')
    account = ig.get_account_info()
    if account:
        print(f'  Balance: \${account[\"balance\"]:.2f}')
        print(f'  Available: \${account[\"available\"]:.2f}')
    # Test price fetch
    candles = ig.get_prices('HOUR_4', 5)
    print(f'  Fetched {len(candles)} candles (4H)')
else:
    print('  FAILED - check IG credentials in .env')
    exit(1)
" || { echo -e "${RED}IG API test failed${NC}"; exit 1; }
echo -e "${GREEN}OK${NC}"

# --- Step 7: Telegram bot ---
echo -e "${YELLOW}[7/8] Testing Telegram bot...${NC}"
python3 -c "
import asyncio
from notifications.telegram_bot import send_standalone_message

async def test():
    await send_standalone_message('Japan 225 Bot: Setup test successful!')
    print('  Message sent to Telegram')

asyncio.run(test())
" || { echo -e "${RED}Telegram test failed${NC}"; exit 1; }
echo -e "${GREEN}OK${NC}"

# --- Step 8: Anthropic API ---
echo -e "${YELLOW}[8/8] Testing Anthropic API...${NC}"
python3 -c "
import anthropic
import os
client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
response = client.messages.create(
    model='claude-sonnet-4-5-20250929',
    max_tokens=50,
    messages=[{'role': 'user', 'content': 'Reply with only: API OK'}],
)
print(f'  Response: {response.content[0].text}')
print(f'  Tokens: {response.usage.input_tokens} in / {response.usage.output_tokens} out')
" || { echo -e "${RED}Anthropic API test failed${NC}"; exit 1; }
echo -e "${GREEN}OK${NC}"

echo ""
echo "============================================"
echo -e "${GREEN}  ALL TESTS PASSED!${NC}"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Run a test scan:  python main.py"
echo "  2. Start monitor:    python monitor.py"
echo "  3. Push to GitHub:   git push"
echo "  4. Set up GitHub Secrets (see README)"
echo "  5. Deploy monitor to Oracle Cloud"
echo ""
echo "Telegram commands available:"
echo "  /status /balance /journal /today /stats"
echo "  /stop /resume /close /force /cost"
