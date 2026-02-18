"""Telegram bot main entry point for plant-ops-ai.

Runs as a persistent process on the Raspberry Pi. Sets up the
python-telegram-bot Application, registers command handlers, and
schedules hourly plant monitoring checks via APScheduler (integrated
through python-telegram-bot's JobQueue).

Environment variables:
    TELEGRAM_BOT_TOKEN  - Bot token from @BotFather (required)
    TELEGRAM_CHAT_ID    - Authorized user's chat ID (required)
    FARMCTL_PATH        - Path to farmctl.py (default: ~/farmctl/farmctl.py)
    DATA_DIR            - Data directory (default: data/)
    AGENT_MODE          - "dry-run" (default) or "live"
    ANTHROPIC_API_KEY   - Claude API key for AI decisions
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from bot.handlers import (
    circulation_command,
    confirm_callback,
    heater_command,
    help_command,
    history_command,
    light_command,
    mode_command,
    pause_command,
    photo_command,
    profile_command,
    resume_command,
    setplant_command,
    start_command,
    status_command,
    water_command,
)
from src.plant_agent import run_check, format_summary_text

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scheduled monitoring
# ---------------------------------------------------------------------------

# Counter for photo frequency (take a photo every Nth check to save costs)
_check_counter: int = 0
PHOTO_EVERY_N_CHECKS: int = 4


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the automated plant monitoring check.

    Called every hour by the JobQueue. Delegates to src.plant_agent.run_check()
    which handles the full sense -> think -> act pipeline, then sends the
    summary to the authorized Telegram chat.

    Skips silently if the pause file exists.
    """
    global _check_counter

    bot_data = context.bot_data
    data_dir = bot_data.get("data_dir", "data")
    pause_file = Path(data_dir) / ".paused"
    chat_id = bot_data.get("authorized_chat_id")
    farmctl_path = bot_data.get("farmctl_path", "")
    agent_mode = bot_data.get("agent_mode", "dry-run")

    # Skip if paused
    if pause_file.exists():
        logger.info("Scheduled check skipped (paused)")
        return

    _check_counter += 1
    take_photo = (_check_counter % PHOTO_EVERY_N_CHECKS == 1)

    try:
        summary = run_check(
            farmctl_path=farmctl_path,
            data_dir=data_dir,
            dry_run=(agent_mode != "live"),
            use_mock=False,
            include_photo=take_photo,
        )

        # Send summary to Telegram
        if chat_id:
            text = format_summary_text(summary)
            await context.bot.send_message(chat_id=chat_id, text=text)

            # Send photo if one was captured
            photo_path = summary.get("photo_path")
            if photo_path and Path(photo_path).exists():
                try:
                    with open(photo_path, "rb") as f:
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=f,
                            caption="Scheduled plant photo",
                        )
                except Exception:
                    logger.warning("Failed to send scheduled photo")

    except Exception as exc:
        logger.exception("Scheduled check failed")
        if chat_id:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Scheduled check ERROR:\n{exc}",
                )
            except Exception:
                logger.exception("Failed to send error notification")


# ---------------------------------------------------------------------------
# Bot setup and main
# ---------------------------------------------------------------------------

def main() -> None:
    """Start the Telegram bot.

    Loads configuration from environment variables, builds the
    python-telegram-bot Application, registers all command handlers,
    sets up the hourly scheduled check, and starts long-polling.
    """
    # Load .env file if present (for local development)
    load_dotenv()

    # --- Required config ---------------------------------------------------
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is required")
        sys.exit(1)

    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    farmctl_path = os.getenv("FARMCTL_PATH", os.path.expanduser("~/farmctl/farmctl.py"))
    data_dir = os.getenv("DATA_DIR", "data")
    agent_mode = os.getenv("AGENT_MODE", "dry-run")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")

    if not chat_id:
        logger.warning(
            "TELEGRAM_CHAT_ID not set - bot will accept commands from anyone!"
        )

    if not anthropic_key:
        logger.warning(
            "ANTHROPIC_API_KEY not set - scheduled AI decisions will be disabled"
        )

    # Ensure data directory exists
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    # --- Build application -------------------------------------------------
    application = Application.builder().token(bot_token).build()

    # Store config in bot_data for handlers to access
    application.bot_data["authorized_chat_id"] = chat_id
    application.bot_data["farmctl_path"] = farmctl_path
    application.bot_data["data_dir"] = data_dir
    application.bot_data["agent_mode"] = agent_mode
    application.bot_data["anthropic_api_key"] = anthropic_key

    # --- Register command handlers -----------------------------------------
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("photo", photo_command))
    application.add_handler(CommandHandler("water", water_command))
    application.add_handler(CommandHandler("light", light_command))
    application.add_handler(CommandHandler("heater", heater_command))
    application.add_handler(CommandHandler("circulation", circulation_command))
    application.add_handler(CommandHandler("setplant", setplant_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("mode", mode_command))

    # Callback query handler for inline keyboard buttons
    application.add_handler(CallbackQueryHandler(confirm_callback))

    # --- Schedule hourly monitoring check ----------------------------------
    job_queue = application.job_queue
    if job_queue is not None:
        job_queue.run_repeating(
            scheduled_check,
            interval=3600,    # every hour
            first=10,         # first run 10 seconds after startup
        )
        logger.info("Scheduled monitoring check registered (every 3600s)")
    else:
        logger.warning(
            "JobQueue not available. Install python-telegram-bot[job-queue] "
            "for scheduled checks."
        )

    # --- Start polling -----------------------------------------------------
    logger.info(
        "Plant-Ops AI bot starting (mode=%s, chat_id=%s)",
        agent_mode,
        chat_id or "ANY",
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
