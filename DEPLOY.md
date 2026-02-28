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
IG_ENV=demo                    # Start with demo!
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
TRADING_MODE=paper             # Start with paper!
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

Before switching from paper to live:

- [ ] Ran successfully in paper mode for at least 1 week
- [ ] All Telegram commands respond correctly (`/status`, `/balance`, `/close`, `/kill`)
- [ ] Scanning runs on schedule (check logs — should see 5-min scan attempts during sessions)
- [ ] Position monitoring works (tested with paper trades)
- [ ] Exit strategy phases trigger correctly (breakeven at +150pts, runner at 75% TP)
- [ ] Alert expiry works (unconfirmed alerts auto-expire after 15 min)
- [ ] System pause/resume works via `/stop` and `/resume`
- [ ] Inline Close/Hold buttons work on position alerts

When ready:
1. Edit `.env` on the Oracle VM: change `TRADING_MODE=live` and `IG_ENV=live`
2. Restart: `sudo systemctl restart japan225-bot`
3. Start with minimum lot sizes (0.01–0.02)

---

## Troubleshooting

**Monitor crashes and restarts:**
Systemd auto-restarts after 30 seconds. Check logs: `journalctl -u japan225-bot --since "10 min ago"`.

**IG API connection fails:**
Tokens expire after ~6 hours. The bot auto-reauthenticates. If persistent, check credentials.

**Telegram bot not responding:**
Ensure only ONE instance of `monitor.py` is running. Two instances will fight over Telegram updates: `ps aux | grep monitor.py`.

**Scans not firing every 5 minutes:**
Check that the VM clock is correct (`date`) and that the current time is within an active session (Tokyo/London/NY in Kuwait time UTC+3). Off-hours = 30-min heartbeat only.

**Database locked errors:**
Only one instance of `monitor.py` should be running. Check: `ps aux | grep monitor.py`. Kill duplicates.

---

*Deploy once, run forever (or until Oracle changes their free tier).*
