#!/bin/bash
set -e

# Oracle Cloud Infrastructure Bootstrap Script for MyBot Trading Bot
# This script sets up a fresh Ubuntu ARM instance with Docker and the trading bot

LOGFILE="/var/log/oracle-bot-bootstrap.log"
BOT_DIR="/opt/intraday_bot"
REPO_URL="https://github.com/mathankanagaraj/MyBot.git"

# Logging function
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOGFILE"
}

log "========================================="
log "[SETUP] Oracle instance bootstrap starting..."
log "========================================="

# Set timezone to Asia/Kolkata
log "[TIMEZONE] Setting timezone to Asia/Kolkata..."
timedatectl set-timezone Asia/Kolkata || log "[WARNING] Failed to set timezone"

# Update system packages
log "[APT] Updating system packages..."
apt-get update -y >> "$LOGFILE" 2>&1
apt-get upgrade -y >> "$LOGFILE" 2>&1

# Install essential packages
log "[APT] Installing essential packages..."
apt-get install -y \
    apt-transport-https \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    git \
    jq \
    >> "$LOGFILE" 2>&1

# Install Docker
log "[DOCKER] Installing Docker Engine..."
if ! command -v docker &> /dev/null; then
    # Add Docker's official GPG key
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    # Set up Docker repository
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

    # Install Docker Engine
    apt-get update -y >> "$LOGFILE" 2>&1
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >> "$LOGFILE" 2>&1
    
    log "[DOCKER] Docker installed successfully"
else
    log "[DOCKER] Docker already installed"
fi

# Enable and start Docker
log "[DOCKER] Enabling Docker service..."
systemctl enable docker >> "$LOGFILE" 2>&1
systemctl start docker >> "$LOGFILE" 2>&1

# Add ubuntu user to docker group
log "[DOCKER] Adding ubuntu user to docker group..."
usermod -aG docker ubuntu || log "[WARNING] Failed to add ubuntu to docker group"

# Clone bot repository
log "[GIT] Cloning bot repository..."
if [ -d "$BOT_DIR" ]; then
    log "[GIT] Directory $BOT_DIR already exists, pulling latest changes..."
    cd "$BOT_DIR"
    git pull >> "$LOGFILE" 2>&1
else
    git clone "$REPO_URL" "$BOT_DIR" >> "$LOGFILE" 2>&1
    log "[GIT] Repository cloned to $BOT_DIR"
fi

# Create log and audit directories
log "[SETUP] Creating log and audit directories..."
mkdir -p "$BOT_DIR/logs" "$BOT_DIR/audit"
chown -R ubuntu:ubuntu "$BOT_DIR"

# Create systemd log directory
mkdir -p /var/log/intraday_bot
chown ubuntu:ubuntu /var/log/intraday_bot

# Create environment configuration directory
log "[CONFIG] Creating environment configuration..."
mkdir -p /etc/default

# Create default environment file (user must update with actual credentials)
cat > /etc/default/intraday-bot << 'EOF'
# Angel Broker API Credentials
ANGEL_API_KEY="YOUR_API_KEY_HERE"
ANGEL_CLIENT_CODE="YOUR_CLIENT_CODE_HERE"
ANGEL_PASSWORD="YOUR_PASSWORD_HERE"
ANGEL_TOTP_SECRET="YOUR_TOTP_SECRET_HERE"

# Telegram Configuration
TELEGRAM_BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID="YOUR_TELEGRAM_CHAT_ID_HERE"

# Trading Configuration
TRADING_MODE="LIVE"
SYMBOLS="NIFTY,BANKNIFTY"
MAX_POSITIONS=2
RISK_PER_TRADE=0.02
EOF

log "[CONFIG] Created /etc/default/intraday-bot (UPDATE WITH YOUR CREDENTIALS!)"

# Create environment file generator script
log "[SCRIPT] Creating environment file generator..."
cat > /usr/local/bin/create_env.sh << 'EOF'
#!/bin/bash
# Generate .env file from /etc/default/intraday-bot

source /etc/default/intraday-bot

cat > /opt/intraday_bot/.env << ENVEOF
# Angel Broker API Credentials
ANGEL_API_KEY=${ANGEL_API_KEY}
ANGEL_CLIENT_CODE=${ANGEL_CLIENT_CODE}
ANGEL_PASSWORD=${ANGEL_PASSWORD}
ANGEL_TOTP_SECRET=${ANGEL_TOTP_SECRET}

# Telegram Configuration
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}

# Trading Configuration
TRADING_MODE=${TRADING_MODE}
SYMBOLS=${SYMBOLS}
MAX_POSITIONS=${MAX_POSITIONS}
RISK_PER_TRADE=${RISK_PER_TRADE}
ENVEOF

chmod 600 /opt/intraday_bot/.env
chown ubuntu:ubuntu /opt/intraday_bot/.env
echo "Environment file created at /opt/intraday_bot/.env"
EOF

chmod +x /usr/local/bin/create_env.sh
log "[SCRIPT] Created /usr/local/bin/create_env.sh"

# Generate initial .env file
log "[CONFIG] Generating initial .env file..."
/usr/local/bin/create_env.sh >> "$LOGFILE" 2>&1

# Create Telegram notification script
log "[SCRIPT] Creating Telegram notification script..."
cat > /usr/local/bin/tg.sh << 'EOF'
#!/bin/bash
# Send Telegram notification

source /etc/default/intraday-bot

