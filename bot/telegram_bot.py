"""Telegram bot main entry point for plant-ops-ai.

Runs as a persistent process on the Raspberry Pi. Sets up the
python-telegram-bot Application, registers command handlers, and
schedules hourly plant monitoring checks via APScheduler (integrated
through python-telegram-bot's JobQueue).

Environment variables:
    TELEGRAM_BOT_TOKEN  - Bot token from @BotFather (required)
    TELEGRAM_CHAT_ID    - Comma-separated authorized chat IDs (required)
    FARMCTL_PATH        - Path to farmctl.py (default: ~/farmctl/farmctl.py)
    DATA_DIR            - Data directory (default: data/)
    AGENT_MODE          - "dry-run" (default) or "live"
    ANTHROPIC_API_KEY   - Claude API key for AI decisions
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.error import BadRequest, Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.handlers import (
    _split_text,
    chat_message_handler,
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
    chat_ids = bot_data.get("authorized_chat_ids", [])
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

        # Send summary to all authorized users
        if chat_ids:
            text = format_summary_text(summary)
            photo_path = summary.get("photo_path")
            has_photo = photo_path and Path(photo_path).exists()

            for chat_id in chat_ids:
                try:
                    for chunk in _split_text(text):
                        await context.bot.send_message(chat_id=chat_id, text=chunk)
                    if has_photo:
                        with open(photo_path, "rb") as f:
                            await context.bot.send_photo(
                                chat_id=chat_id,
                                photo=f,
                                caption="Scheduled plant photo",
                            )
                except BadRequest as exc:
                    if "chat not found" in str(exc).lower():
                        logger.error(
                            "Chat ID %s not found - check TELEGRAM_CHAT_ID "
                            "in .env. Send /start to the bot first.",
                            chat_id,
                        )
                    else:
                        logger.error(
                            "Telegram error sending to %s: %s", chat_id, exc
                        )
                except Exception:
                    logger.warning(
                        "Failed to send scheduled check to %s", chat_id
                    )

    except Exception as exc:
        logger.exception("Scheduled check failed")
        for chat_id in chat_ids:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Scheduled check ERROR:\n{exc}",
                )
            except BadRequest:
                logger.error(
                    "Cannot send error notification - chat %s not found",
                    chat_id,
                )
            except Exception:
                logger.exception("Failed to send error notification")


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------


async def _error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Global error handler for the bot application.

    Handles known errors cleanly instead of dumping full tracebacks:
    - Conflict (409): another bot instance is polling → shut down gracefully
    - BadRequest (400): e.g. invalid chat ID → log a clear message
    """
    err = context.error

    if isinstance(err, Conflict):
        logger.error(
            "Another bot instance is already running with this token. "
            "Kill the other process first, then restart."
        )
        # Stop this instance to avoid endless 409 retry loops
        asyncio.get_event_loop().call_soon(
            lambda: asyncio.ensure_future(context.application.updater.stop())
        )
        return

    if isinstance(err, BadRequest) and "chat not found" in str(err).lower():
        logger.error(
            "Chat not found - check TELEGRAM_CHAT_ID in .env. "
            "Make sure you have sent /start to the bot first."
        )
        return

    logger.error("Unhandled bot error: %s", err)


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


async def _post_init(application: Application) -> None:
    """Validate configuration after the bot connects to Telegram."""
    chat_ids = application.bot_data.get("authorized_chat_ids", [])
    if not chat_ids:
        return
    for chat_id in chat_ids:
        try:
            await application.bot.get_chat(chat_id)
            logger.info("Chat ID %s verified OK", chat_id)
        except BadRequest:
            logger.warning(
                "TELEGRAM_CHAT_ID=%s is not reachable. Scheduled messages "
                "will fail. Send /start to the bot from the correct account, "
                "or fix the chat ID in .env.",
                chat_id,
            )
        except Exception as exc:
            logger.warning("Could not verify chat ID %s: %s", chat_id, exc)


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

    chat_id_str = os.getenv("TELEGRAM_CHAT_ID", "")
    chat_ids = [cid.strip() for cid in chat_id_str.split(",") if cid.strip()]
    farmctl_path = os.getenv("FARMCTL_PATH", os.path.expanduser("~/farmctl/farmctl.py"))
    data_dir = os.getenv("DATA_DIR", "data")
    agent_mode = os.getenv("AGENT_MODE", "dry-run")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")

    if not chat_ids:
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
    application = (
        Application.builder()
        .token(bot_token)
        .post_init(_post_init)
        .build()
    )

    # Store config in bot_data for handlers to access
    application.bot_data["authorized_chat_ids"] = chat_ids
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

    # Natural language chat handler (catches all non-command text)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, chat_message_handler)
    )

    # Callback query handler for inline keyboard buttons
    application.add_handler(CallbackQueryHandler(confirm_callback))

    # Global error handler (catches Conflict, BadRequest, etc. cleanly)
    application.add_error_handler(_error_handler)

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
        "Plant-Ops AI bot starting (mode=%s, authorized_users=%s)",
        agent_mode,
        len(chat_ids) if chat_ids else "ANY",
    )
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
