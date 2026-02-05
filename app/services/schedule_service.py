from datetime import datetime, date, timedelta
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
import pytz
import logging

from app.db.models import User, Schedule, Task
from app.api.schemas import DailySchedule, TaskBlockJson

# Configure logging
logger = logging.getLogger(__name__)


def get_or_create_schedule(db: Session, user_id: int, schedule_date: str) -> Schedule:
    """Get existing schedule or create new one for a date"""
    try:
        schedule = (
            db.query(Schedule)
            .filter(Schedule.user_id == user_id, Schedule.date == schedule_date)
            .first()
        )

        if not schedule:
            logger.info(
                f"Creating new schedule for user_id={user_id}, date={schedule_date}"
            )
            schedule = Schedule(user_id=user_id, date=schedule_date)
            db.add(schedule)
            db.commit()
            db.refresh(schedule)
            logger.info(f"Created schedule id={schedule.id}")

        return schedule
    except SQLAlchemyError as e:
        logger.error(f"Database error in get_or_create_schedule: {e}", exc_info=True)
        db.rollback()
        raise


def save_schedule(db: Session, user_id: int, schedule_data: DailySchedule) -> Schedule:
    """Save or update a daily schedule with tasks"""
    try:
        logger.info(
            f"Saving schedule for user_id={user_id}, date={schedule_data.date}, tasks_count={len(schedule_data.tasks)}"
        )

        # Get or create schedule
        schedule = get_or_create_schedule(db, user_id, schedule_data.date)

        # Delete existing tasks for this schedule
        deleted_count = db.query(Task).filter(Task.schedule_id == schedule.id).delete()
        logger.info(
            f"Deleted {deleted_count} existing tasks for schedule_id={schedule.id}"
        )

        # Add new tasks
        for idx, task_json in enumerate(schedule_data.tasks):
            task = Task(
                task_uuid=task_json.id,
                user_id=user_id,
                schedule_id=schedule.id,
                start_time=task_json.startTime,
                end_time=task_json.endTime,
                task_description=task_json.task,
            )
            db.add(task)
            logger.debug(
                f"Added task {idx+1}/{len(schedule_data.tasks)}: {task_json.startTime}-{task_json.endTime} - {task_json.task[:50]}"
            )

        # Update schedule timestamp
        schedule.updated_at = datetime.utcnow()

        db.commit()
        db.refresh(schedule)

        logger.info(
            f"Successfully saved schedule_id={schedule.id} with {len(schedule_data.tasks)} tasks"
        )
        return schedule

    except SQLAlchemyError as e:
        logger.error(f"Database error in save_schedule: {e}", exc_info=True)
        db.rollback()
        raise


def get_schedule_for_date(
    db: Session, user_id: int, schedule_date: str
) -> Optional[DailySchedule]:
    """Get schedule for a specific date"""
    try:
        schedule = (
            db.query(Schedule)
            .filter(Schedule.user_id == user_id, Schedule.date == schedule_date)
            .first()
        )

        if not schedule:
            logger.debug(
                f"No schedule found for user_id={user_id}, date={schedule_date}"
            )
            return None

        tasks = (
            db.query(Task)
            .filter(Task.schedule_id == schedule.id)
            .order_by(Task.start_time)
            .all()
        )
        logger.debug(f"Retrieved {len(tasks)} tasks for schedule_id={schedule.id}")

        task_list = [
            TaskBlockJson(
                id=task.task_uuid,
                startTime=task.start_time,
                endTime=task.end_time,
                task=task.task_description,
            )
            for task in tasks
        ]

        return DailySchedule(
            date=schedule.date,
            createdAt=schedule.created_at.isoformat() if schedule.created_at else "",
            updatedAt=schedule.updated_at.isoformat() if schedule.updated_at else "",
            tasks=task_list,
        )
    except SQLAlchemyError as e:
        logger.error(f"Database error in get_schedule_for_date: {e}", exc_info=True)
        raise


def get_today_schedule(db: Session, user_id: int) -> Optional[DailySchedule]:
    """Get today's schedule"""
    today = date.today().isoformat()
    logger.debug(f"Getting today's schedule for user_id={user_id}, date={today}")
    return get_schedule_for_date(db, user_id, today)


def get_upcoming_tasks(
    db: Session, user_id: int, minutes_ahead: int = 15
) -> List[Task]:
    """Get tasks starting within the next N minutes"""
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            logger.warning(f"User not found: user_id={user_id}")
            return []

        try:
            tz = pytz.timezone(user.timezone or "Asia/Kolkata")
        except Exception as e:
            logger.warning(
                f"Invalid timezone '{user.timezone}' for user_id={user_id}, using default: {e}"
            )
            tz = pytz.timezone("Asia/Kolkata")

        now = datetime.now(tz)
        today = now.date().isoformat()
        current_time = now.strftime("%H:%M")
        future_time = (now + timedelta(minutes=minutes_ahead)).strftime("%H:%M")

        tasks = (
            db.query(Task)
            .join(Schedule)
            .filter(
                Task.user_id == user_id,
                Schedule.date == today,
                Task.start_time >= current_time,
                Task.start_time <= future_time,
                Task.is_completed == False,
            )
            .all()
        )

        logger.debug(
            f"Found {len(tasks)} upcoming tasks for user_id={user_id} between {current_time} and {future_time}"
        )
        return tasks

    except SQLAlchemyError as e:
        logger.error(f"Database error in get_upcoming_tasks: {e}", exc_info=True)
        return []


