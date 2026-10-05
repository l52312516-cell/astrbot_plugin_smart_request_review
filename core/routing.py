"""Bind OneBot actions to an event account without mutating a shared client."""


class AccountClient:
    def __init__(self, client, self_id):
        self.client = client
        self.self_id = str(self_id)

    async def call_action(self, action, **params):
        caller = getattr(self.client, "call_action", None)
        if not callable(caller):
            caller = getattr(getattr(self.client, "api", None), "call_action", None)
        if not callable(caller):
            raise RuntimeError("协议端不支持 call_action")
        if not self.self_id.isdigit() or int(self.self_id) <= 0:
            raise RuntimeError("缺少有效的机器人 self_id，无法路由请求")
        # aiocqhttp consumes this key to select the reverse-WebSocket account.
        # Never retry without it: that could execute an action on another bot.
        params["self_id"] = int(self.self_id)
        return await caller(action, **params)
