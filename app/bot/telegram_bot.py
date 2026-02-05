import os
import random
import string
import asyncio
from datetime import datetime, timedelta
from typing import Optional
import httpx
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
import logging

from app.db.models import User, TelegramLinkCode, Task
from app.db.database import SessionLocal

# Configure logging
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds


async def send_message(
    chat_id: str, text: str, parse_mode: str = "HTML", retry_count: int = 0
) -> bool:
    """Send a message to a Telegram chat with retry logic"""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set - cannot send messages")
        return False

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{TELEGRAM_API_URL}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            )

            if response.status_code == 200:
                logger.info(f"Message sent successfully to chat_id={chat_id}")
                return True
            else:
                logger.warning(
                    f"Failed to send message to chat_id={chat_id}, status={response.status_code}, response={response.text}"
                )

                # Retry logic
                if retry_count < MAX_RETRIES:
                    logger.info(
                        f"Retrying message send (attempt {retry_count + 1}/{MAX_RETRIES}) in {RETRY_DELAY}s"
                    )
                    await asyncio.sleep(RETRY_DELAY)
                    return await send_message(
                        chat_id, text, parse_mode, retry_count + 1
                    )

                return False

    except httpx.TimeoutException as e:
        logger.error(f"Timeout sending message to chat_id={chat_id}: {e}")
        if retry_count < MAX_RETRIES:
            logger.info(
                f"Retrying after timeout (attempt {retry_count + 1}/{MAX_RETRIES}) in {RETRY_DELAY}s"
            )
            await asyncio.sleep(RETRY_DELAY)
            return await send_message(chat_id, text, parse_mode, retry_count + 1)
        return False

    except httpx.HTTPError as e:
        logger.error(f"HTTP error sending message to chat_id={chat_id}: {e}")
        if retry_count < MAX_RETRIES:
            logger.info(
                f"Retrying after HTTP error (attempt {retry_count + 1}/{MAX_RETRIES}) in {RETRY_DELAY}s"
            )
            await asyncio.sleep(RETRY_DELAY)
            return await send_message(chat_id, text, parse_mode, retry_count + 1)
        return False

    except Exception as e:
        logger.error(
            f"Unexpected error sending Telegram message to chat_id={chat_id}: {e}",
            exc_info=True,
        )
        return False


def generate_link_code(db: Session, user_id: int) -> str:
    """Generate a 6-digit code for linking Telegram account"""
    try:
        # Delete any existing codes for this user
        deleted_count = (
            db.query(TelegramLinkCode)
            .filter(TelegramLinkCode.user_id == user_id)
            .delete()
        )
        if deleted_count > 0:
            logger.info(
                f"Deleted {deleted_count} existing link codes for user_id={user_id}"
            )

        # Generate new code
        code = "".join(random.choices(string.digits, k=6))

        # Store code with 10 minute expiry
        link_code = TelegramLinkCode(
            user_id=user_id,
            code=code,
            expires_at=datetime.utcnow() + timedelta(minutes=10),
        )
        db.add(link_code)
        db.commit()

        logger.info(f"Generated link code for user_id={user_id}, code={code}")
        return code

    except SQLAlchemyError as e:
        logger.error(f"Database error in generate_link_code: {e}", exc_info=True)
        db.rollback()
        raise


def verify_link_code(
    db: Session, code: str, telegram_chat_id: str, telegram_username: str = None
) -> Optional[User]:
    """Verify a link code and connect Telegram account"""
    try:
        link_code = (
            db.query(TelegramLinkCode)
            .filter(
                TelegramLinkCode.code == code,
                TelegramLinkCode.expires_at > datetime.utcnow(),
            )
            .first()
        )

        logger.debug(f"Verifying link code={code}, found={link_code is not None}")

        if not link_code:
            logger.warning(f"Invalid or expired link code: {code}")
            return None

        # Get user and update Telegram info
        user = db.query(User).filter(User.id == link_code.user_id).first()
        if user:
            user.telegram_chat_id = telegram_chat_id
            user.telegram_username = telegram_username

            # Delete used code
            db.delete(link_code)
            db.commit()

            logger.info(
                f"Successfully linked Telegram account: user_id={user.id}, chat_id={telegram_chat_id}, username={telegram_username}"
            )
            return user
        else:
            logger.error(
                f"User not found for link code: code={code}, user_id={link_code.user_id}"
            )

        return None

    except SQLAlchemyError as e:
        logger.error(f"Database error in verify_link_code: {e}", exc_info=True)
        db.rollback()
        return None


