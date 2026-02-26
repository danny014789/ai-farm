# Plant-Ops AI Agent Architecture

## System Overview

```
                        Internet (Anthropic API)
                              |
                              v
+---------------------------[Raspberry Pi]---------------------------+
|                                                                     |
|  cron (every 15-30 min)                                            |
|       |                                                             |
|       v                                                             |
|  plant_agent.py                                                    |
|       |                                                             |
|       +---> reads sensors via farmctl.py (serial -> Arduino)       |
|       +---> takes photo via farmctl.py (rpicam-still)              |
|       +---> sends data + photo to Claude API                       |
|       +---> Claude returns decision (JSON)                         |
|       +---> executes actions via farmctl.py (relay commands)       |
|       +---> logs decision + actions to local DB/file               |
|       +---> sends notification if needed (email/webhook)           |
|                                                                     |
+---------------------------[Arduino]--------------------------------+
|  sensors: temp, humidity, CO2, light, soil moisture                |
|  relays:  light, heater, pump, circulation                        |
+--------------------------------------------------------------------+
```

## Key Decision: Anthropic Python SDK vs Agent SDK

### Recommendation: Start with the Anthropic Python SDK (not Agent SDK)

**Why:**

| Factor | Python SDK | Agent SDK |
|--------|-----------|-----------|
| Cron-friendly | Yes - starts, runs, exits | Designed for long-running sessions |
| Cost control | You control exactly how many API calls | Can loop unpredictably |
| Debuggability | Simple request/response | Abstracted orchestration |
| Pi resources | Minimal | Slightly heavier |
| Safety | You control the tool execution loop | SDK executes tools automatically |
| Complexity | ~100 lines of code | More setup overhead |

For a **scheduled cron job** that reads sensors and makes decisions, the raw SDK
is simpler, cheaper, and safer. The Agent SDK is better for interactive sessions
where a human is watching.

**Upgrade path**: Start with Python SDK. If you later want a more interactive
agent (e.g., a chat interface to your plant), upgrade to Agent SDK then.

## Architecture: The Decision Loop

```python
# Pseudocode for plant_agent.py

# 0. OUTDOOR WEATHER (optional)
weather = fetch_outdoor_weather()  # Open-Meteo API, no key required; skipped if WEATHER_LAT/LON unset

# 1. GATHER DATA
sensor_data = run("farmctl.py status --json")
photo_path  = run("farmctl.py camera-snap --out /tmp/plant.jpg --json")

# 2. LOAD CONTEXT
history     = load_recent_decisions(last_n=10)
plant_profile = load_plant_profile()  # species, growth stage, preferences

# 3. ASK CLAUDE
response = claude.messages.create(
    model="claude-sonnet-4-6",  # good balance of cost/quality
    system=SYSTEM_PROMPT,  # plant care expert, safety rules
    messages=[
        {"role": "user", "content": [
            {"type": "text", "text": format_sensor_data(sensor_data)},
            {"type": "text", "text": format_history(history)},
            {"type": "image", "source": load_image(photo_path)},
            {"type": "text", "text": "Analyze plant status. Return JSON decision."}
        ]}
    ],
    max_tokens=1024
)

# 4. PARSE DECISION (structured JSON output)
decision = parse_decision(response)
# Example: {"action": "water", "duration_sec": 5, "reason": "soil moisture low",
#           "urgency": "normal", "notify_human": false}

# 5. SAFETY CHECK (hardcoded limits, NOT AI-controlled)
validated = safety_check(decision)
# - max water: 30 sec
# - max heater: never above 30C
# - no conflicting actions
# - rate limit: no repeated watering within 1 hour

# 6. EXECUTE
if validated:
    execute_action(decision)  # calls farmctl.py commands

# 7. LOG
log_decision(sensor_data, decision, validated, executed=True)

# 8. NOTIFY (if needed)
if decision.get("notify_human"):
    send_notification(decision["reason"])
```

## Safety Architecture (Critical)

The AI should NEVER have unchecked control over hardware. Safety is enforced
in **Python code, not in the AI prompt**.

### Safety Layers

