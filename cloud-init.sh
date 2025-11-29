#cloud-config
ssh_authorized_keys:
  - ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQDQM6zuglVy8qcOqQ6pe9n2heo/Y1CrH3CZCpXocGIPBLV+bAWktmJKbYAl8Ml1OUNCKdEdutybgBhp1eiMkQ2Gx/PRn4XEpVouwOpD3N8vA7tNpWTAs8LEiZpd2UYGwhHw9XuS6E0mQfiskEXXztl+5JbRFn3nnM9PJkBCY3xYK6qOlcfVcFyd8QFRnNKD0WwJPqfBXdulagPCMkYaH/s4qZXjnpcXcjaQLaluvHQgTI2+8bvGKC71U6RCkU1LM5jFysBYpLvvWIwUaYGA0qGCUZOathDJxZv0qW/UU7Nu+FmtJbbNdu4fS3fvnJJK7GVxEA2oVODzI5jdvDUy3JahJ0sEQOuKPCVeWhL7Y7ueUegqa3yK1irzRjwhe/16rBwCn3XyBQmEh4MA9u4GrCh2PYBatIsdfVcYwQrlfshl9mUfw17izjjuGxDx6CJcY8zimy84XkvEXc4kL0r0VUW5MequtKWA/opRrAkyjOaVq31oYfrPuL9qIOX2Pce2la9dwivUMe7Gu81d627Ct7GMARhgqdJzPKj0qaBbvMkpBmCl9MAGfa62nakC0hPO95gKl3THImKaPb2iQECi/HZqvSXGeImOeowbmcwXfU4CIr0MgB9oAX7t+0TSCTHx29b1BVmE0xLDXWJ+JoaUKc3xKNbMqoQNBJwfmQA8gyrmpw== oci-cloud-shell

package_update: true
package_upgrade: true

