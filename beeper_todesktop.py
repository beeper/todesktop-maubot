from typing import Type, Match, Optional, Dict, Callable, Awaitable, Any
import asyncio
import secrets
import json
import re

from aiohttp import ContentTypeError, ClientSession, web
from aiohttp.web import Request, Response
from yarl import URL

from mautrix.types import RoomID, JSON, MessageType
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from maubot import Plugin
from maubot.handlers import web


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        if not self["webhook_secret"] or self["webhook_secret"] == "put a random password here":
            helper.base["webhook_secret"] = secrets.token_urlsafe(32)
        else:
            helper.copy("webhook_secret")
        helper.copy("gitlab_url")
        helper.copy("gitlab_token")
        helper.copy("projects")


url_pattern = re.compile("https://dl.todesktop.com/([a-z0-9]+)/builds/([a-z0-9]+)")
WebhookHandler = Callable[[Dict[str, Any], JSON], Awaitable[Dict[str, str]]]


class TodesktopBot(Plugin):
    base_url: URL
    webhook_handlers: Dict[str, WebhookHandler]

    async def start(self):
        self.on_external_config_update()
        self.webhook_handlers = {
            "todesktop": self.handle_todesktop,
            "android": self.handle_android,
        }

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    def on_external_config_update(self) -> None:
        self.config.load_and_update()
        self.base_url = URL(self.config["gitlab_url"])

    async def find_todesktop_build(self, project_id: int, job_id: int) -> Optional[Match]:
        url = self.base_url / f"api/v4/projects/{project_id}/jobs/{job_id}/trace"
        headers = {"PRIVATE-TOKEN": self.config["gitlab_token"]}
        async with ClientSession() as sess, sess.get(url, headers=headers) as resp:
            resp.raise_for_status()
            text = await resp.text()
        return url_pattern.search(text)

    async def handle_todesktop(self, _: Dict[str, Any], data: JSON) -> Dict[str, str]:
        try:
            match = await self.find_todesktop_build(data["project_id"], data["build_id"])
        except Exception:
            self.log.exception("Error finding todesktop build ID")
            raise web.HTTPInternalServerError(text="500: Internal Server Error\n"
                                                   "Failed to get todesktop build ID\n")
        if not match:
            raise ValueError("Todesktop URL not found")
        return {"todesktop_url": match.group(0)}

    @staticmethod
    async def handle_android(project: Dict[str, Any], data: JSON) -> Dict[str, str]:
        apk_path = project["apk_path_map"][data["build_name"]]
        repo_url = data["repository"]["homepage"]
        job_id = data["build_id"]
        return {"apk_url": f"{repo_url}/-/jobs/{job_id}/artifacts/browse/{apk_path}"}

    async def handle_webhook(self, room_id: RoomID, data: JSON) -> str:
        try:
            project = self.config["projects"][data["project_id"]]
        except KeyError:
            raise web.HTTPNotFound(text="404: Unknown project ID")
        if data["build_status"] != "success":
            return "Non-success webhook ignored"
        try:
            extra_params = await self.webhook_handlers[project["type"]](project, data)
        except ValueError as e:
            return str(e)
        message = project["message_format"].format(**{
            "build_name": project["build_name_map"][data["build_name"]],
            "commit_hash": data["sha"][:8],
            "commit_url": URL(data["repository"]["homepage"]) / "-" / "commit" / data["sha"],
            **extra_params,
        })
        try:
            await self.client.send_markdown(room_id, message, msgtype=MessageType.NOTICE)
        except Exception:
            self.log.exception("Error sending message to Matrix")
            raise web.HTTPInternalServerError(text="500: Internal Server Error\n"
                                                   "Failed to send notification to Matrix\n")
        return "Notification sent"

    @web.post("/webhooks")
    async def webhook(self, request: Request) -> Response:
        try:
            token = request.headers["X-Gitlab-Token"]
        except KeyError:
            return Response(text="401: Unauthorized\n"
                                 "Missing auth token header\n", status=401)
        if token != self.config["webhook_secret"]:
            return Response(text="401: Unauthorized\n", status=401)
        try:
            room_id = RoomID(request.query["room"])
        except KeyError:
            return Response(text="400: Bad request\nNo room specified. "
                                 "Did you forget the ?room query parameter?\n",
                            status=400)
        try:
            data = await request.json()
        except ContentTypeError:
            return Response(status=406, text="406: Not Acceptable\n",
                            headers={"Accept": "application/json"})
        except json.JSONDecodeError:
            return Response(status=400, text="400: Bad request\nRequest body not JSON\n")

        message = await asyncio.shield(self.handle_webhook(room_id, data))

        return Response(status=200, text=f"200: OK\n{message}\n")
