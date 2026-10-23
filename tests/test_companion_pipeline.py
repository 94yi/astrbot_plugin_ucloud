"""Reproduce the live handler-list iterator failure in AstrBot's real process stage."""

import importlib.util
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot_plugin_ucloud.main import Main


class CompanionPipelineTests(unittest.IsolatedAsyncioTestCase):
    def load_guard(self):
        path = os.environ.get("COMPANION_GUARD_TEST_PATH")
        if not path:
            self.skipTest("Optional Companion integration")
        spec = importlib.util.spec_from_file_location(
            "pipeline.astrbot_plugin_companion_io_guard.main", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    async def test_real_process_stage_keeps_submission_after_three_consumed_handlers(
        self,
    ):
        guard = self.load_guard()
        from astrbot.api.platform import (
            AstrBotMessage,
            MessageMember,
            MessageType,
            PlatformMetadata,
        )
        from astrbot.api.message_components import Plain
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )
        from astrbot.core.pipeline.process_stage.stage import ProcessStage
        from astrbot.core.pipeline.process_stage.method.star_request import (
            StarRequestSubStage,
        )
        from astrbot.core.star.star import star_map

        msg = AstrBotMessage()
        msg.type = MessageType.FRIEND_MESSAGE
        msg.self_id = "999999999991"
        msg.sender = MessageMember(user_id="999999999992", nickname="isolated")
        msg.message = [Plain("提交作业")]
        event = AiocqhttpMessageEvent(
            "提交作业",
            msg,
            PlatformMetadata(name="aiocqhttp", description="isolated", id="isolated"),
            "999999999992",
            None,
        )
        plugin = object.__new__(Main)
        plugin._drafts = {}
        plugin._account = AsyncMock(return_value=None)
        plugin._client = SimpleNamespace(
            submit_homework=AsyncMock(), upload_attachment=AsyncMock()
        )
        guard_instance = SimpleNamespace(
            context=SimpleNamespace(get_all_stars=lambda: [])
        )
        visited = []

        async def builtin(_event):
            visited.append("builtin")

        async def route(_event):
            await guard.CompanionIoGuard.platform_only(guard_instance, _event)

        def handler(name, module, callback):
            return SimpleNamespace(
                handler_name=name,
                handler_module_path=module,
                handler_full_name=module + "_" + name,
                handler=callback,
            )

        builtin_module = "isolated.builtin"
        guard_module = "pipeline.astrbot_plugin_companion_io_guard.main"
        cloud_module = "astrbot_plugin_ucloud.main"
        handlers = [
            handler("before" + str(i), builtin_module, builtin) for i in range(3)
        ]
        handlers += [
            handler("platform_only", guard_module, route),
            handler("natural_submit", cloud_module, plugin.natural_submit),
        ]
        event.set_extra("activated_handlers", handlers)
        event.set_extra("handlers_parsed_params", {})
        process = ProcessStage()
        process.ctx = SimpleNamespace(
            astrbot_config={
                "provider_settings": {
                    "enable": True,
                    "prompt_prefix": "",
                    "identifier": False,
                }
            }
        )
        process.star_request_sub_stage = StarRequestSubStage()
        await process.star_request_sub_stage.initialize(process.ctx)
        models = []

        class Model:
            async def process(self, _event):
                models.append(True)
                yield None

        process.agent_sub_stage = Model()
        results = []
        with patch.dict(
            star_map,
            {
                k: SimpleNamespace(name=k)
                for k in (builtin_module, guard_module, cloud_module)
            },
        ):
            async for _ in process.process(event):
                if event.is_stopped():
                    break
                if event.get_result():
                    results.append(event.get_result().get_plain_text())
                    event._has_send_oper = True
        self.assertEqual(len(visited), 3)
        self.assertIn("尚未登录", "".join(results))
        self.assertFalse(models)
        plugin._account.assert_awaited_once()
        plugin._client.submit_homework.assert_not_awaited()
        plugin._client.upload_attachment.assert_not_awaited()

    async def test_guard_precedes_actual_builtin_session_controller(self):
        guard = self.load_guard()
        from astrbot.builtin_stars.astrbot.main import Main as Builtin
        from astrbot.core.star.star_handler import star_handlers_registry

        registered = list(star_handlers_registry)
        ours = next(
            h
            for h in registered
            if h.handler_module_path == guard.CompanionIoGuard.__module__
            and h.handler_name == "platform_only"
        )
        builtin = next(
            h
            for h in registered
            if h.handler_module_path == Builtin.__module__
            and h.handler_name == "handle_session_control_agent"
        )
        self.assertGreater(
            ours.extras_configs["priority"], builtin.extras_configs["priority"]
        )
