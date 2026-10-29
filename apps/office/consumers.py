"""Authenticated, permission-filtered live activity stream."""
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.core.permissions import CEO_ALLOWED
from apps.work.events import COMPANY_GROUP


@database_sync_to_async
def _allowed_departments(user):
    """None = all departments; set() = no access; otherwise the permitted department ids."""
    if not user.is_authenticated or not user.is_active:
        return set()
    if user.is_founder or (user.is_ceo and "view_monitor" in CEO_ALLOWED):
        return None
    if user.is_ceo:
        return set()
    grants = list(user.permission_assignments.filter(permission="view_monitor").values_list("department_id", flat=True))
    if any(g is None for g in grants):
        return None
    return set(grants)


class ActivityConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        self.allowed = await _allowed_departments(user) if user is not None else set()
        if self.allowed == set():
            await self.close(code=4403)
            return
        await self.channel_layer.group_add(COMPANY_GROUP, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(COMPANY_GROUP, self.channel_name)

    async def receive_json(self, content, **kwargs):
        # Read-only stream: control actions go through authorized HTTP endpoints.
        await self.send_json({"error": "read-only channel"})

    async def activity_event(self, message):
        dept = message["payload"].get("department_id")
        if self.allowed is None or dept in self.allowed:
            await self.send_json({"event": message["event"], "payload": message["payload"]})