write_files:
  # --------------------------
  # Global Environment Variables
  # --------------------------
  # IMPORTANT: Edit these values with your actual credentials after instance creation
  - path: /etc/default/intraday-bot
    permissions: "0644"
    content: |
      BOT_DIR="/opt/intraday_bot"
      REPO_URL="https://github.com/mathankanagaraj/MyBot.git"
      BRANCH="main"
      
      # Telegram Notifications
      TELEGRAM_TOKEN="8312696115:AAFCdD1OdeMfpQ0KVioOoLu85nt6YPNDWL8"
      TELEGRAM_CHAT="8255162322"
      
      # Angel Broker API Credentials (REPLACE WITH YOUR ACTUAL VALUES)
      ANGEL_API_KEY="YOUR_API_KEY_HERE"
      ANGEL_CLIENT_CODE="YOUR_CLIENT_CODE_HERE"
      ANGEL_PASSWORD="YOUR_PASSWORD_HERE"
      ANGEL_TOTP_SECRET="YOUR_TOTP_SECRET_HERE"
      
      # Trading Configuration
      SYMBOLS="NIFTY,BANKNIFTY,RELIANCE,INFY,TCS,ICICIBANK,HDFCBANK"
      MODE="LIVE"
      MAX_CONTRACTS_PER_TRADE="1"
      ALLOC_PCT="0.70"
      MAX_DAILY_LOSS="5000"
      MAX_POSITION_SIZE="50000"
      MARKET_HOURS_ONLY="true"
      TIMEZONE="Asia/Kolkata"

  # --------------------------
  # .env file generator script
  # --------------------------
  - path: /usr/local/bin/create_env.sh
    permissions: "0755"
    content: |
      #!/usr/bin/env bash
      source /etc/default/intraday-bot
      cat > "${BOT_DIR}/.env" <<EOF
      # Angel Broker API Credentials
      ANGEL_API_KEY=${ANGEL_API_KEY}
      ANGEL_CLIENT_CODE=${ANGEL_CLIENT_CODE}
      ANGEL_PASSWORD=${ANGEL_PASSWORD}
      ANGEL_TOTP_SECRET=${ANGEL_TOTP_SECRET}
      
      # Telegram Notifications
      TELEGRAM_TOKEN=${TELEGRAM_TOKEN}
      TELEGRAM_CHAT_ID=${TELEGRAM_CHAT}
      
      # Indian Market Symbols (NSE Stocks + Index Futures)
      SYMBOLS=${SYMBOLS}
      
      # Trading Mode (LIVE ONLY - Angel Broker doesn't support paper trading)
      MODE=${MODE}
      
      # Risk Management
      MAX_CONTRACTS_PER_TRADE=${MAX_CONTRACTS_PER_TRADE}
      ALLOC_PCT=${ALLOC_PCT}
      MAX_DAILY_LOSS=${MAX_DAILY_LOSS}
      MAX_POSITION_SIZE=${MAX_POSITION_SIZE}
      
      # Market Hours
      MARKET_HOURS_ONLY=${MARKET_HOURS_ONLY}
      TIMEZONE=${TIMEZONE}
      EOF
      chmod 600 "${BOT_DIR}/.env"

  # --------------------------
  # Telegram sender script
  # --------------------------
  - path: /usr/local/bin/tg.sh
    permissions: "0755"
    content: |
      #!/usr/bin/env bash
      source /etc/default/intraday-bot
      MSG="$1"
      curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT}" \
        -d text="$MSG" >/dev/null 2>&1 || true

  # --------------------------
  # Systemd service - START
  # --------------------------
  - path: /etc/systemd/system/intraday-bot-start.service
    permissions: "0644"
    content: |
      [Unit]
      Description=Intraday Bot - Start Service
      After=network-online.target docker.service
      Wants=network-online.target

      [Service]
      Type=oneshot
      EnvironmentFile=/etc/default/intraday-bot
      WorkingDirectory=/opt/intraday_bot
      ExecStartPre=/usr/local/bin/tg.sh "📈 Starting Intraday Bot..."
      ExecStart=/usr/bin/docker compose up -d --build
      StandardOutput=append:/var/log/intraday_bot/service.log
      StandardError=append:/var/log/intraday_bot/service.log
      RemainAfterExit=yes

  # --------------------------
  # Systemd service - STOP
  # --------------------------
  - path: /etc/systemd/system/intraday-bot-stop.service
    permissions: "0644"
    content: |
      [Unit]
      Description=Intraday Bot - Stop Service
      After=docker.service

      [Service]
      Type=oneshot
      EnvironmentFile=/etc/default/intraday-bot
      WorkingDirectory=/opt/intraday_bot
      ExecStartPre=/usr/local/bin/tg.sh "🛑 Stopping Intraday Bot..."
      ExecStart=/usr/bin/docker compose down
      StandardOutput=append:/var/log/intraday_bot/service.log
      StandardError=append:/var/log/intraday_bot/service.log

  # --------------------------
  # Timer: Start at 09:00 IST
  # --------------------------
  - path: /etc/systemd/system/intraday-bot-start.timer
    permissions: "0644"
    content: |
      [Unit]
      Description=Start Intraday Bot at Market Open (09:00 IST)

      [Timer]
      OnCalendar=Mon-Fri 09:00 Asia/Kolkata
      Persistent=true

      [Install]
      WantedBy=timers.target

  # --------------------------
  # Timer: Stop at 15:45 IST
  # --------------------------
  - path: /etc/systemd/system/intraday-bot-stop.timer
    permissions: "0644"
    content: |
      [Unit]
      Description=Stop Intraday Bot at Market Close (15:45 IST)

      [Timer]
      OnCalendar=Mon-Fri 15:45 Asia/Kolkata
      Persistent=true

      [Install]
      WantedBy=timers.target

  # --------------------------
  # Health Check Service
  # --------------------------
  - path: /etc/systemd/system/intraday-bot-health.service
    permissions: "0644"
    content: |
      [Unit]
      Description=Health Check for Intraday Bot
      After=docker.service

      [Service]
      Type=oneshot
      ExecStart=/usr/local/bin/intraday_health.sh
      StandardOutput=append:/var/log/intraday_bot/health.log
      StandardError=append:/var/log/intraday_bot/health.log


  # --------------------------
  # Health Check Timer (every 2 minutes)
  # --------------------------
  - path: /etc/systemd/system/intraday-bot-health.timer
    permissions: "0644"
    content: |
      [Unit]
      Description=Runs bot health check every 2 minutes

      [Timer]
      OnUnitActiveSec=120s
      Unit=intraday-bot-health.service

      [Install]
      WantedBy=timers.target


  # --------------------------
  # Health Check Script
  # --------------------------
  - path: /usr/local/bin/intraday_health.sh
    permissions: "0755"
    content: |
      #!/usr/bin/env bash
      if ! docker ps --format '{{.Names}}' | grep -q "intraday"; then
        /usr/local/bin/tg.sh "⚠️ Bot container not running — restarting!"
        systemctl start intraday-bot-start.service
      fi

  # --------------------------
  # Log rotation
  # --------------------------
  - path: /etc/logrotate.d/intraday_bot
    permissions: "0644"
    content: |
      /var/log/intraday_bot/*.log {
        daily
        rotate 14
        compress
        missingok
        notifempty
        dateext
      }

  # --------------------------
  # Bootstrap Script
  # --------------------------
  - path: /root/setup_oracle_bot.sh
    permissions: "0755"
    content: |
      #!/usr/bin/env bash
      set -euo pipefail

      echo "[SETUP] Oracle instance bootstrap starting..."

      # timezone
      ln -sf /usr/share/zoneinfo/Asia/Kolkata /etc/localtime

      # install docker
      apt-get update
      apt-get install -y ca-certificates curl gnupg git

      install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
      chmod a+r /etc/apt/keyrings/docker.asc

      echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
        $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | tee /etc/apt/sources.list.d/docker.list > /dev/null

      apt-get update
      apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
      systemctl enable docker
      systemctl start docker

      mkdir -p /opt/intraday_bot
      mkdir -p /var/log/intraday_bot

      source /etc/default/intraday-bot
      if [ ! -d "$BOT_DIR/.git" ]; then
        git clone --branch "$BRANCH" "$REPO_URL" "$BOT_DIR"
      else
        cd "$BOT_DIR" && git pull || true
      fi

      chmod -R 755 /opt/intraday_bot

      # Create .env file from environment variables
      /usr/local/bin/create_env.sh
      echo "[SETUP] Created .env file from environment variables"

      systemctl daemon-reload
      systemctl enable intraday-bot-start.timer
      systemctl enable intraday-bot-stop.timer
      systemctl enable intraday-bot-health.timer
      systemctl start intraday-bot-start.timer
      systemctl start intraday-bot-stop.timer
      systemctl start intraday-bot-health.timer

      /usr/local/bin/tg.sh "☁️ Oracle Instance Boot Complete — Bot Scheduler Online"

runcmd:
  - bash /root/setup_oracle_bot.sh | tee /var/log/oracle-bot-bootstrap.log
