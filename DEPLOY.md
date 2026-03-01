# Deployment Guide - Oracle Cloud Free Tier

Step-by-step guide to deploy the Japan 225 Trading Bot on Oracle Cloud's Always Free Tier.

The entire bot runs as a **single process** (`monitor.py`) on the VM. There is no GitHub Actions scan job — all scanning, monitoring, and Telegram handling happens on the VM.

---

## Prerequisites

- Oracle Cloud account (free signup at cloud.oracle.com)
- GitHub repository with the bot code pushed
- Credentials tested locally with `./setup.sh`

---

## Step 1: Create an Always Free VM

1. Log into Oracle Cloud Console
2. Go to **Compute > Instances > Create Instance**
3. Settings:
   - **Name:** `japan225-bot`
   - **Image:** Ubuntu 22.04 or 24.04
   - **Shape:** VM.Standard.A1.Flex (ARM) - 1 OCPU, 6 GB RAM
   - **This is Always Free eligible**
4. Under **Add SSH keys:** upload your public key or generate one
5. Click **Create**
6. Note the **Public IP Address** once it's running

---

## Step 2: Connect and Set Up the Server

```bash
# SSH into your VM
ssh ubuntu@YOUR_PUBLIC_IP

# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.10+ and pip
sudo apt install -y python3 python3-pip python3-venv git

# Clone your repo
git clone https://github.com/YOUR_USERNAME/japan225-bot.git
cd japan225-bot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## Step 3: Configure Credentials

```bash
# Copy and edit the environment file
cp .env.example .env
nano .env
```

Fill in all values:
```
IG_API_KEY=your_key
IG_USERNAME=your_username
IG_PASSWORD=your_password
IG_ACC_NUMBER=your_account
IG_ENV=live
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
TRADING_MODE=live
DASHBOARD_TOKEN=choose_a_long_random_secret
DEBUG=false
```

---

## Step 4: Verify Setup

```bash
source venv/bin/activate
./setup.sh
```

All checks should pass. If any fail, fix the credentials and retry.

---

## Step 5: Create a Systemd Service

This ensures the monitor auto-starts on boot and restarts on crash.

```bash
sudo nano /etc/systemd/system/japan225-bot.service
```

Paste this content:

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
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable japan225-bot
sudo systemctl start japan225-bot
```

---

## Step 6: Verify It's Running

```bash
# Check service status
sudo systemctl status japan225-bot

# View live logs
sudo journalctl -u japan225-bot -f

# Test via Telegram
# Send /status to your bot — it should respond
```

---

## Step 7: Set Up the Web Dashboard

The dashboard is a FastAPI app served via an ngrok tunnel, with a static frontend on GitHub Pages.

### 7a — Install Claude Code CLI (required for dashboard chat)

```bash
npm install -g @anthropic-ai/claude-code
```

Verify: `claude --version`

### 7b — Install ngrok

```bash
curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list
sudo apt update && sudo apt install ngrok
ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN
```

Get a free static domain at `dashboard.ngrok.com → Domains → New Domain`.

### 7c — Create dashboard systemd service

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

### 7d — Create ngrok systemd service

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
ExecStart=/usr/local/bin/ngrok http --domain=YOUR_STATIC_DOMAIN.ngrok-free.app 8080
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Replace `YOUR_STATIC_DOMAIN` with your actual ngrok domain.

### 7e — Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable japan225-dashboard japan225-ngrok
sudo systemctl start japan225-dashboard japan225-ngrok
```

### 7f — Connect the frontend

1. Open `https://mostafaiq.github.io/japan225-bot/`
2. Click ⚙ (top-right)
3. Enter your ngrok URL (`https://YOUR_STATIC_DOMAIN.ngrok-free.app`)
4. Enter your `DASHBOARD_TOKEN`
5. Click **Save & Connect**

### 7g — Verify all three services

```bash
sudo systemctl status japan225-bot japan225-dashboard japan225-ngrok
```

---

## Maintenance Commands

```bash
# View logs
sudo journalctl -u japan225-bot -f
sudo journalctl -u japan225-bot --since "1 hour ago"

# Restart the service
sudo systemctl restart japan225-bot

# Stop the service
sudo systemctl stop japan225-bot

# Update the bot code
cd ~/japan225-bot
git pull
sudo systemctl restart japan225-bot

# Check disk usage (free tier has 47GB)
df -h
```

---

## Database Architecture

The SQLite database lives **exclusively on the Oracle Cloud VM** at `storage/data/trading.db`. It is written only by `monitor.py`.

- No sync needed — one process, one DB, one VM.
- DB is never touched by any external job.

To back up the database from the VM:

```bash
# On your local machine
scp ubuntu@YOUR_IP:/home/ubuntu/japan225-bot/storage/data/trading.db ./trading_backup.db
```

---

## Firewall Rules

The bot only needs **outbound** internet access. No inbound ports need to be opened.

If Oracle Cloud's default security list blocks outbound traffic:
1. Go to **Networking > Virtual Cloud Networks > Your VCN > Security Lists**
2. Ensure there's an **Egress Rule** allowing all traffic (0.0.0.0/0, all protocols)

---

## Going Live Checklist

- [ ] All Telegram commands respond correctly (`/status`, `/balance`, `/close`, `/kill`)
- [ ] Scanning runs on schedule (check logs — should see 5-min scan attempts during sessions)
- [ ] Exit strategy phases trigger correctly (breakeven at +150pts, runner at 75% TP in <2hrs)
- [ ] Alert expiry works (unconfirmed alerts auto-expire after 15 min)
- [ ] System pause/resume works via `/stop` and `/resume`
- [ ] Inline Close/Hold buttons work on position alerts
- [ ] `python3 healthcheck.py` shows all green (234 tests passing, all services active)
- [ ] `IG_ENV=live` set in `.env`
- [ ] Start with minimum lot sizes (0.01–0.02)

---

## Troubleshooting

**Monitor crashes and restarts:**
Systemd auto-restarts after 30 seconds. Check logs: `journalctl -u japan225-bot --since "10 min ago"`.

**IG API returns 503 (weekend maintenance / outage):**
The bot stays alive. Telegram is started before the IG connection attempt, so it remains fully responsive. The bot sends you a Telegram alert and retries IG every 5 minutes. No action needed — it self-recovers when IG comes back up.

**IG API connection fails (persistent):**
Tokens expire after ~6 hours. The bot auto-reauthenticates. If it keeps failing outside of known maintenance windows, check your `.env` credentials with `./setup.sh`.

**Telegram bot not responding:**
The bot only stops responding to Telegram if the `japan225-bot` process has died entirely. Check: `sudo systemctl status japan225-bot`. If it's stopped, start it: `sudo systemctl start japan225-bot`. Also ensure only ONE instance is running: `ps aux | grep monitor.py`.

**Scans not firing every 5 minutes:**
Check that the VM clock is correct (`date`) and that the current time is within an active session (Tokyo/London/NY in Kuwait time UTC+3). Off-hours = 30-min heartbeat only.

**Database locked errors:**
Only one instance of `monitor.py` should be running. Check: `ps aux | grep monitor.py`. Kill duplicates.

---

*Deploy once, run forever (or until Oracle changes their free tier).*
