# Oracle Cloud Infrastructure (OCI) Setup Guide
## Complete Guide for ARM Free Tier Deployment

This guide walks you through creating a new OCI account and deploying the MyBot trading bot on the ARM-based free tier.

---

## Table of Contents

1. [OCI Account Creation](#1-oci-account-creation)
2. [Understanding OCI Free Tier](#2-understanding-oci-free-tier)
3. [Initial OCI Console Setup](#3-initial-oci-console-setup)
4. [Network Configuration (VCN)](#4-network-configuration-vcn)
5. [SSH Key Generation](#5-ssh-key-generation)
6. [Creating ARM Compute Instance](#6-creating-arm-compute-instance)
7. [Firewall & Security Configuration](#7-firewall--security-configuration)
8. [Post-Deployment Configuration](#8-post-deployment-configuration)
9. [Monitoring & Maintenance](#9-monitoring--maintenance)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. OCI Account Creation

### Step 1.1: Sign Up for OCI Free Tier

1. **Visit Oracle Cloud**: Go to [https://www.oracle.com/cloud/free/](https://www.oracle.com/cloud/free/)

2. **Click "Start for free"**

3. **Fill in account details**:
   - Country/Territory
   - First Name, Last Name
   - Email address (use a valid email - you'll need to verify it)
   - Password (strong password required)

4. **Verify email**: Check your inbox and click the verification link

5. **Complete registration**:
   - Cloud Account Name (choose carefully - cannot be changed)
   - Home Region (choose closest to India for best latency):
     - **Recommended**: `ap-mumbai-1` (Mumbai, India)
     - Alternative: `ap-hyderabad-1` (Hyderabad, India)
   
   > [!WARNING]
   > **Home Region cannot be changed after selection!** Choose wisely.

6. **Payment verification**:
   - Enter credit/debit card details (for verification only)
   - You will NOT be charged unless you upgrade to paid tier
   - Oracle charges ₹2 (or $0.01) temporarily and refunds immediately

7. **Wait for account provisioning** (usually 5-10 minutes)

8. **Login**: You'll receive an email when your account is ready

---

## 2. Understanding OCI Free Tier

### What's Included (Always Free)

#### Compute (ARM-based)
- **4 ARM-based Ampere A1 cores** (can be split across instances)
- **24 GB RAM total** (can be split across instances)
- **200 GB Block Storage**

**Recommended Configuration for Trading Bot**:
- 1 instance with 2 cores + 12 GB RAM (leaves room for future instances)
- OR 1 instance with 4 cores + 24 GB RAM (maximum for single instance)

#### Network
- **2 VCNs (Virtual Cloud Networks)**
- **2 Load Balancers**
- **10 TB outbound data transfer per month**

#### Storage
- **200 GB Block Storage** (boot + additional volumes)
- **20 GB Object Storage**
- **10 GB Archive Storage**

#### Other Services
- Oracle Autonomous Database (2 instances)
- Monitoring, Notifications, Logging

> [!IMPORTANT]
> Free tier resources are **ALWAYS FREE** - they never expire and you're never charged for them, even after the 30-day trial period ends.

---

## 3. Initial OCI Console Setup

### Step 3.1: First Login

1. Go to [https://cloud.oracle.com/](https://cloud.oracle.com/)
2. Enter your **Cloud Account Name** (from registration)
3. Click **Continue**
4. Login with your email and password

### Step 3.2: Verify Your Region

1. Top right corner shows your current region
2. Ensure it's your home region (e.g., `India East (Mumbai)`)
3. You can subscribe to additional regions, but home region is where free tier resources are available

### Step 3.3: Understand the Console

- **Hamburger menu** (☰) top left: Access all services
- **Region selector**: Top right
- **Profile menu**: Top right (user icon)
- **Compartments**: Logical containers for organizing resources

---

## 4. Network Configuration (VCN)

A Virtual Cloud Network (VCN) is required before creating compute instances.

### Step 4.1: Create VCN Using Wizard

1. **Open VCN Wizard**:
   - Click hamburger menu (☰)
   - Navigate to: **Networking** → **Virtual Cloud Networks**
   - Click **Start VCN Wizard**

2. **Select "Create VCN with Internet Connectivity"**
   - Click **Start VCN Wizard**

3. **Configure VCN**:
   ```
   VCN Name: trading-bot-vcn
   Compartment: (root) or create a new compartment
   VCN IPv4 CIDR Block: 10.0.0.0/16
   Public Subnet CIDR Block: 10.0.0.0/24
   Private Subnet CIDR Block: 10.0.1.0/24
   ```

4. **Click "Next"** → Review configuration

5. **Click "Create"** (takes ~30 seconds)

6. **Wizard creates**:
   - ✅ VCN
   - ✅ Public subnet
   - ✅ Private subnet
   - ✅ Internet Gateway
   - ✅ NAT Gateway
   - ✅ Service Gateway
   - ✅ Route tables
   - ✅ Security lists

### Step 4.2: Configure Security List (Firewall Rules)

1. **Navigate to your VCN**:
   - **Networking** → **Virtual Cloud Networks**
   - Click on `trading-bot-vcn`

2. **Click "Security Lists"** (left menu)

3. **Click on "Default Security List for trading-bot-vcn"**

4. **Add Ingress Rule for SSH**:
   - Click **Add Ingress Rules**
   - Configure:
     ```
     Source Type: CIDR
     Source CIDR: 0.0.0.0/0
     IP Protocol: TCP
     Source Port Range: (leave empty)
     Destination Port Range: 22
     Description: SSH access
     ```
   - Click **Add Ingress Rules**

   > [!CAUTION]
   > Using `0.0.0.0/0` allows SSH from anywhere. For better security, use your specific IP address or IP range.

5. **Verify existing rules**:
   - Should already have rule for port 22 (SSH)
   - Egress rules should allow all outbound traffic

---

## 5. SSH Key Generation

You need an SSH key pair to access your instance.

### Option A: Using OCI Cloud Shell (Easiest)

1. **Open Cloud Shell**:
   - Click the **Developer Tools** icon (>_) in top right corner
   - Wait for Cloud Shell to start

2. **Generate SSH key**:
   ```bash
   ssh-keygen -t rsa -b 4096 -C "oci-trading-bot"
   ```
   - Press Enter to accept default location (`~/.ssh/id_rsa`)
   - Press Enter twice for no passphrase (or set one if you prefer)

3. **Display public key**:
   ```bash
   cat ~/.ssh/id_rsa.pub
   ```
   - Copy the entire output (starts with `ssh-rsa`)
   - Save it in a text file - you'll need this when creating the instance

4. **Download private key** (for later use):
   ```bash
   cat ~/.ssh/id_rsa
   ```
   - Copy the entire output (including `-----BEGIN` and `-----END` lines)
   - Save to your local computer as `oci-trading-bot.pem`

### Option B: Using Your Local Machine (Mac/Linux)

1. **Open Terminal**

2. **Generate SSH key**:
   ```bash
   ssh-keygen -t rsa -b 4096 -f ~/.ssh/oci-trading-bot -C "oci-trading-bot"
   ```

3. **View public key**:
   ```bash
   cat ~/.ssh/oci-trading-bot.pub
   ```
   - Copy the entire output

4. **Set permissions on private key**:
   ```bash
   chmod 600 ~/.ssh/oci-trading-bot
   ```

---

## 6. Creating ARM Compute Instance

### Step 6.1: Launch Instance Creation Wizard

1. **Navigate to Compute**:
   - Hamburger menu (☰) → **Compute** → **Instances**

2. **Click "Create Instance"**

### Step 6.2: Configure Instance Details

#### Basic Information
```
Name: trading-bot-instance
Create in compartment: (root) or your compartment
Availability Domain: (select any - usually AD-1)
```

#### Image and Shape

1. **Click "Edit" next to "Image and shape"**

2. **Change Image**:
   - Click **Change Image**
   - Select **Canonical Ubuntu**
   - Choose **22.04** (not 20.04 or 24.04)
   - **IMPORTANT**: Ensure it says "Arm" or "aarch64" in the description
   - Click **Select Image**

3. **Change Shape**:
   - Click **Change Shape**
   - **Shape series**: Select **Ampere**
   - **Shape name**: Select **VM.Standard.A1.Flex**
   - **Configure**:
     ```
     Number of OCPUs: 2 (or 4 if you want maximum)
     Amount of memory (GB): 12 (or 24 if using 4 OCPUs)
     ```
     
     > [!TIP]
     > Start with 2 OCPUs + 12 GB RAM. This is more than enough for the trading bot and leaves resources for future instances.
   
   - Click **Select Shape**

#### Networking

1. **Primary VNIC information**:
   ```
   VCN: trading-bot-vcn
   Subnet: Public Subnet-trading-bot-vcn (regional)
   ```

2. **Public IP address**: Select **Assign a public IPv4 address**

   > [!IMPORTANT]
   > You MUST assign a public IP to SSH into the instance!

#### Add SSH Keys

1. **Select "Paste public keys"**
2. **Paste your SSH public key** (from Step 5)

#### Boot Volume

```
Boot volume size (GB): 50 GB (default, sufficient for bot)
```

> [!NOTE]
> You have 200 GB total free storage. 50 GB boot volume is plenty for the trading bot.

### Step 6.3: Add Cloud-Init Script

1. **Click "Show advanced options"** at the bottom

2. **Click "Management" tab**

3. **Initialization script**: Select **Paste cloud-init script**

4. **Paste the contents of your `cloud-init.sh` file**:
   - Open `/Users/mathan/Documents/GitHub/MyBot/cloud-init.sh`
   - Copy the ENTIRE contents
   - Paste into the text box

   > [!WARNING]
   > Make sure you copy the COMPLETE file, starting with `#cloud-config`

### Step 6.4: Create Instance

1. **Review all settings**

2. **Click "Create"**

3. **Wait for provisioning** (5-10 minutes):
   - Status will show: **PROVISIONING** → **RUNNING**
   - Note the **Public IP Address** once it appears

---

## 7. Firewall & Security Configuration

### Step 7.1: Configure OS Firewall (Ubuntu)

The cloud-init script handles Docker installation, but you may need to configure the OS firewall later if you add services.

For now, the default Ubuntu firewall is disabled, which is fine since OCI Security Lists control access.

### Step 7.2: Verify Security List Rules

1. **Go to your instance details**:
   - **Compute** → **Instances** → Click on `trading-bot-instance`

2. **Click on the subnet name** under "Primary VNIC"

3. **Click "Security Lists"**

4. **Verify rules**:
   - **Ingress**: Port 22 (SSH) from 0.0.0.0/0
   - **Egress**: All traffic allowed

---

## 8. Post-Deployment Configuration

### Step 8.1: Wait for Cloud-Init to Complete

After instance status shows **RUNNING**, wait an additional **5-10 minutes** for cloud-init to:
- Install Docker
- Clone your repository
- Set up systemd services and timers

### Step 8.2: Connect via SSH

#### From Mac/Linux Terminal:

```bash
ssh -i ~/.ssh/oci-trading-bot ubuntu@<PUBLIC_IP_ADDRESS>
```

Replace `<PUBLIC_IP_ADDRESS>` with your instance's public IP.

#### First-time connection:
- You'll see a message about authenticity of host
- Type `yes` and press Enter

### Step 8.3: Verify Bootstrap Completion

1. **Check bootstrap log**:
   ```bash
   tail -100 /var/log/oracle-bot-bootstrap.log
   ```
   
   Look for: `[SETUP] Oracle instance bootstrap starting...` and completion messages

2. **Check Docker installation**:
   ```bash
   docker --version
   docker compose version
   ```
   
   Expected output:
   ```
   Docker version 24.x.x
   Docker Compose version v2.x.x
   ```

3. **Check bot directory**:
   ```bash
   ls -la /opt/intraday_bot
   ```
   
   Should show: `src/`, `Dockerfile`, `docker-compose.yml`, `.env`, etc.

4. **Check systemd timers**:
   ```bash
   systemctl status intraday-bot-start.timer
   systemctl status intraday-bot-stop.timer
   systemctl status intraday-bot-health.timer
   ```
   
   All should show: **Active: active (waiting)**

### Step 8.4: Configure Angel Broker Credentials

> [!IMPORTANT]
> This is the MOST CRITICAL step! The bot will not work without your actual credentials.

1. **Edit environment file**:
   ```bash
   sudo nano /etc/default/intraday-bot
   ```

2. **Replace placeholder values** with your actual Angel Broker credentials:
   ```bash
   # Find these lines and replace with YOUR values:
   ANGEL_API_KEY="YOUR_ACTUAL_API_KEY"
   ANGEL_CLIENT_CODE="YOUR_ACTUAL_CLIENT_CODE"
   ANGEL_PASSWORD="YOUR_ACTUAL_PASSWORD"
   ANGEL_TOTP_SECRET="YOUR_ACTUAL_TOTP_SECRET"
   ```

3. **Save and exit**:
   - Press `Ctrl + X`
   - Press `Y` to confirm
   - Press `Enter`

4. **Regenerate .env file**:
   ```bash
   sudo /usr/local/bin/create_env.sh
   ```

5. **Verify .env file**:
   ```bash
   sudo cat /opt/intraday_bot/.env
   ```
   
   Ensure your actual credentials are present (not placeholders)

### Step 8.5: Test Manual Start

1. **Start the bot manually**:
   ```bash
   sudo systemctl start intraday-bot-start.service
   ```

2. **Check if container is running**:
   ```bash
   docker ps
   ```
   
   Expected output:
   ```
   CONTAINER ID   IMAGE          COMMAND              STATUS
   xxxxx          ...            "python -u main.py"  Up X seconds
   ```

3. **Check bot logs**:
   ```bash
   docker logs intraday_options_bot_angel
   ```
   
   Look for:
   - ✅ "Starting Intraday Options Bot"
   - ✅ Angel Broker login success
   - ❌ Any error messages

4. **Check Telegram**:
   - You should receive: "🚀 Bot Starting Up (Mode: LIVE)"

5. **Stop the bot** (since it will auto-start at 09:00):
   ```bash
   sudo systemctl start intraday-bot-stop.service
   ```

---

## 9. Monitoring & Maintenance

### Daily Monitoring

#### Check Bot Status
```bash
# SSH into instance
ssh -i ~/.ssh/oci-trading-bot ubuntu@<PUBLIC_IP>

# Check if container is running
docker ps

# View recent logs
docker logs --tail 50 intraday_options_bot_angel

# View service logs
tail -50 /var/log/intraday_bot/service.log

# View health check logs
tail -50 /var/log/intraday_bot/health.log
```

#### Check Systemd Timers
```bash
# List all timers
systemctl list-timers

# Check when bot will start/stop next
systemctl status intraday-bot-start.timer
systemctl status intraday-bot-stop.timer
```

### Weekly Maintenance

#### Update System Packages
```bash
sudo apt update && sudo apt upgrade -y
```

#### Pull Latest Bot Code
```bash
cd /opt/intraday_bot
sudo git pull
sudo systemctl restart intraday-bot-start.service  # Only during market hours
```

#### Check Disk Usage
```bash
df -h
du -sh /opt/intraday_bot/logs
du -sh /opt/intraday_bot/audit
```

#### Rotate Logs (automatic via logrotate)
Logs are automatically rotated daily and kept for 14 days.

### Monthly Maintenance

#### Review OCI Billing
1. Go to OCI Console
2. **Governance & Administration** → **Billing & Cost Management**
3. Verify you're still on free tier (should be $0.00)

#### Backup Trade Audit Data
```bash
# Download audit CSV files
scp -i ~/.ssh/oci-trading-bot ubuntu@<PUBLIC_IP>:/opt/intraday_bot/audit/*.csv ~/Desktop/
```

---

## 10. Troubleshooting

### Issue: Cannot SSH into Instance

**Symptoms**: Connection timeout or "Connection refused"

**Solutions**:

1. **Verify Security List**:
   - OCI Console → VCN → Security Lists
   - Ensure port 22 is open from your IP

2. **Check instance status**:
   - Should be **RUNNING** (not STOPPED or TERMINATED)

3. **Verify public IP**:
   - Use the correct public IP from instance details

4. **Check SSH key**:
   ```bash
   # Verify key permissions
   chmod 600 ~/.ssh/oci-trading-bot
   
   # Try verbose mode
   ssh -v -i ~/.ssh/oci-trading-bot ubuntu@<PUBLIC_IP>
   ```

5. **Try OCI Cloud Shell**:
   - Open Cloud Shell in OCI Console
   - Upload your private key
   - SSH from there

---

### Issue: Cloud-Init Failed

**Symptoms**: Docker not installed, bot directory missing

**Solutions**:

1. **Check cloud-init logs**:
   ```bash
   sudo cat /var/log/cloud-init-output.log
   sudo cat /var/log/oracle-bot-bootstrap.log
   ```

2. **Re-run bootstrap manually**:
   ```bash
   sudo bash /root/setup_oracle_bot.sh
   ```

3. **Check for errors**:
   - Look for "apt-get" errors (network issues)
   - Look for "git clone" errors (repository access)

---

### Issue: Bot Container Not Starting

**Symptoms**: `docker ps` shows no container

**Solutions**:

1. **Check Docker service**:
   ```bash
   sudo systemctl status docker
   ```

2. **Try manual start**:
   ```bash
   cd /opt/intraday_bot
   sudo docker compose up
   ```
   
   Look for error messages

3. **Check .env file**:
   ```bash
   sudo cat /opt/intraday_bot/.env
   ```
   
   Ensure credentials are correct (not placeholders)

4. **Check Docker logs**:
   ```bash
   sudo journalctl -u docker -n 50
   ```

---

### Issue: Bot Crashes Immediately

**Symptoms**: Container starts then stops, error in logs

**Solutions**:

1. **Check bot logs**:
   ```bash
   docker logs intraday_options_bot_angel
   ```

2. **Common errors**:
   - **"Invalid credentials"**: Check Angel Broker API key, password, TOTP
   - **"Module not found"**: Rebuild Docker image
     ```bash
     cd /opt/intraday_bot
     sudo docker compose build --no-cache
     sudo docker compose up -d
     ```
   - **"Connection refused"**: Check network connectivity
     ```bash
     ping -c 3 google.com
     ```

---

### Issue: Telegram Notifications Not Working

**Symptoms**: No messages received in Telegram

**Solutions**:

1. **Verify Telegram token and chat ID**:
   ```bash
   sudo cat /etc/default/intraday-bot | grep TELEGRAM
   ```

2. **Test Telegram manually**:
   ```bash
   /usr/local/bin/tg.sh "Test message from OCI"
   ```

3. **Check if bot is blocked**:
   - Open Telegram
   - Search for your bot
   - Ensure you haven't blocked it

---

### Issue: Timer Not Triggering

**Symptoms**: Bot doesn't start at 09:00 or stop at 15:45

**Solutions**:

1. **Check timer status**:
   ```bash
   systemctl status intraday-bot-start.timer
   systemctl status intraday-bot-stop.timer
   ```

2. **Check system timezone**:
   ```bash
   timedatectl
   ```
   
   Should show: `Time zone: Asia/Kolkata`

3. **Check next trigger time**:
   ```bash
   systemctl list-timers
   ```

4. **Manually trigger timer**:
   ```bash
   sudo systemctl start intraday-bot-start.service
   ```

---

### Issue: Out of Disk Space

**Symptoms**: "No space left on device" errors

**Solutions**:

1. **Check disk usage**:
   ```bash
   df -h
   ```

2. **Clean Docker images**:
   ```bash
   docker system prune -a
   ```

3. **Clean old logs**:
   ```bash
   sudo rm /var/log/intraday_bot/*.log.*.gz
   ```

4. **Increase boot volume** (if needed):
   - OCI Console → Instance → Boot Volume
   - Click "Edit"
   - Increase size (within 200 GB free tier limit)

---

## Quick Reference Commands

### Instance Management
```bash
# SSH into instance
ssh -i ~/.ssh/oci-trading-bot ubuntu@<PUBLIC_IP>

# Check instance status (from OCI Console)
# Compute → Instances → trading-bot-instance
```

### Bot Management
```bash
# Start bot manually
sudo systemctl start intraday-bot-start.service

# Stop bot manually
sudo systemctl start intraday-bot-stop.service

# Check container status
docker ps

# View logs
docker logs intraday_options_bot_angel
docker logs -f intraday_options_bot_angel  # Follow logs

# Restart bot
cd /opt/intraday_bot
sudo docker compose restart
```

### Systemd Timers
```bash
# Check timer status
systemctl list-timers
systemctl status intraday-bot-start.timer
systemctl status intraday-bot-stop.timer

# Disable auto-start (for testing)
sudo systemctl stop intraday-bot-start.timer
sudo systemctl disable intraday-bot-start.timer

# Re-enable auto-start
sudo systemctl enable intraday-bot-start.timer
sudo systemctl start intraday-bot-start.timer
```

### Logs
```bash
# Service logs
tail -f /var/log/intraday_bot/service.log

# Health check logs
tail -f /var/log/intraday_bot/health.log

# Cloud-init logs
sudo cat /var/log/cloud-init-output.log
sudo cat /var/log/oracle-bot-bootstrap.log
```

### System Maintenance
```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Update bot code
cd /opt/intraday_bot
sudo git pull
sudo docker compose build
sudo docker compose up -d

# Check disk usage
df -h
du -sh /opt/intraday_bot/*
```

---

## Cost Monitoring

### Always Free Resources Used

Your trading bot setup uses:
- ✅ **Compute**: 2 OCPUs + 12 GB RAM (within 4 OCPU + 24 GB limit)
- ✅ **Storage**: ~50 GB boot volume (within 200 GB limit)
- ✅ **Network**: VCN, public IP, outbound data (within limits)

**Monthly cost**: **$0.00** (Always Free)

### How to Verify You're on Free Tier

1. **OCI Console** → **Governance & Administration** → **Billing & Cost Management**
2. Check **Cost Analysis**
3. Should show: **$0.00** for Compute, Storage, Network

> [!WARNING]
> If you accidentally create resources outside free tier (e.g., x86 instances, extra storage), you WILL be charged. Always verify before creating resources!

---

## Security Best Practices

### 1. Restrict SSH Access

Instead of allowing SSH from anywhere (`0.0.0.0/0`), restrict to your IP:

1. Find your public IP: [https://whatismyipaddress.com/](https://whatismyipaddress.com/)
2. Update Security List ingress rule:
   ```
   Source CIDR: <YOUR_IP>/32
   ```

### 2. Use SSH Key Passphrase

When generating SSH keys, use a strong passphrase for the private key.

### 3. Regular Updates

```bash
# Weekly system updates
sudo apt update && sudo apt upgrade -y

# Monthly Docker image updates
cd /opt/intraday_bot
sudo docker compose pull
sudo docker compose up -d
```

### 4. Monitor Access Logs

```bash
# Check SSH login attempts
sudo tail -50 /var/log/auth.log
```

### 5. Enable OCI Audit Logging

1. **OCI Console** → **Governance & Administration** → **Audit**
2. Review login attempts and resource changes

---

## Next Steps

After completing this setup:

1. ✅ **Test the bot** during market hours (09:00-15:30 IST)
2. ✅ **Monitor Telegram notifications**
3. ✅ **Review trade audit logs** in `/opt/intraday_bot/audit/`
4. ✅ **Set up local monitoring** (optional):
   - Create a cron job to ping the instance
   - Set up alerts for downtime
5. ✅ **Backup strategy**:
   - Regularly download audit CSV files
   - Keep a local copy of your configuration

---

## Support & Resources

### OCI Documentation
- [OCI Free Tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm)
- [Compute Instances](https://docs.oracle.com/en-us/iaas/Content/Compute/home.htm)
- [VCN Documentation](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/overview.htm)

### Community
- [OCI Community Forums](https://community.oracle.com/customerconnect/categories/oci)
- [OCI Discord](https://discord.gg/oracle-cloud)

### Emergency Contacts
- **OCI Support**: Available through OCI Console (free tier has limited support)
- **Billing Issues**: [https://www.oracle.com/cloud/contact.html](https://www.oracle.com/cloud/contact.html)

---

## Appendix: Alternative Configurations

### Configuration A: Maximum Resources (Single Instance)
```
OCPUs: 4
RAM: 24 GB
Boot Volume: 100 GB
```
Use this if you plan to run only one instance and want maximum performance.

### Configuration B: Multiple Instances
```
Instance 1 (Trading Bot): 2 OCPUs, 12 GB RAM, 50 GB storage
Instance 2 (Future use): 2 OCPUs, 12 GB RAM, 50 GB storage
```
Use this if you plan to run multiple services or test environments.

### Configuration C: Minimal (Testing)
```
OCPUs: 1
RAM: 6 GB
Boot Volume: 50 GB
```
Use this for testing before committing to full deployment.

---

**Good luck with your OCI deployment! 🚀**

If you encounter any issues not covered in this guide, check the troubleshooting section or reach out for support.