def unlink_telegram(db: Session, user_id: int) -> bool:
    """Unlink Telegram account from user"""
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            logger.info(
                f"Unlinking Telegram for user_id={user_id}, chat_id={user.telegram_chat_id}"
            )
            user.telegram_chat_id = None
            user.telegram_username = None
            db.commit()
            return True
        else:
            logger.warning(f"User not found for unlink: user_id={user_id}")
        return False

    except SQLAlchemyError as e:
        logger.error(f"Database error in unlink_telegram: {e}", exc_info=True)
        db.rollback()
        return False


async def handle_telegram_webhook(update: dict) -> str:
    """Handle incoming Telegram webhook updates"""
    db = SessionLocal()
    try:
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "")
        username = message.get("from", {}).get("username", "")

        logger.info(
            f"Received webhook update: chat_id={chat_id}, text={text[:50] if text else 'None'}"
        )

        if not chat_id or not text:
            logger.warning("Invalid webhook update: missing chat_id or text")
            return "OK"

        # Check if user is already linked
        user = db.query(User).filter(User.telegram_chat_id == chat_id).first()

        # Handle commands
        if text.startswith("/start"):
            if user:
                await send_message(
                    chat_id,
                    f"👋 Welcome back, <b>{user.display_name or user.username}</b>!\n\nYour Telegram is already linked to your Second Thought account.\n\nI'll send you reminders for your scheduled tasks.\n\nType /help to see available commands.",
                )
            else:
                await send_message(
                    chat_id,
                    "👋 Welcome to <b>Second Thought</b> - your schedule assistant!\n\nTo link your account, get a code from the app settings and send it here.\n\nJust type /link followed by your 6-digit code:\n<code>/link 123456</code>",
                )

        elif text.startswith("/link"):
            parts = text.split()
            if len(parts) != 2:
                await send_message(
                    chat_id, "Please provide your link code:\n<code>/link 123456</code>"
                )
            else:
                code = parts[1]
                linked_user = verify_link_code(db, code, chat_id, username)
                if linked_user:
                    await send_message(
                        chat_id,
                        f"✅ Success! Your Telegram is now linked to <b>{linked_user.username}</b>.\n\nI'll send you reminders for your scheduled tasks based on your notification settings.",
                    )
                else:
                    await send_message(
                        chat_id,
                        "❌ Invalid or expired code. Please get a new code from the app settings.",
                    )

        elif text.startswith("/today"):
            if not user:
                await send_message(
                    chat_id, "Please link your account first using /link"
                )
            else:
                from app.services.schedule_service import get_today_schedule

                schedule = get_today_schedule(db, user.id)
                if schedule and schedule.tasks:
                    task_list = "\n".join(
                        [
                            f"• <b>{t.startTime}-{t.endTime}</b>: {t.task}"
                            for t in schedule.tasks
                        ]
                    )
                    await send_message(
                        chat_id, f"📅 <b>Today's Schedule</b>\n\n{task_list}"
                    )
                else:
                    await send_message(
                        chat_id,
                        "📅 No tasks scheduled for today.\n\nAdd tasks in the Second Thought app!",
                    )

        elif text.startswith("/settings"):
            if not user:
                await send_message(
                    chat_id, "Please link your account first using /link"
                )
            else:
                settings_msg = f"""⚙️ <b>Your Settings</b>

🔔 <b>Notifications:</b>
• Remind 10 min before: {'✅' if user.remind_before_activity else '❌'}
• Remind on start: {'✅' if user.remind_on_start else '❌'}
• Nudge during activity: {'✅' if user.nudge_during_activity else '❌'}
• Congratulate on finish: {'✅' if user.congratulate_on_finish else '❌'}

⏱ Default slot duration: {user.default_slot_duration} min
🌍 Timezone: {user.timezone}

<i>Change these settings in the app!</i>"""
                await send_message(chat_id, settings_msg)

        elif text.startswith("/help"):
            help_text = """🤖 <b>Second Thought Bot Commands</b>

/start - Start the bot
/link [code] - Link your account
/today - View today's schedule
/settings - View your settings
/unlink - Unlink your account
/help - Show this help

Your notification settings in the app control when I'll message you about your tasks."""
            await send_message(chat_id, help_text)

        elif text.startswith("/unlink"):
            if user:
                unlink_telegram(db, user.id)
                await send_message(
                    chat_id, "✅ Your Telegram has been unlinked from your account."
                )
            else:
                await send_message(
                    chat_id, "Your Telegram isn't linked to any account."
                )

        else:
            # Unknown message
            if user:
                await send_message(
                    chat_id,
                    "I didn't understand that. Type /help for available commands.",
                )
            else:
                await send_message(
                    chat_id,
                    "Please link your account first. Use /link [code] to connect your Second Thought account.",
                )

        return "OK"

    except Exception as e:
        logger.error(f"Error handling webhook update: {e}", exc_info=True)
        return "ERROR"

    finally:
        db.close()


