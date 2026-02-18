# Plant-Ops AI

An AI-powered plant automation agent that reads environmental sensors, asks Claude for care decisions, validates them through a 4-layer safety system, executes hardware actions, and reports everything via Telegram.

Runs on a Raspberry Pi connected to an Arduino sensor/relay board and a Pi camera module.

## Features

- **AI-driven plant care** -- Claude Sonnet analyzes sensor data + plant photos to decide when to water, adjust lighting, run the heater, or activate circulation
- **4-layer safety system** -- hardcoded limits, action allowlist, rate limiting, and human emergency stop ensure the AI never has unchecked hardware control
- **Telegram interface** -- monitor sensors, view photos, trigger manual actions, configure plants, and receive automated check reports from anywhere
- **Plant knowledge research** -- when you set a new plant species, Claude researches optimal growing conditions and caches the results
- **Offline fallback** -- conservative rule-based actions keep your plant alive when the API is unreachable
- **Dry-run mode** -- test the full pipeline without executing any hardware commands
- **Structured logging** -- append-only JSONL logs for every sensor reading and decision

## Architecture

```
Telegram  <-->  [Raspberry Pi]  <-->  Internet (Anthropic API)
                     |
                 farmctl.py
                     |
                 [Arduino]
              sensors + relays
```

**Hourly loop**: read sensors --> capture photo --> ask Claude --> validate via safety layer --> execute action --> log --> notify via Telegram.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design, decision rationale, safety layers, and cost analysis.

## Project Structure

```
plant-ops-ai/
├── bot/
│   ├── telegram_bot.py         # Bot entry point, scheduled checks
│   ├── handlers.py             # Telegram command handlers
│   └── keyboards.py            # Inline keyboard builders
├── src/
│   ├── plant_agent.py          # Main orchestrator (sense -> think -> act)
│   ├── claude_client.py        # Anthropic API wrapper
│   ├── prompts.py              # System/user prompt templates
│   ├── plant_knowledge.py      # One-time plant research + caching
│   ├── sensor_reader.py        # farmctl.py sensor reading
│   ├── action_executor.py      # farmctl.py action execution
│   ├── safety.py               # Safety validation layer
│   ├── config_loader.py        # YAML config loading
│   └── logger.py               # JSONL structured logging
├── config/
│   ├── safety_limits.yaml      # Hardcoded safety limits
│   └── plant_profile.yaml      # Current plant configuration
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

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and quick-access menu |
| `/help` | Full command list |
| `/status` | Current sensor readings (temp, humidity, CO2, light, soil moisture) |
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

Manual action commands (`/water`, `/light`, `/heater`, `/circulation`) require confirmation via inline keyboard before executing.

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
touch /tmp/plant-agent-stop
```
Remove it to resume:
```bash
rm /tmp/plant-agent-stop
```

## Configuration

### Environment Variables (`.env`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes | -- | Claude API key |
| `TELEGRAM_BOT_TOKEN` | Yes | -- | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | -- | Your Telegram chat ID (restricts access) |
| `FARMCTL_PATH` | No | `~/farmctl/farmctl.py` | Path to farmctl.py on the Pi |
| `SERIAL_PORT` | No | `/dev/ttyACM0` | Arduino serial port |
| `CLAUDE_MODEL` | No | `claude-sonnet-4-20250514` | Claude model to use |
| `DATA_DIR` | No | `./data` | Directory for logs and cached data |
| `AGENT_MODE` | No | `dry-run` | `dry-run` (log only) or `live` (execute actions) |

### Plant Profile (`config/plant_profile.yaml`)

Set your plant species and growth stage either by editing the YAML file directly or by using the `/setplant` Telegram command. When a new plant is set, Claude researches optimal growing conditions and saves them to `data/plant_knowledge.md`.

### Safety Limits (`config/safety_limits.yaml`)

Edit this file to adjust hardware safety limits for your setup. These limits are enforced regardless of what the AI recommends.

## Development

### Running Tests

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run a specific test file
python3 -m pytest tests/test_safety.py -v
```

### Mock Mode

For local development without hardware:

```bash
# Single check with mock sensors, dry-run, no photo
python3 -m src.plant_agent --once --dry-run --mock --no-photo

# Start bot (will use mock sensors if farmctl.py is not found)
AGENT_MODE=dry-run python3 -m bot.telegram_bot
```

### Verbose Output

```bash
python3 -m src.plant_agent --once --dry-run --mock --verbose
```

## Cost Estimate

Using Claude Sonnet with hourly checks (24 calls/day):

| Component | Monthly Cost |
|-----------|-------------|
| Claude API (text only) | ~$1.50--2.50 |
| Claude API (with photo every 4th check) | ~$2.00--4.00 |
| Telegram Bot API | Free |
| **Total** | **~$2--4/month** |

The daily API cost cap in `safety_limits.yaml` (default $1.00/day) prevents unexpected charges.

## License

MIT
