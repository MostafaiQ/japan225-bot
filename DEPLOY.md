# Deployment Guide - Oracle Cloud Free Tier

Step-by-step guide to deploy the Japan 225 Trading Bot monitor on Oracle Cloud's Always Free Tier.

---

## Prerequisites

- Oracle Cloud account (free signup at cloud.oracle.com)
- GitHub repository with the bot code pushed
- All GitHub Secrets configured (see README.md)
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

# Install Python 3.11+ and pip
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

All 8 checks should pass. If any fail, fix the credentials and retry.

---

## Step 5: Create a Systemd Service

This ensures the monitor auto-starts on boot and restarts on crash.

```bash
sudo nano /etc/systemd/system/japan225-bot.service
```

Paste this content:

```ini
[Unit]
Description=Japan 225 Trading Bot Monitor
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
# Send /status to your bot - it should respond
```

---

## Step 7: Enable GitHub Actions Scanning

1. Push your code to GitHub (if not already)
2. Go to your repo > **Actions** tab
3. The "Japan 225 Scan" workflow should appear
4. It will auto-run on the cron schedule (every 2 hours Mon-Fri)
5. You can also trigger it manually via **Run workflow**

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

The SQLite database lives **exclusively on the Oracle Cloud VM** at `storage/data/trading.db`.

**GitHub Actions does NOT write to the database.** The scan workflow runs AI analysis and sends trade alerts via Telegram, but all persistent state (trades, positions, account history, scan history) is written only by `monitor.py` on the VM.

This means:
- No `git pull` cron job needed — there is no DB to sync from GitHub
- No DB lock conflicts — only one process writes
- DB is never accidentally overwritten by a `git pull`

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
- [ ] All Telegram commands respond correctly
- [ ] Scans run on schedule (check GitHub Actions history)
- [ ] Position monitoring works (tested with paper trades)
- [ ] Exit strategy phases trigger correctly
- [ ] Alert expiry works (unconfirmed alerts auto-expire)
- [ ] System pause/resume works via /stop and /resume

When ready:
1. Edit `.env` on the Oracle VM: change `TRADING_MODE=live` and `IG_ENV=live`
2. Update GitHub Secrets: `TRADING_MODE=live` and `IG_ENV=live`
3. Restart: `sudo systemctl restart japan225-bot`
4. Start with minimum lot sizes (0.01-0.02)

---

## Troubleshooting

**Monitor crashes and restarts:**
Systemd auto-restarts after 30 seconds. Check logs with `journalctl -u japan225-bot --since "10 min ago"`.

**IG API connection fails:**
Tokens expire after ~6 hours. The bot auto-reauthenticates. If persistent, check credentials.

**Telegram bot not responding:**
Ensure only ONE instance of monitor.py is running. Two instances will fight over Telegram updates.

**GitHub Actions scan fails:**
Check the Actions tab for error logs. Most common: expired secrets or IG API downtime.

**Database locked errors:**
The database lives only on the VM and is written only by `monitor.py`. If you see lock errors, ensure only one instance of `monitor.py` is running: `ps aux | grep monitor.py`.

---

*Deploy once, run forever (or until Oracle changes their free tier).*
