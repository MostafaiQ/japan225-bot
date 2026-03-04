# Deployment Guide

Step-by-step guide to deploy the Japan 225 Trading Bot on Oracle Cloud's Always Free Tier. The entire system runs on a single VM -- no additional infrastructure needed.

## Architecture

```
Oracle Cloud VM (Always Free, ARM, 1 OCPU, 6GB RAM)
├── japan225-bot.service         # monitor.py -- scanning + monitoring + Telegram
├── japan225-dashboard.service   # FastAPI on 127.0.0.1:8080 (optional)
└── japan225-ngrok.service       # ngrok tunnel for remote dashboard access (optional)
```

The bot (`monitor.py`) is the only required service. The dashboard and ngrok are optional for remote monitoring.

---

## 1. Create the VM

1. Sign up at [cloud.oracle.com](https://cloud.oracle.com) (free, no credit card required for Always Free resources)
2. Go to **Compute > Instances > Create Instance**
3. Configure:
   - **Image:** Ubuntu 22.04 or 24.04
   - **Shape:** VM.Standard.A1.Flex (ARM) -- 1 OCPU, 6 GB RAM
   - **Boot volume:** 47 GB (default)
   - **SSH key:** Upload your public key or generate one
4. Click **Create** and note the **Public IP** once running

---

## 2. Server Setup

```bash
ssh ubuntu@YOUR_PUBLIC_IP

# System updates
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git

# Clone the repo
git clone https://github.com/mostafaiq/japan225-bot.git
cd japan225-bot

# Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
nano .env   # Fill in all values (see README.md for details)
```

### Required `.env` values

```bash
IG_API_KEY=your_key
IG_USERNAME=your_username
IG_PASSWORD=your_password
IG_ACC_NUMBER=your_account
IG_ENV=demo                      # Start with demo, switch to live when ready
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
TRADING_MODE=live
DEBUG=false

# Optional: for web dashboard
DASHBOARD_TOKEN=your_long_random_secret
```

### Verify

```bash
source venv/bin/activate
./setup.sh
```

All checks should pass. Fix any credential issues and retry.

---

## 3. Bot Service (Required)

Create the systemd service:

```bash
sudo nano /etc/systemd/system/japan225-bot.service
```

```ini
[Unit]
Description=Japan 225 Trading Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/japan225-bot
Environment=PATH=/home/ubuntu/japan225-bot/venv/bin:/usr/bin
ExecStart=/home/ubuntu/japan225-bot/venv/bin/python monitor.py
Restart=always
RestartSec=1
KillSignal=SIGKILL
TimeoutStopSec=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

> **Note:** `KillSignal=SIGKILL` and `TimeoutStopSec=1` ensure instant restarts. The bot's positions are protected by broker-side stop losses, the SQLite DB uses WAL mode (crash-safe), and candle data is cached to disk.

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable japan225-bot
sudo systemctl start japan225-bot
```

Verify:

```bash
sudo systemctl status japan225-bot
sudo journalctl -u japan225-bot -f   # Live logs
```

Send `/status` to your Telegram bot -- it should respond.

---

## 4. Dashboard Setup (Optional)

The web dashboard provides remote monitoring, config changes, and a Claude AI chat interface -- no SSH required.

### 4a. Install Claude Code CLI

Required for the dashboard chat feature:

```bash
# Install Node.js if not present
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

# Install Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Verify
claude --version
```

### 4b. Install ngrok

```bash
curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
  | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
  | sudo tee /etc/apt/sources.list.d/ngrok.list
sudo apt update && sudo apt install ngrok

# Authenticate
ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN
```

Get a free static domain at [dashboard.ngrok.com](https://dashboard.ngrok.com) > Domains > New Domain.

### 4c. Dashboard systemd service

```bash
sudo nano /etc/systemd/system/japan225-dashboard.service
```

```ini
[Unit]
Description=Japan 225 Dashboard
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/japan225-bot
Environment=PATH=/home/ubuntu/japan225-bot/venv/bin:/usr/bin
EnvironmentFile=/home/ubuntu/japan225-bot/.env
ExecStart=/home/ubuntu/japan225-bot/venv/bin/python dashboard/run.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 4d. ngrok systemd service

```bash
sudo nano /etc/systemd/system/japan225-ngrok.service
```

```ini
[Unit]
Description=Japan 225 ngrok Tunnel
After=network.target

[Service]
Type=simple
User=ubuntu
ExecStart=/usr/local/bin/ngrok http --domain=YOUR_DOMAIN.ngrok-free.app 8080
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Replace `YOUR_DOMAIN` with your actual ngrok static domain.

### 4e. Start dashboard services

```bash
sudo systemctl daemon-reload
sudo systemctl enable japan225-dashboard japan225-ngrok
sudo systemctl start japan225-dashboard japan225-ngrok
```

### 4f. Connect the frontend

The frontend is a single-page app hosted on GitHub Pages. To use it:

1. Fork the repo and enable GitHub Pages on the `docs/` folder
2. Open your GitHub Pages URL
3. Click the settings icon (top-right)
4. Enter your ngrok URL and `DASHBOARD_TOKEN`
5. Click **Save & Connect**

### 4g. Verify all services

```bash
sudo systemctl status japan225-bot japan225-dashboard japan225-ngrok
```

---

## 5. Firewall

The bot only needs **outbound** internet access. No inbound ports required.

If Oracle Cloud's default security list blocks outbound traffic:
1. Go to **Networking > Virtual Cloud Networks > Your VCN > Security Lists**
2. Ensure there's an egress rule allowing all traffic (0.0.0.0/0, all protocols)

---

## 6. Maintenance

### Common commands

```bash
# Live logs
sudo journalctl -u japan225-bot -f
sudo journalctl -u japan225-bot --since "1 hour ago"

# Restart
sudo systemctl restart japan225-bot

# Stop
sudo systemctl stop japan225-bot

# Update code
cd ~/japan225-bot
git pull
sudo systemctl restart japan225-bot

# Check disk usage
df -h

# Database backup (from your local machine)
scp ubuntu@YOUR_IP:~/japan225-bot/storage/data/trading.db ./backup.db
```

### Health check

```bash
source ~/japan225-bot/venv/bin/activate
cd ~/japan225-bot
python3 healthcheck.py
```

Checks services, test suite, git status, trades, config, and recent errors.

---

## 7. Going Live Checklist

- [ ] All tests pass: `python3 -m pytest tests/ -v`
- [ ] `/status` responds in Telegram
- [ ] Scans run every 5 min during active sessions (check logs)
- [ ] Set `IG_ENV=live` in `.env`
- [ ] Start with minimum lots (0.01-0.02)
- [ ] Confirm exit phases work: breakeven at +150pts, runner at 75% TP
- [ ] Test `/close` and `/kill` commands
- [ ] Test alert auto-expiry (15 min timeout)

---

## 8. Adapting for Your Own Use

### Different instrument

1. Find the IG epic for your instrument at [IG Labs](https://labs.ig.com)
2. Update `config/settings.py`:
   ```python
   EPIC = "your.epic.here"
   CONTRACT_SIZE = 1           # Check IG's contract spec
   MARGIN_FACTOR = 0.005       # Check IG's margin requirement for your instrument
   ```
3. Adjust `DEFAULT_SL_DISTANCE`, `DEFAULT_TP_DISTANCE`, and `BREAKEVEN_TRIGGER` for your instrument's volatility
4. Update `SESSION_HOURS_UTC` for your instrument's active hours
5. Update the AI system prompt in `ai/analyzer.py` to reference your instrument

### Different broker

The bot uses the [trading-ig](https://github.com/ig-python/ig-markets-api-python-library) library. To use a different broker:
1. Replace `core/ig_client.py` with your broker's API wrapper
2. Implement the same interface: `connect()`, `get_prices()`, `open_position()`, `modify_position()`, `close_position()`, `get_open_positions()`, `get_market_info()`, `get_account_info()`
3. The rest of the bot (indicators, AI, risk management, Telegram) works unchanged

### Different AI provider

The bot calls Claude via the Claude Code CLI subprocess. To use a different AI:
1. Replace the `_run_claude()` method in `ai/analyzer.py`
2. Keep the same JSON output schema (the rest of the bot parses this)
3. The system prompt and scan prompt can be reused with any LLM

---

## Troubleshooting

**Bot crashes and restarts:**
Systemd auto-restarts in 1 second. Check logs: `journalctl -u japan225-bot --since "10 min ago"`.

**IG API returns 503:**
Normal during weekends and maintenance. The bot stays alive, sends a Telegram alert, and retries every minute. Dashboard shows "IG OFFLINE". It self-recovers.

**IG tokens expire:**
Tokens expire after ~6 hours. The bot auto-reauthenticates via `ensure_connected()`.

**Telegram not responding:**
Check if the bot process is running: `sudo systemctl status japan225-bot`. Telegram is initialized first and stays available even when IG is down.

**Rate limit errors on candle fetches:**
The bot caches candles to disk (`storage/data/candle_cache.json`). After restart, it loads the cache and uses delta fetches. If you hit the weekly 10,000-point IG data allowance, the bot backs off for 1 hour and uses cached data.

**Database locked:**
Only one instance of `monitor.py` should run. Check: `ps aux | grep monitor.py`. Kill duplicates.
