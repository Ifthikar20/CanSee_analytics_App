from apps.notifications.models import Notification
from core.exceptions import ResourceNotFound


class NotificationService:
    @staticmethod
    def create(*, user, notification_type: str, title: str, message: str, data: dict = None, action_url: str = "") -> Notification:
        """Create an in-app notification. Clients read it through the API."""
        return Notification.objects.create(
            user=user,
            type=notification_type,
            title=title,
            message=message,
            data=data or {},
            action_url=action_url,
        )

    @staticmethod
    def mark_read(*, notification_id: str, user) -> Notification:
        try:
            notification = Notification.objects.get(id=notification_id, user=user)
        except Notification.DoesNotExist:
            raise ResourceNotFound("Notification not found.") from None
        notification.read = True
        notification.save(update_fields=["read"])
        return notification

    @staticmethod
    def mark_all_read(*, user) -> int:
        return Notification.objects.filter(user=user, read=False).update(read=True)
