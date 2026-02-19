# Plant-Ops AI

An AI-powered plant automation agent that reads environmental sensors, asks Claude for care decisions, validates them through a 4-layer safety system, executes hardware actions, and reports everything via Telegram.

Runs on a Raspberry Pi connected to an Arduino sensor/relay board and a Pi camera module.

## Features

- **AI-driven plant care** -- Claude Sonnet analyzes sensor data + plant photos to decide when to water, adjust lighting, run the heater, or activate circulation
- **Multi-action decisions** -- Claude can recommend multiple actions per check (e.g., water AND turn on the heater) and knows the current state of all actuators
- **Natural language chat** -- talk to your plant agent in plain English via Telegram ("how's my plant?", "water it a bit", "what happened overnight?")
- **AI operational memory** -- the agent logs observations (watering outcomes, growth milestones, patterns) and reads them back on future checks, so it learns from its own experience
- **Knowledge research** -- when you set a plant species, Claude researches optimal growing conditions and caches the results; the agent can update this knowledge over time as it learns
- **4-layer safety system** -- hardcoded limits, action allowlist, rate limiting, and human emergency stop ensure the AI never has unchecked hardware control
- **Multi-user Telegram** -- invite friends by adding their chat IDs to `.env`
- **Photo with lighting** -- automatically turns on the grow light before taking photos for better image quality
- **Offline fallback** -- conservative rule-based actions keep your plant alive when the API is unreachable
- **Dry-run mode** -- test the full pipeline without executing any hardware commands
- **Structured logging** -- append-only JSONL logs for every sensor reading, decision, and AI observation

## Architecture

```
Telegram  <-->  [Raspberry Pi]  <-->  Internet (Anthropic API)
                     |
                 farmctl.py
                     |
                 [Arduino]
              sensors + relays
```

**Hourly loop**: read sensors --> capture photo (with light) --> ask Claude --> validate via safety layer --> execute actions --> log observations --> notify via Telegram.

**Chat mode**: user sends message --> read sensors --> ask Claude with conversation context --> validate & execute any actions --> reply naturally.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design, decision rationale, safety layers, and cost analysis.

## Project Structure

```
plant-ops-ai/
├── bot/
│   ├── telegram_bot.py         # Bot entry point, scheduled checks
│   ├── handlers.py             # Telegram command + chat handlers
│   └── keyboards.py            # Inline keyboard builders
├── src/
│   ├── plant_agent.py          # Main orchestrator (sense -> think -> act)
│   ├── claude_client.py        # Anthropic API wrapper
│   ├── prompts.py              # System/user prompt templates
│   ├── plant_knowledge.py      # One-time plant research + caching
│   ├── sensor_reader.py        # farmctl.py sensor reading
│   ├── action_executor.py      # farmctl.py action execution
│   ├── actuator_state.py       # Track actuator on/off state
│   ├── safety.py               # Safety validation layer
│   ├── config_loader.py        # YAML config loading
│   └── logger.py               # JSONL structured logging
├── config/
│   ├── safety_limits.yaml      # Hardcoded safety limits
│   └── plant_profile.yaml      # Current plant configuration
├── deploy/
│   ├── plant-ops-ai.service    # systemd service template
│   └── install.sh              # Service install script
├── tests/
├── data/                       # Runtime data (gitignored)
├── .env.example
├── requirements.txt
├── ARCHITECTURE.md
└── PI_SETUP.md
```

## Prerequisites

