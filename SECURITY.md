# Security & Privacy Protection

## 🔒 Security Status: PROTECTED

This repository is **PUBLIC** and all sensitive data is protected from being committed.

---

## ⚠️ CRITICAL SECURITY FIX (Dec 11, 2025)

### Issue Found
The `.env` file containing API keys, passwords, and tokens was accidentally tracked in git history (5+ commits).

### Actions Taken
1. ✅ Removed `.env` from git tracking: `git rm --cached .env`
2. ✅ Created `.env.example` template (safe to commit)
3. ✅ Enhanced `.gitignore` with comprehensive patterns
4. ✅ Verified no other sensitive files are tracked

### Required Action
**⚠️ ALL API credentials in `.env` should be regenerated immediately** since they were exposed in public git history:
- Angel One API Key
- Angel One TOTP Secret
- Telegram Bot Tokens (both ANGEL and IBKR)
- Any other passwords or secrets

---

## 🛡️ Protected Files & Patterns

The following are automatically excluded from git commits:

### Configuration Files
- `.env` (contains all secrets)
- `.env.local`, `.env.production`, etc.
- `docker-compose.override.yml`

### SSH & Keys
- `ssh_keys/` directory
- `*.pem`, `*.key`, `*.pub` files
- `id_rsa*`, `id_ecdsa*`, `id_ed25519*`

### Credentials & Secrets
- Files with `*secret*` in name
- Files with `*credential*` in name
- Files with `*password*` in name
- `*.pfx`, `*.p12` certificate files

### Runtime Data
- `logs/` directory
- `audit/` directory
- `*.log` files
- `*.csv` audit files

### Python Build Artifacts
- `__pycache__/`
- `*.pyc`, `*.pyo`, `*.pyd`
- Virtual environments (`venv/`, `.venv/`)

---

## ✅ Safe to Commit

The following files are safe and **should** be committed:
- `.env.example` - Template with placeholder values
- `.gitignore` - Protection rules
- All source code (`src/**/*.py`)
- Documentation (`*.md` files)
- `requirements.txt`
- `Dockerfile`, `docker-compose.yml`

---

## 📋 Setup Instructions for New Developers

### 1. Clone Repository
```bash
git clone https://github.com/mathankanagaraj/MyBot.git
cd MyBot
```

### 2. Create Environment File
```bash
cp .env.example .env
```

### 3. Fill in Your Credentials
Edit `.env` and replace placeholders with your actual values:
```bash
# Angel Broker API Credentials
ANGEL_API_KEY=your_actual_api_key
ANGEL_CLIENT_CODE=your_actual_client_code
ANGEL_PASSWORD=your_actual_password
ANGEL_TOTP_SECRET=your_actual_totp_secret

# Telegram Notifications - ANGEL ONE BOT
TELEGRAM_TOKEN=your_actual_telegram_bot_token
TELEGRAM_CHAT_ID=your_actual_telegram_chat_id

# Telegram Notifications - IBKR BOT
IBKR_TELEGRAM_TOKEN=your_actual_ibkr_telegram_bot_token
IBKR_TELEGRAM_CHAT_ID=your_actual_ibkr_telegram_chat_id
```

### 4. Verify Protection
Before committing any changes:
```bash
# Check what files would be committed
git status

# Verify .env is NOT listed (should show "Untracked files" or nothing)
# If .env appears, DO NOT commit!
```

---

## 🚨 Emergency: If Secrets Are Exposed

If you accidentally commit sensitive data:

### Immediate Actions
1. **Rotate ALL credentials immediately**
   - Generate new API keys
   - Create new Telegram bots
   - Change all passwords

2. **Remove from git history**
   ```bash
   # Remove file from git tracking
   git rm --cached <sensitive-file>
   
   # Commit the removal
   git commit -m "Remove sensitive file from tracking"
   
   # Push changes
   git push origin main
   ```

3. **For files already in history**, consider using:
   - `git filter-branch` (destructive, rewrites history)
   - `BFG Repo-Cleaner` (recommended for large repos)
   - Or create a new repository and migrate code without history

### Prevention
- Always review `git status` before committing
- Use `git diff --staged` to see what will be committed
- Consider using pre-commit hooks to block sensitive patterns

---

## 🔐 Telegram Bot Separation

This bot uses **separate Telegram bots** for Angel One and IBKR notifications:

### Angel One Bot
- Uses: `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`
- Sends: Indian market (NSE) notifications
- Identifies as: `[AngelOne]` in messages

### IBKR Bot
- Uses: `IBKR_TELEGRAM_TOKEN` and `IBKR_TELEGRAM_CHAT_ID`
- Sends: US market notifications
- Identifies as: `[IBKR]` in messages

### Benefits
- **Separate notifications** - Don't mix Indian and US market alerts
- **Independent monitoring** - Track each bot separately
- **Flexible routing** - Send to different chat groups/channels
- **Clear identification** - Know which bot sent which message

### Configuration
```env
# Indian Market Bot (Angel One)
TELEGRAM_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz123456789
TELEGRAM_CHAT_ID=123456789

# US Market Bot (IBKR)
IBKR_TELEGRAM_TOKEN=9876543210:ZYXwvuTSRqponMLKjihGFEdcBA987654321
IBKR_TELEGRAM_CHAT_ID=987654321  # Can be same or different
```

---

## 📊 Audit & Compliance

### What Gets Logged
- Trade execution data (in `audit/` directory)
- Application logs (in `logs/` directory)
- Both are excluded from git commits

### Data Retention
- Logs rotate daily
- Audit files persist until manually deleted
- No sensitive credentials are logged

---

## 🔍 Verification Checklist

Before pushing to GitHub:
- [ ] `.env` is NOT in `git status` output
- [ ] No `*.pem` or `*.key` files in `git status`
- [ ] No API keys visible in source code
- [ ] No passwords in comments
- [ ] `.env.example` only has placeholder values
- [ ] All secrets are in `.env` (gitignored)

---

## 📞 Security Contact

If you discover a security vulnerability:
1. **DO NOT** create a public GitHub issue
2. Contact repository owner directly
3. Provide details of the vulnerability
4. Allow time for fix before public disclosure

---

**Last Updated:** December 11, 2025  
**Status:** All sensitive data protected ✅