```
Layer 1: HARDCODED LIMITS (in Python, not AI-controllable)
  - Max water duration: 30 seconds per cycle
  - Max heater: off if temp > 30C
  - Max light: 18 hours per day
  - Min interval between waterings: 60 min
  - Circulation fan: max 3600 seconds (60 min) per cycle, no rate limit between activations

Layer 2: ALLOWLIST (AI can only choose from predefined actions)
  - water(sec)     -> capped at 30
  - light(on|off)  -> checked against daily schedule
  - heater(on|off) -> checked against temp limits
  - circulation(sec) -> capped at 3600
  - "do_nothing"   -> always allowed
  - "notify_human" -> always allowed

Layer 3: RATE LIMITING
  - Max API calls per hour: configurable
  - Max actions per hour: 10 (global cap)
  - Daily cost cap: $1.00 (track token usage)

Layer 4: HUMAN OVERRIDE
  - Emergency stop: touch /tmp/plant-agent-stop -> agent exits immediately
  - Manual mode file: agent skips execution but still logs recommendations
  - Email/notification for unusual decisions
```

### What the AI prompt CANNOT override:
- Hardware safety limits (hardcoded in Python)
- Action allowlist (only predefined commands)
- Rate limits (enforced before execution)
- Cost caps (tracked per-run)

## Notification / Human Feedback Loop

### Options (simplest to most complex):

**1. Email (Recommended to start)**
```
- Use Python smtplib or a service like SendGrid/Mailgun
- Send daily digest: sensor trends + actions taken + photo
- Send alerts: unusual readings, AI uncertainty, action failures
- Free tier usually sufficient (100 emails/day)
```

**2. Webhook to messaging (Telegram/Discord/Slack)**
```
- Telegram Bot API is simplest (free, no server needed)
- Send text + photo in one message
- Can add reply buttons for human approval
```

**3. Simple web dashboard (later)**
```
- Flask/FastAPI on Pi, exposed via Tailscale
- Show sensor history, photos, decision log
- Manual override buttons
```

### Daily Digest Email Example:
```
Subject: Plant Report - 2026-02-18

Sensor Summary (last 24h):
  Temp:     22-26C (avg 24C)
  Humidity: 55-72% (avg 63%)
  Soil:     42% (trending down)
  Light:    14h total

Actions Taken:
  08:15 - Watered 8 sec (soil was 38%)
  18:00 - Light off (schedule)
  22:30 - Heater on (temp dropped to 20C)

AI Notes:
  "Soil moisture dropping faster than usual. May need
   to increase watering frequency or check for drainage
   issues. Plant looks healthy in photo analysis."

[photo attached]
```

## Cost Estimation

Using Claude Sonnet (recommended for cost/quality balance):

| Frequency | Input tokens | Output tokens | Monthly cost |
|-----------|-------------|---------------|--------------|
| Every 30 min | ~2K/call | ~500/call | ~$3-5/mo |
| Every 15 min | ~2K/call | ~500/call | ~$6-10/mo |
| Every 60 min | ~2K/call | ~500/call | ~$1.5-3/mo |

With vision (sending plant photo):
- Add ~1K tokens per image
- Roughly doubles the cost
- Worth it for detecting visual issues (wilting, pests, discoloration)

**Recommendation**: Every 30 min with photo = ~$5-8/mo. Very affordable.

## Development Workflow

```
[Mac - Local Development]          [Raspberry Pi - Runtime]
        |                                   |
  Claude Code edits code              runs plant_agent.py
        |                                   |
  git commit + push                    git pull
        |                                   |
   GitHub repo  <--------------------> GitHub repo
        |                                   |
  test with mock data               test with real hardware
```

### Step-by-step:

1. **Develop locally** on Mac using Claude Code
   - Write plant_agent.py, config files, prompts
   - Test with mock sensor data (no Arduino needed)
   - Use `--dry-run` flag to skip actual hardware commands

2. **Push to GitHub** from Mac

3. **Pull on Pi** via SSH
   ```bash
   ssh pi@<tailscale-ip> "cd ~/plant-ops-ai && git pull && pip install -r requirements.txt"
   ```

4. **Test on Pi** with real hardware
   ```bash
   ssh pi@<tailscale-ip> "cd ~/plant-ops-ai && python3 plant_agent.py --once --verbose"
   ```

5. **Set up cron** when stable
   ```bash
   # On Pi: crontab -e
   */30 * * * * cd ~/plant-ops-ai && python3 plant_agent.py --once >> /var/log/plant-agent.log 2>&1
   ```

