# Raspberry Pi Setup Guide

Step-by-step guide to deploy plant-ops-ai on a Raspberry Pi. Each section has the commands you need to run.

## Prerequisites

Before starting, make sure you have:

- Raspberry Pi running a recent Raspberry Pi OS (Bookworm or later) with network access
- Python 3.11 or newer (`python3 --version` to check)
- `pip` installed (`python3 -m pip --version`)
- `git` installed (`git --version`)
- `farmctl.py` is included in the repo at `farmctl/farmctl.py` -- after cloning you should be able to run `python3 farmctl/farmctl.py status` and see sensor output
- Arduino connected via USB (typically `/dev/ttyACM0`)
- Pi camera module connected and enabled

## 1. Clone the Repository

```bash
git clone <repo-url> ~/plant-ops-ai
cd ~/plant-ops-ai
```

## 2. Create Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
```

After this, your shell prompt should show `(venv)` at the beginning. All subsequent commands assume the virtual environment is active.

## 3. Install Dependencies

```bash
pip install -r requirements.txt
```

This installs: `anthropic`, `python-telegram-bot`, `python-dotenv`, `pyyaml`, `apscheduler`.

## 4. Configure Environment Variables

```bash
cp .env.example .env
nano .env
```

Fill in each variable:

| Variable | Where to get it | Example |
|----------|----------------|---------|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) -- create an API key | `sk-ant-api03-...` |
| `TELEGRAM_BOT_TOKEN` | Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, follow prompts | `123456789:AAF...` |
| `TELEGRAM_CHAT_ID` | Message [@userinfobot](https://t.me/userinfobot) on Telegram, it replies with your chat ID | `987654321` |
| `FARMCTL_PATH` | Path to farmctl.py (defaults to in-repo `farmctl/farmctl.py`) | Leave unset or `/home/pi/plant-ops-ai/farmctl/farmctl.py` |
| `SERIAL_PORT` | Arduino serial port (check with `ls /dev/ttyACM*`) | `/dev/ttyACM0` |
| `CLAUDE_MODEL` | Which Claude model to use (Sonnet recommended) | `claude-sonnet-4-6` |
| `DATA_DIR` | Where to store logs and cached data | `/home/pi/plant-ops-ai/data` |
| `AGENT_MODE` | Start with `dry-run`, switch to `live` when ready | `dry-run` |
| `WEATHER_LAT` | (Optional) Latitude for outdoor weather via Open-Meteo — no API key needed. When set, Claude sees current outdoor temp/humidity alongside your indoor sensor data. Find your coordinates at [latlong.net](https://www.latlong.net/) | `24.1477` |
| `WEATHER_LON` | (Optional) Longitude paired with `WEATHER_LAT` | `120.6736` |

Save and exit nano (`Ctrl+O`, `Enter`, `Ctrl+X`).

## 5. Configure Plant Profile

You have two options:

**Option A -- Edit the YAML file directly:**

```bash
nano config/plant_profile.yaml
```

Set the `plant.name`, `plant.growth_stage`, and `plant.planted_date` fields. The `ideal_conditions` will be populated automatically when the AI researches your plant.

**Option B -- Use Telegram after the bot starts:**

Send `/setplant basil` (or whatever your plant is) to the bot. It will prompt you to select the growth stage and then research optimal conditions via Claude.

## 6. Test with Mock Sensors

This tests the full pipeline using fake sensor data, without touching any hardware:

```bash
cd ~/plant-ops-ai
source venv/bin/activate
python3 -m src.plant_agent --once --dry-run --mock
```

You should see a formatted summary with mock sensor readings and an AI decision. If you get API errors, check your `ANTHROPIC_API_KEY` in `.env`.

## 7. Test with Real Sensors

This reads from the actual Arduino sensors but does not execute any actions:

```bash
python3 -m src.plant_agent --once --dry-run
```

You should see real sensor values (temperature, humidity, CO2, light, soil moisture). If sensor reading fails, check:
- Is the Arduino connected? (`ls /dev/ttyACM*`)
- Does `farmctl.py status` work on its own? (`python3 farmctl/farmctl.py status` from the project root)
- Is `FARMCTL_PATH` correct in `.env` (or leave it unset to use the in-repo default)?

## 8. Start the Telegram Bot

```bash
python3 -m bot.telegram_bot
```

The bot will:
- Start listening for Telegram commands
- Run the first automated check 10 seconds after startup
- Run subsequent checks every hour

Open Telegram, find your bot, and send `/start`. Try `/status` to see live sensor readings.

When you are satisfied everything works, switch to live mode:
- Send `/mode live` in Telegram, or
- Edit `.env` and set `AGENT_MODE=live`, then restart the bot

## 9. Run as a systemd Service and Install Watchdog

To keep the bot running after you close SSH and auto-restart on boot, use the included install script. It automatically detects your username and paths:

```bash
cd ~/plant-ops-ai
bash deploy/install.sh
sudo systemctl start plant-ops-ai
```

Check that it is running:

```bash
sudo systemctl status plant-ops-ai
```

Useful service commands:

```bash
# View live logs
sudo journalctl -u plant-ops-ai -f