if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
    echo "Telegram credentials not configured"
    exit 1
fi

MESSAGE="$1"
if [ -z "$MESSAGE" ]; then
    echo "Usage: tg.sh \"message\""
    exit 1
fi

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="${TELEGRAM_CHAT_ID}" \
    -d text="${MESSAGE}" \
    -d parse_mode="HTML" > /dev/null
EOF

chmod +x /usr/local/bin/tg.sh
log "[SCRIPT] Created /usr/local/bin/tg.sh"

# Create systemd service for bot start
log "[SYSTEMD] Creating bot start service..."
cat > /etc/systemd/system/intraday-bot-start.service << 'EOF'
[Unit]
Description=Start Intraday Options Trading Bot
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/intraday_bot
EnvironmentFile=/etc/default/intraday-bot
ExecStartPre=/usr/local/bin/create_env.sh
ExecStart=/usr/bin/docker compose up -d --build
ExecStartPost=/usr/local/bin/tg.sh "🚀 Bot Starting Up (Mode: ${TRADING_MODE})"
StandardOutput=append:/var/log/intraday_bot/service.log
StandardError=append:/var/log/intraday_bot/service.log

[Install]
WantedBy=multi-user.target
EOF

# Create systemd service for bot stop
log "[SYSTEMD] Creating bot stop service..."
cat > /etc/systemd/system/intraday-bot-stop.service << 'EOF'
[Unit]
Description=Stop Intraday Options Trading Bot
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/intraday_bot
EnvironmentFile=/etc/default/intraday-bot
ExecStart=/usr/bin/docker compose down
ExecStartPost=/usr/local/bin/tg.sh "🛑 Bot Stopped (End of Trading Day)"
StandardOutput=append:/var/log/intraday_bot/service.log
StandardError=append:/var/log/intraday_bot/service.log

[Install]
WantedBy=multi-user.target
EOF

# Create systemd service for health check
log "[SYSTEMD] Creating health check service..."
cat > /etc/systemd/system/intraday-bot-health.service << 'EOF'
[Unit]
Description=Health Check for Intraday Options Trading Bot
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=root
ExecStart=/bin/bash -c '\
    if ! docker ps | grep -q intraday_options_bot_angel; then \
        /usr/local/bin/tg.sh "⚠️ Bot container is not running! Attempting restart..."; \
        cd /opt/intraday_bot && docker compose up -d --build; \
    fi'
StandardOutput=append:/var/log/intraday_bot/health.log
StandardError=append:/var/log/intraday_bot/health.log
EOF

# Create systemd timer for bot start (09:00 IST, Mon-Fri)
log "[SYSTEMD] Creating bot start timer..."
cat > /etc/systemd/system/intraday-bot-start.timer << 'EOF'
[Unit]
Description=Start Intraday Bot at 09:00 IST (Mon-Fri)

[Timer]
OnCalendar=Mon-Fri 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Create systemd timer for bot stop (15:45 IST, Mon-Fri)
log "[SYSTEMD] Creating bot stop timer..."
cat > /etc/systemd/system/intraday-bot-stop.timer << 'EOF'
[Unit]
Description=Stop Intraday Bot at 15:45 IST (Mon-Fri)

[Timer]
OnCalendar=Mon-Fri 15:45:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Create systemd timer for health check (every 15 minutes during market hours)
log "[SYSTEMD] Creating health check timer..."
cat > /etc/systemd/system/intraday-bot-health.timer << 'EOF'
[Unit]
Description=Health Check for Intraday Bot (Every 15 minutes)

[Timer]
OnCalendar=Mon-Fri 09:00..15:45:00/15
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Reload systemd and enable timers
log "[SYSTEMD] Reloading systemd and enabling timers..."
systemctl daemon-reload
systemctl enable intraday-bot-start.timer >> "$LOGFILE" 2>&1
systemctl enable intraday-bot-stop.timer >> "$LOGFILE" 2>&1
systemctl enable intraday-bot-health.timer >> "$LOGFILE" 2>&1
systemctl start intraday-bot-start.timer >> "$LOGFILE" 2>&1
systemctl start intraday-bot-stop.timer >> "$LOGFILE" 2>&1
systemctl start intraday-bot-health.timer >> "$LOGFILE" 2>&1

log "[SYSTEMD] Timers enabled and started"

# Configure log rotation
log "[LOGROTATE] Configuring log rotation..."
cat > /etc/logrotate.d/intraday-bot << 'EOF'
/var/log/intraday_bot/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0644 ubuntu ubuntu
}

/opt/intraday_bot/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0644 ubuntu ubuntu
}
EOF

log "[LOGROTATE] Log rotation configured"

# Set proper permissions
log "[PERMISSIONS] Setting proper permissions..."
chown -R ubuntu:ubuntu "$BOT_DIR"
chmod -R 755 "$BOT_DIR"

log "========================================="
log "[SUCCESS] Bootstrap completed successfully!"
log "========================================="
log ""
log "NEXT STEPS:"
log "1. Edit /etc/default/intraday-bot with your actual credentials"
log "2. Run: sudo /usr/local/bin/create_env.sh"
log "3. Test manually: sudo systemctl start intraday-bot-start.service"
log "4. Check logs: docker logs intraday_options_bot_angel"
log "5. Verify timers: systemctl list-timers"
log ""
log "Bot will automatically start at 09:00 and stop at 15:45 IST (Mon-Fri)"
log "========================================="

exit 0