async def send_task_reminder(user: User, task: Task, reminder_type: str) -> bool:
    """Send a task reminder notification"""
    if not user.telegram_chat_id:
        logger.warning(
            f"Cannot send reminder: user_id={user.id} has no telegram_chat_id"
        )
        return False

    try:
        if reminder_type == "remind_before":
            message = f"⏰ <b>Coming up in 10 minutes!</b>\n\n📋 {task.task_description}\n🕐 {task.start_time} - {task.end_time}"

        elif reminder_type == "remind_on_start":
            message = f"🚀 <b>Time to start!</b>\n\n📋 {task.task_description}\n🕐 Now until {task.end_time}"

        elif reminder_type == "nudge_during":
            message = f"💪 <b>Keep going!</b>\n\n📋 {task.task_description}\n\nYou're doing great, stay focused!"

        elif reminder_type == "congratulate":
            message = f"🎉 <b>Time's up!</b>\n\n📋 {task.task_description}\n\nGreat job completing this task!"

        else:
            logger.error(f"Unknown reminder_type: {reminder_type}")
            return False

        logger.info(
            f"Sending {reminder_type} notification: user_id={user.id}, task_id={task.id}, chat_id={user.telegram_chat_id}"
        )
        success = await send_message(user.telegram_chat_id, message)

        if success:
            logger.info(
                f"Successfully sent {reminder_type} notification: user_id={user.id}, task_id={task.id}"
            )
        else:
            logger.error(
                f"Failed to send {reminder_type} notification: user_id={user.id}, task_id={task.id}"
            )

        return success

    except Exception as e:
        logger.error(
            f"Error sending task reminder: user_id={user.id}, task_id={task.id}, type={reminder_type}, error={e}",
            exc_info=True,
        )
        return False


async def process_notifications():
    """Process and send all pending notifications

    This function is called every minute by the scheduler.
    It handles all errors gracefully to ensure one failure doesn't stop future notifications.
    """
    logger.info("=== Starting notification processing cycle ===")

    db = SessionLocal()
    notification_stats = {
        "remind_before": {"sent": 0, "failed": 0},
        "remind_on_start": {"sent": 0, "failed": 0},
        "nudge_during": {"sent": 0, "failed": 0},
        "congratulate": {"sent": 0, "failed": 0},
    }

    try:
        from app.services.schedule_service import (
            get_tasks_for_notification,
            update_notification_flags,
        )

        # Get all notifications that need to be sent
        notifications = get_tasks_for_notification(db)
        total_to_send = sum(len(v) for v in notifications.values())

        logger.info(f"Found {total_to_send} notifications to process")

        if total_to_send == 0:
            logger.info("No notifications to send this cycle")
            return

        # Process each notification type
        for reminder_type, tasks in notifications.items():
            logger.info(f"Processing {len(tasks)} '{reminder_type}' notifications")

            for user, task in tasks:
                try:
                    # Send the notification
                    success = await send_task_reminder(user, task, reminder_type)

                    if success:
                        # Update the flag in database
                        flag_map = {
                            "remind_before": "reminded_before",
                            "remind_on_start": "reminded_on_start",
                            "nudge_during": "nudged_during",
                            "congratulate": "congratulated",
                        }
                        update_notification_flags(db, task.id, flag_map[reminder_type])
                        notification_stats[reminder_type]["sent"] += 1
                        logger.info(
                            f"✓ Notification sent and flagged: {reminder_type}, user_id={user.id}, task_id={task.id}"
                        )
                    else:
                        notification_stats[reminder_type]["failed"] += 1
                        logger.error(
                            f"✗ Failed to send notification: {reminder_type}, user_id={user.id}, task_id={task.id}"
                        )

                except Exception as e:
                    # Log error but continue with other notifications
                    notification_stats[reminder_type]["failed"] += 1
                    logger.error(
                        f"Error processing notification: {reminder_type}, user_id={user.id}, task_id={task.id}, error={e}",
                        exc_info=True,
                    )
                    continue

        # Log summary
        total_sent = sum(stats["sent"] for stats in notification_stats.values())
        total_failed = sum(stats["failed"] for stats in notification_stats.values())

        logger.info(
            f"=== Notification cycle complete: {total_sent} sent, {total_failed} failed ==="
        )
        for ntype, stats in notification_stats.items():
            if stats["sent"] > 0 or stats["failed"] > 0:
                logger.info(
                    f"  {ntype}: {stats['sent']} sent, {stats['failed']} failed"
                )

    except Exception as e:
        logger.error(f"Critical error in process_notifications: {e}", exc_info=True)

    finally:
        try:
            db.close()
            logger.debug("Database session closed")
        except Exception as e:
            logger.error(f"Error closing database session: {e}", exc_info=True)