def get_tasks_for_notification(db: Session) -> dict:
    """Get all tasks that need notifications across all users

    Uses a 2-minute window to make notifications more robust and less likely to be missed.
    """
    result = {
        "remind_before": [],  # 10 min before start
        "remind_on_start": [],  # At start time
        "nudge_during": [],  # Middle of task
        "congratulate": [],  # At end time
    }

    try:
        users = db.query(User).filter(User.telegram_chat_id.isnot(None)).all()
        logger.info(
            f"Checking notifications for {len(users)} users with Telegram linked"
        )

        for user in users:
            try:
                # Get user timezone
                try:
                    tz = pytz.timezone(user.timezone or "Asia/Kolkata")
                except Exception as e:
                    logger.warning(
                        f"Invalid timezone '{user.timezone}' for user_id={user.id}, using default: {e}"
                    )
                    tz = pytz.timezone("Asia/Kolkata")

                now = datetime.now(tz)
                today = now.date().isoformat()

                # Get today's uncompleted tasks
                tasks = (
                    db.query(Task)
                    .join(Schedule)
                    .filter(
                        Task.user_id == user.id,
                        Schedule.date == today,
                        Task.is_completed == False,
                    )
                    .all()
                )

                logger.debug(
                    f"User {user.id} ({user.username}): {len(tasks)} active tasks today"
                )

                for task in tasks:
                    try:
                        # Parse task times
                        start_h, start_m = map(int, task.start_time.split(":"))
                        end_h, end_m = map(int, task.end_time.split(":"))

                        start_minutes = start_h * 60 + start_m
                        end_minutes = end_h * 60 + end_m
                        current_minutes = now.hour * 60 + now.minute

                        # IMPROVED: 2-minute window instead of 1 minute for more robust notifications

                        # 10 minutes before (8-12 minutes before to be safe)
                        if user.remind_before_activity and not task.reminded_before:
                            time_until_start = start_minutes - current_minutes
                            if 8 <= time_until_start <= 12:
                                result["remind_before"].append((user, task))
                                logger.info(
                                    f"NOTIFICATION: Remind before - user_id={user.id}, task_id={task.id}, task={task.task_description[:50]}, starts in {time_until_start} min"
                                )

                        # At start (within 2 minutes of start time)
                        if user.remind_on_start and not task.reminded_on_start:
                            time_diff = abs(start_minutes - current_minutes)
                            if time_diff <= 2:
                                result["remind_on_start"].append((user, task))
                                logger.info(
                                    f"NOTIFICATION: Remind on start - user_id={user.id}, task_id={task.id}, task={task.task_description[:50]}"
                                )

                        # Nudge during (middle of task, with 2-minute window)
                        if user.nudge_during_activity and not task.nudged_during:
                            middle_minutes = (start_minutes + end_minutes) // 2
                            time_diff = abs(middle_minutes - current_minutes)
                            if time_diff <= 2:
                                result["nudge_during"].append((user, task))
                                logger.info(
                                    f"NOTIFICATION: Nudge during - user_id={user.id}, task_id={task.id}, task={task.task_description[:50]}"
                                )

                        # Congratulate at end (within 2 minutes of end time)
                        if user.congratulate_on_finish and not task.congratulated:
                            time_diff = abs(end_minutes - current_minutes)
                            if time_diff <= 2:
                                result["congratulate"].append((user, task))
                                logger.info(
                                    f"NOTIFICATION: Congratulate - user_id={user.id}, task_id={task.id}, task={task.task_description[:50]}"
                                )

                    except Exception as e:
                        logger.error(
                            f"Error processing task_id={task.id} for user_id={user.id}: {e}",
                            exc_info=True,
                        )
                        continue

            except Exception as e:
                logger.error(
                    f"Error processing notifications for user_id={user.id}: {e}",
                    exc_info=True,
                )
                continue

        total_notifications = sum(len(v) for v in result.values())
        logger.info(
            f"Found {total_notifications} total notifications to send: "
            f"remind_before={len(result['remind_before'])}, "
            f"remind_on_start={len(result['remind_on_start'])}, "
            f"nudge_during={len(result['nudge_during'])}, "
            f"congratulate={len(result['congratulate'])}"
        )

        return result

    except SQLAlchemyError as e:
        logger.error(
            f"Database error in get_tasks_for_notification: {e}", exc_info=True
        )
        return result


def mark_task_completed(db: Session, user_id: int, task_uuid: str) -> bool:
    """Mark a task as completed"""
    try:
        task = (
            db.query(Task)
            .filter(Task.user_id == user_id, Task.task_uuid == task_uuid)
            .first()
        )

        if task:
            logger.info(
                f"Marking task as completed: task_id={task.id}, task_uuid={task_uuid}, user_id={user_id}"
            )
            task.is_completed = True
            task.completed_at = datetime.utcnow()
            db.commit()
            return True
        else:
            logger.warning(
                f"Task not found for completion: task_uuid={task_uuid}, user_id={user_id}"
            )
        return False

    except SQLAlchemyError as e:
        logger.error(f"Database error in mark_task_completed: {e}", exc_info=True)
        db.rollback()
        return False


def update_notification_flags(db: Session, task_id: int, flag_name: str) -> None:
    """Update notification tracking flags"""
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task:
            setattr(task, flag_name, True)
            db.commit()
            logger.debug(
                f"Updated notification flag: task_id={task_id}, flag={flag_name}"
            )
        else:
            logger.warning(
                f"Task not found for flag update: task_id={task_id}, flag={flag_name}"
            )
    except SQLAlchemyError as e:
        logger.error(f"Database error in update_notification_flags: {e}", exc_info=True)
        db.rollback()