# Restart after config changes
sudo systemctl restart plant-ops-ai

# Stop the service
sudo systemctl stop plant-ops-ai
```

### Install the Watchdog (recommended)

The systemd `Restart=always` policy handles clean crashes, but if the bot process **hangs** (stuck on a network call, serial read, etc.) it stays "running" and systemd never restarts it. The watchdog catches this by monitoring a heartbeat file that the bot updates every 5 minutes.

Run the watchdog installer **once** after deploying:

```bash
cd ~/plant-ops-ai
sudo bash deploy/install_watchdog.sh
```

This does two things:

1. **Adds a root cron job** — runs `deploy/watchdog.sh` every 5 minutes. If the service is not active, or if the heartbeat file is more than 10 minutes old, it restarts the service and logs the event to `data/watchdog.log`.

2. **Configures a sudoers rule** — allows the Pi user to run `sudo systemctl restart plant-ops-ai` without a password, which is required by the Telegram `/restart` command (see below).

### Telegram /restart command

Send `/restart` to the bot at any time to trigger a graceful service restart from Telegram:

- The bot replies with a confirmation, then issues `sudo systemctl restart plant-ops-ai`
- The process exits and systemd brings it back within ~30 seconds
- Useful after a `git pull` when you want to reload without SSHing in

> **Note:** `/restart` only works when the bot is still responsive. If the bot is completely dead or hung, the watchdog (above) will restart it automatically — or you can SSH in and run `sudo systemctl restart plant-ops-ai` manually.

## 10. Monitoring

### Log Files

All runtime data is stored in the `data/` directory (or wherever `DATA_DIR` points):

| File | Contents |
|------|----------|
| `data/decisions.jsonl` | Every AI decision: timestamp, action, reason, whether it was executed |
| `data/sensor_history.jsonl` | Every sensor reading: temp, humidity, CO2, light, soil moisture |
| `data/plant_knowledge.md` | Cached plant care research from Claude |
| `data/plant_latest.jpg` | Most recent plant photo |
| `data/.paused` | Exists when monitoring is paused via `/pause` |

View the last 10 decisions:

```bash
tail -10 ~/plant-ops-ai/data/decisions.jsonl | python3 -m json.tool
```

View the last sensor reading:

```bash
tail -1 ~/plant-ops-ai/data/sensor_history.jsonl | python3 -m json.tool
```

### Checking Status

From Telegram: send `/status` for current sensors, `/history` for recent decisions.

From the Pi:

```bash
# Service status
sudo systemctl status plant-ops-ai

# Live log output
sudo journalctl -u plant-ops-ai -f

# Quick one-off check
cd ~/plant-ops-ai && source venv/bin/activate
python3 -m src.plant_agent --once --dry-run --verbose
```

## 11. Emergency Stop

To immediately halt all automated actions:

```bash
touch /tmp/plant-agent-stop
```

When this file exists, the safety layer blocks all hardware commands. The bot continues running and responding to Telegram commands, but no automated actions will be executed.

To resume:

```bash
rm /tmp/plant-agent-stop
```

You can also use the Telegram `/pause` command, which pauses scheduled checks (but still allows manual commands).

## 12. Updating

When you push new code from your development machine:

```bash
cd ~/plant-ops-ai
source venv/bin/activate
git pull
pip install -r requirements.txt
sudo systemctl restart plant-ops-ai
```

## 13. Troubleshooting

### Bot is dead — sends no response to Telegram messages

Work through these checks in order:

**1. Check the service status:**

```bash
sudo systemctl status plant-ops-ai
```

If the service shows `failed` or `inactive`, restart it:

```bash
sudo systemctl restart plant-ops-ai
```

**2. Check live logs for the root cause:**

```bash
sudo journalctl -u plant-ops-ai -n 100 --no-pager
```

Common error patterns and fixes:

| Log message | Cause | Fix |
|-------------|-------|-----|
| `Conflict (409): another bot instance...` | Two instances running same token | `sudo systemctl stop plant-ops-ai; sudo pkill -f telegram_bot; sudo systemctl start plant-ops-ai` |
| `TELEGRAM_BOT_TOKEN environment variable is required` | `.env` missing or token not set | Check `~/.env` or `~/plant-ops-ai/.env` |
| `getaddrinfo failed` / `Network is unreachable` | No internet on the Pi | Check Pi network; Telegram needs internet access |
| `Start request repeated too quickly` | Crash loop hit systemd rate limit | `sudo systemctl reset-failed plant-ops-ai; sudo systemctl start plant-ops-ai` |

**3. Check if the process is hung (running but not responding):**

```bash
# Check heartbeat age — should be < 5 minutes
ls -la ~/plant-ops-ai/data/.heartbeat
date

