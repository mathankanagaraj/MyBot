#cloud-config
ssh_authorized_keys:
  - ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQDQM6zuglVy8qcOqQ6pe9n2heo/Y1CrH3CZCpXocGIPBLV+bAWktmJKbYAl8Ml1OUNCKdEdutybgBhp1eiMkQ2Gx/PRn4XEpVouwOpD3N8vA7tNpWTAs8LEiZpd2UYGwhHw9XuS6E0mQfiskEXXztl+5JbRFn3nnM9PJkBCY3xYK6qOlcfVcFyd8QFRnNKD0WwJPqfBXdulagPCMkYaH/s4qZXjnpcXcjaQLaluvHQgTI2+8bvGKC71U6RCkU1LM5jFysBYpLvvWIwUaYGA0qGCUZOathDJxZv0qW/UU7Nu+FmtJbbNdu4fS3fvnJJK7GVxEA2oVODzI5jdvDUy3JahJ0sEQOuKPCVeWhL7Y7ueUegqa3yK1irzRjwhe/16rBwCn3XyBQmEh4MA9u4GrCh2PYBatIsdfVcYwQrlfshl9mUfw17izjjuGxDx6CJcY8zimy84XkvEXc4kL0r0VUW5MequtKWA/opRrAkyjOaVq31oYfrPuL9qIOX2Pce2la9dwivUMe7Gu81d627Ct7GMARhgqdJzPKj0qaBbvMkpBmCl9MAGfa62nakC0hPO95gKl3THImKaPb2iQECi/HZqvSXGeImOeowbmcwXfU4CIr0MgB9oAX7t+0TSCTHx29b1BVmE0xLDXWJ+JoaUKc3xKNbMqoQNBJwfmQA8gyrmpw== oci-cloud-shell

package_update: true
package_upgrade: true

write_files:
  # --------------------------
  # Global Environment Variables
  # --------------------------
  - path: /etc/default/intraday-bot
    permissions: "0644"
    content: |
      BOT_DIR="/opt/intraday_bot"
      REPO_URL="https://github.com/mathankanagaraj/MyBot.git"
      BRANCH="main"
      TELEGRAM_TOKEN="8312696115:AAFCdD1OdeMfpQ0KVioOoLu85nt6YPNDWL8"
      TELEGRAM_CHAT="8255162322"

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
  # Systemd service (start/stop)
  # --------------------------
  - path: /etc/systemd/system/intraday-bot.service
    permissions: "0644"
    content: |
      [Unit]
      Description=Intraday Bot (Docker Compose)
      After=network-online.target docker.service
      Wants=network-online.target

      [Service]
      Type=oneshot
      EnvironmentFile=/etc/default/intraday-bot
      WorkingDirectory=/opt/intraday_bot
      ExecStartPre=/usr/local/bin/tg.sh "📈 Starting Intraday Bot..."
      ExecStart=/usr/bin/docker compose up -d --build
      ExecStop=/usr/local/bin/tg.sh "🛑 Stopping Intraday Bot..."
      ExecStop=/usr/bin/docker compose down
      StandardOutput=append:/var/log/intraday_bot/service.log
      StandardError=append:/var/log/intraday_bot/service.log
      RemainAfterExit=yes

      [Install]
      WantedBy=multi-user.target

  # --------------------------
  # Timer: Start 09:00, Stop 15:45 IST
  # --------------------------
  - path: /etc/systemd/system/intraday-bot.timer
    permissions: "0644"
    content: |
      [Unit]
      Description=Scheduled Start/Stop for Intraday Bot (Indian Market)

      [Timer]
      OnCalendar=Mon-Fri 09:00 Asia/Kolkata
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
        systemctl start intraday-bot.service
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

      systemctl daemon-reload
      systemctl enable intraday-bot.timer
      systemctl enable intraday-bot-health.timer
      systemctl start intraday-bot.timer
      systemctl start intraday-bot-health.timer

      /usr/local/bin/tg.sh "☁️ Oracle Instance Boot Complete — Bot Scheduler Online"

runcmd:
  - bash /root/setup_oracle_bot.sh | tee /var/log/oracle-bot-bootstrap.log