- Python 3.11+
- A Raspberry Pi with `farmctl.py` working (Arduino connected, sensors reading, relays toggling)
- Anthropic API key ([console.anthropic.com](https://console.anthropic.com))
- Telegram bot token (create one via [@BotFather](https://t.me/BotFather))
- Your Telegram chat ID (message [@userinfobot](https://t.me/userinfobot) to find it)

## Quick Start

```bash
# Clone and enter the project
git clone <repo-url> ~/plant-ops-ai
cd ~/plant-ops-ai

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your API keys and paths

# Test with mock data (no hardware needed)
python3 -m src.plant_agent --once --dry-run --mock

# Start the Telegram bot
python3 -m bot.telegram_bot
```

For full Raspberry Pi deployment instructions, see [PI_SETUP.md](PI_SETUP.md).

## Running as a Service

To keep the bot running after SSH disconnect and auto-start on boot:

```bash
# From the project directory on your Pi:
bash deploy/install.sh
sudo systemctl start plant-ops-ai
```

This installs a systemd service configured for your user. Useful commands:

```bash
sudo systemctl status plant-ops-ai      # Check status
sudo systemctl restart plant-ops-ai     # Restart (after git pull)
sudo systemctl stop plant-ops-ai        # Stop
sudo journalctl -u plant-ops-ai -f      # Live logs
```

## Updating

When new code is pushed:

```bash
cd ~/plant-ops-ai
git pull
sudo systemctl restart plant-ops-ai
```

## Telegram Interface

### Natural Language Chat

Just type any message to the bot in plain English:

- "How's my plant doing?"
- "Water it a bit"
- "Turn on the light"
- "What happened overnight?"
- "The leaves look droopy, what should I do?"

The AI reads current sensors, checks its memory of past observations, and responds conversationally. If you ask it to do something, it executes the action (with safety validation) and confirms.

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and quick-access menu |
| `/help` | Full command list |
| `/status` | Current sensor readings |
| `/photo` | Capture and send a plant photo |
| `/water [sec]` | Manual watering (default 5s, max 30s) |
| `/light on\|off` | Toggle grow light |
| `/heater on\|off` | Toggle heater |
| `/circulation [sec]` | Run circulation fan (default 60s, max 300s) |
| `/setplant <name>` | Set plant species and trigger AI research |
| `/profile` | View plant profile and ideal conditions |
| `/history [n]` | Show last N decisions (default 5) |
| `/pause` | Pause automated monitoring |
| `/resume` | Resume automated monitoring |
| `/mode dry-run\|live` | Switch between dry-run and live execution |

Manual action commands require confirmation via inline keyboard before executing.

### Multi-User Setup

To invite someone to use your bot:

1. They search for your bot on Telegram and send `/start`
2. They message [@userinfobot](https://t.me/userinfobot) to get their chat ID
3. They send you the number
4. You add their ID to `.env`: `TELEGRAM_CHAT_ID=your_id,their_id`
5. Restart the bot

## AI Memory System

The agent has two layers of memory:

**Plant knowledge** (`data/plant_knowledge.md`) -- a comprehensive growing guide researched by Claude when you first set a plant. Contains ideal conditions, care tips, common issues, and growth stage advice. The agent can append updates to this document as it learns.

**Plant log** (`data/plant_log.jsonl`) -- operational memory where the agent records observations after each check. Examples:
- "Watered 10 sec, soil went from 25% to 40% -- good amount for this pot size"
- "Soil dries from 60% to 30% in ~8 hours at 26C"
- "First flower spotted in photo"

These observations are fed back to Claude on every check, so the agent genuinely learns from past actions and adjusts its behavior over time.

## Safety System

The AI never has unchecked control over hardware. Safety is enforced in Python code, not in the AI prompt.

**Layer 1 -- Hardcoded limits** (`config/safety_limits.yaml`):
- Max watering: 30 seconds per cycle, minimum 60 minutes between waterings, 6 per day
- Heater off above 30C, on below 10C (failsafe), max 120 minutes continuous
- Light: max 18 hours per day within configured schedule
- Circulation fan: max 300 seconds, minimum 30 minutes between activations
- Global: max 10 actions per hour, daily API cost cap

**Layer 2 -- Action allowlist**: Claude can only choose from predefined actions (`water`, `light_on`, `light_off`, `heater_on`, `heater_off`, `circulation`, `do_nothing`, `notify_human`). Any other output is rejected.

**Layer 3 -- Rate limiting**: Per-action-type rate limits and a global actions-per-hour cap prevent runaway behavior.

**Layer 4 -- Human override**: Create the emergency stop file to immediately halt all automated actions:
```bash
touch /tmp/plant-agent-stop    # Stop all actions
rm /tmp/plant-agent-stop       # Resume
```

## Configuration

### Environment Variables (`.env`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes | -- | Claude API key |
| `TELEGRAM_BOT_TOKEN` | Yes | -- | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | -- | Comma-separated Telegram chat IDs |
| `FARMCTL_PATH` | No | `~/farmctl/farmctl.py` | Path to farmctl.py on the Pi |
| `SERIAL_PORT` | No | `/dev/ttyACM0` | Arduino serial port |
| `CLAUDE_MODEL` | No | `claude-sonnet-4-6` | Claude model to use |
| `DATA_DIR` | No | `./data` | Directory for logs and cached data |
| `AGENT_MODE` | No | `dry-run` | `dry-run` (log only) or `live` (execute actions) |

### Plant Profile (`config/plant_profile.yaml`)

Set your plant species and growth stage either by editing the YAML file directly or by using the `/setplant` Telegram command. When a new plant is set, Claude researches optimal growing conditions and saves them to `data/plant_knowledge.md`.

### Safety Limits (`config/safety_limits.yaml`)

Edit this file to adjust hardware safety limits for your setup. These limits are enforced regardless of what the AI recommends.

## Development

### Running Tests

```bash
python3 -m pytest tests/ -v
```

### Mock Mode

For local development without hardware:

```bash
# Single check with mock sensors, dry-run, no photo
python3 -m src.plant_agent --once --dry-run --mock --no-photo

# Start bot in dry-run mode
AGENT_MODE=dry-run python3 -m bot.telegram_bot
```

## Cost Estimate

Using Claude Sonnet 4.6 with hourly checks (24 calls/day):

| Component | Monthly Cost |
|-----------|-------------|
| Claude API (text only) | ~$1.50--2.50 |
| Claude API (with photo every 4th check) | ~$2.00--4.00 |
| Claude API (chat messages) | Varies with usage |
| Telegram Bot API | Free |
| **Total** | **~$2--5/month** |

The daily API cost cap in `safety_limits.yaml` (default $1.00/day) prevents unexpected charges.

## License

MIT