## File Structure

```
plant-ops-ai/
  ARCHITECTURE.md          # this file
  README.md                # setup instructions
  requirements.txt         # anthropic, python-telegram-bot, python-dotenv, pyyaml, apscheduler
  .env.example             # ANTHROPIC_API_KEY=sk-...
  config/
    plant_profile.yaml     # species, growth stage, care preferences
    safety_limits.yaml     # hardcoded max values for actions
    hardware_profile.yaml  # physical setup (pump flow rate, pot size, sensor calibration)
  src/
    plant_agent.py         # main entry point / orchestrator
    claude_client.py       # Anthropic API wrapper
    sensor_reader.py       # calls farmctl.py status, parses output; converts soil ADC → % via exponential calibration
    action_executor.py     # calls farmctl.py commands with safety checks
    actuator_state.py      # tracks actuator on/off state
    safety.py              # hardcoded safety limits + validation
    weather.py             # outdoor weather via Open-Meteo (no API key needed)
    logger.py              # decision + action logging (JSONL)
    prompts.py             # system prompt + user prompt templates
    plant_knowledge.py     # one-time plant research + caching
    config_loader.py       # YAML config loading
  bot/
    telegram_bot.py        # bot entry point, scheduled checks
    handlers.py            # Telegram command + chat handlers
    keyboards.py           # inline keyboard builders
  data/
    decisions.jsonl        # append-only decision log
    sensor_history.jsonl   # sensor readings over time
    plant_knowledge.md     # cached plant care research
  tests/
```

## Implementation Order

### Phase 1: Core Loop (get it working)
1. sensor_reader.py - parse farmctl.py output
2. prompts.py - craft the system prompt
3. claude_client.py - call Claude API with sensor data
4. safety.py - hardcoded limits
5. plant_agent.py - wire it all together
6. Test locally with mock data

### Phase 2: Actions + Logging
7. action_executor.py - execute validated decisions
8. logger.py - log everything
9. Test on Pi with real hardware (--dry-run first)
10. Set up cron

### Phase 3: Notifications + Polish
11. notifier.py - email daily digest
12. Plant photo analysis (vision API)
13. Historical trend analysis
14. Web dashboard (optional, later)

## System Prompt Strategy

The system prompt should be:
- **Specific** about the plant species and growth stage
- **Structured** about expected output format (JSON)
- **Clear** about what actions are available
- **Honest** about what the AI can and cannot see
- **Conservative** - when in doubt, do nothing and notify human

See `src/prompts.py` for the actual prompt (to be implemented).

## Soil Moisture Calibration

The soil sensor outputs a raw ADC value (0–1023). `sensor_reader.py` converts it using an exponential fit over 9 measured data points:

```
moisture_pct = exp(-0.00258653 × ADC + 4.91733458)   clamped to [0, 100]
```

**Calibrated range**: ADC 390–822 (≈18–56% moisture). Outside this range the equation extrapolates; readings below ADC 390 (wet soil) can underestimate actual moisture by up to ~10 percentage points. A result of 100% indicates the sensor ADC is below ~121 — sensor is saturated or out of measurable range.

The system logs a `WARNING` whenever a reading falls outside the calibrated range, and the AI system prompt explicitly tells Claude to treat 100% as "saturated / clamped" rather than a precise figure.

**Source-field tracking**: `_parse_sensor_json` tracks whether the soil value came from `soil_raw` (raw ADC, always needs conversion) or `soil_moisture_pct` (already a percentage, pass through). This prevents a bug where an ADC value ≤ 100 (very wet soil) would bypass conversion and be returned directly as a percentage.

**Improving accuracy at the wet end**: add calibration measurements at ADC < 390 (progressively wetter soil samples with known gravimetric moisture) to `soil_moisture_calibration_curve.xlsx` and refit the coefficients in `sensor_reader.py`.

## Offline / Fallback Behavior

If the API is unreachable (network down, API outage):
1. Log sensor data locally (always)
2. Apply simple rule-based fallbacks:
   - If soil moisture < 25%: water for 5 sec
   - If temp < 18C: heater on
   - If temp > 30C: heater off, circulation on
3. Notify human when connectivity returns
4. Never skip logging

This ensures the plant doesn't die if the internet goes down.