# If stale (>10 min old), the bot is hung. Force restart:
sudo systemctl restart plant-ops-ai
```

**4. Trigger restart remotely via Telegram (if partially responsive):**

If the bot is still alive but sluggish, send `/restart` — it will acknowledge and restart the service within 30 seconds.

**5. Check watchdog logs:**

```bash
cat ~/plant-ops-ai/data/watchdog.log
```

If the watchdog has been restarting the service repeatedly, it means the bot keeps hanging or crashing — investigate the service logs for a root cause.

---

### Bot does not start

```
TELEGRAM_BOT_TOKEN environment variable is required
```

Your `.env` file is missing or the token is not set. Check that `/home/pi/plant-ops-ai/.env` exists and contains `TELEGRAM_BOT_TOKEN=...`.

### Sensor reading fails

```
Sensor read failed: ...
```

- Check Arduino USB connection: `ls /dev/ttyACM*`
- Test farmctl.py directly: `python3 farmctl/farmctl.py status`
- Verify `FARMCTL_PATH` in `.env` matches the actual location
- Check serial port permissions: your user may need to be in the `dialout` group:
  ```bash
  sudo usermod -a -G dialout pi
  ```
  Log out and back in for the group change to take effect.

### Claude API errors

```
Claude API call failed: ...
```

- Verify your API key: `echo $ANTHROPIC_API_KEY` (should start with `sk-ant-`)
- Check your Anthropic account has credits at [console.anthropic.com](https://console.anthropic.com)
- The agent will fall back to conservative offline rules if the API is unreachable -- your plant will not be neglected

### Photo capture fails

```
Photo capture failed, continuing without photo
```

- Test the camera directly: `rpicam-still -o /tmp/test.jpg`
- Check that the camera is enabled: `sudo raspi-config` (Interface Options > Camera)
- The agent continues without a photo -- this is not fatal

### Permission denied errors

- Make sure the systemd service `User=` matches your actual username
- Check file ownership: `ls -la ~/plant-ops-ai/.env`
- Check serial port access: `groups` should include `dialout`

### Bot responds but no scheduled checks

- Check that `python-telegram-bot[job-queue]` is installed (APScheduler is required for the JobQueue)
- Look at logs for warnings: `sudo journalctl -u plant-ops-ai | grep -i job`
- Verify the bot is not paused: check if `data/.paused` exists

### Soil moisture reads 100% (or stuck at a wrong value)

Work through these checks in order:

**1. Confirm the Pi is running the latest code** — this is the most common cause.

```bash
cd ~/plant-ops-ai
git log --oneline -3
```

The top commit should match the latest on GitHub. If it is behind, pull and restart:

```bash
git pull origin main
sudo systemctl restart plant-ops-ai
```

**2. Confirm farmctl.py returns a valid soil reading:**

```bash
python3 ~/plant-ops-ai/farmctl/farmctl.py status --json
```

Look for `"soil_raw": <number>` in the output. If `"raw"` is empty (`"raw": ""`), the Arduino is not responding — check the USB connection and serial port (`ls /dev/ttyACM*`).

**3. Confirm the calibration conversion works:**

```bash
cd ~/plant-ops-ai
python3 -c "from src.sensor_reader import _soil_adc_to_pct; print(_soil_adc_to_pct(262))"
```

Should print approximately `69.4`. If it prints `100.0` or errors, the code is not up to date.

**4. If all of the above look correct but /status still shows 100%:**

A genuine 100% means the sensor ADC is ≤ ~121, which causes the calibration formula to return > 100% — clamped to 100%. This indicates the soil is near or past saturation at the moment the bot reads it (the sensor ADC can be lower when the bot polls than when you check manually). Check the system log for the warning:

```bash
sudo journalctl -u plant-ops-ai | grep "Soil ADC"
```

The calibration was measured over ADC 390–822 (≈18–56% moisture). ADC values below 390 are extrapolated and can underestimate by up to ~10 percentage points. See `soil_moisture_calibration_curve.xlsx` and `ARCHITECTURE.md` for details on extending the calibration range.

### High API costs

- The default daily cost cap in `config/safety_limits.yaml` is $1.00/day
- Photos are taken every 4th check by default to reduce vision API costs
- Switch to a cheaper model by changing `CLAUDE_MODEL` in `.env`
- Reduce check frequency by editing the `interval=3600` value in `bot/telegram_bot.py`
