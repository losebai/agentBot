"""管理入口独立媒体适配器：不构造 QQ 身份，不使用 QQ 投递队列。"""

import asyncio
import json
from dataclasses import asdict

from agent_bot.agent.contracts import ToolDefinition, ToolResult
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.local_media import (
    ARGUMENT_MODELS,
    project_local_schema,
    validate_local_call,
)
from agent_bot.agent.tools.policy import ADMIN_MEDIA_TOOLS
from agent_bot.database_pool import finish_database_write
from agent_bot.media.inputs import ImagePreparer
from agent_bot.media.store import finish_cleanup
from agent_bot.models import ChatMediaRequest
from agent_bot.multimodal import DownloadedImage
from agent_bot.services.chat_tools import build_chat_tool_registry
from agent_bot.services.image_task_context import active_image_task
from agent_bot.services.request_deadline import clear_request_deadline


class OwnedPrompt:
    """只跟踪当前管理请求的 Comfy 提交，不接受模型传入提示标识。"""

    def __init__(self, client):
        """服务端装配的客户端决定可取消的固定上游。"""
        self.client, self.prompt_id, self.cancelled = client, None, False

    async def submitting(self, endpoint):
        """取消后的请求不能再次提交；地址仅来自已装配的 Comfy 客户端。"""
        await self.check_cancelled()

    async def submitted(self, prompt_id, endpoint):
        """只有成功提交回调可以设置本请求的取消目标。"""
        self.prompt_id = prompt_id
        await self.check_cancelled()

    async def check_cancelled(self):
        """本地取消意图不等于上游已停止，终态记录由 Agent 保留 unknown。"""
        if self.cancelled:
            raise asyncio.CancelledError

    async def cancel_owned(self):
        """最多五秒尝试按 ID 取消，绝不回退到全局 interrupt。"""
        self.cancelled = True
        if self.client is not None and self.prompt_id is not None:
            with clear_request_deadline():
                try:
                    await asyncio.wait_for(self.client.cancel(self.prompt_id), 5)
                except Exception:
                    pass


class AdminMediaTools:
    """运行时配置决定可用适配器，执行期间使用同一客户端快照。"""

    def __init__(
        self,
        config,
        workflow_service,
        tts_client,
        media_store,
        *,
        comfy_client=None,
        resources=None,
        image_service=None,
    ):
        """引用公共生图服务及本地工作流资源，不另建连接或伪造 QQ 身份。"""
        self.config, self.workflow, self.tts, self.store = (
            config,
            workflow_service,
            tts_client,
            media_store,
        )
        self.comfy = comfy_client
        self.resources = resources
        self.image_service = image_service

    @property
    def allowed_tools(self):
        """直接模型生图与 Comfy 独立授权，关闭的能力不向模型宣传。"""
        if self.resources is not None and not self.resources.readiness.storage_available:
            return frozenset()
        names = set()
        if self.image_service is not None and self.config.chat_image_generation_enabled:
            names.add("image_generate")
        if self.config.chat_local_workflow_enabled and self.workflow is not None:
            names.update(
                {
                    "local_workflow_list",
                    "local_workflow_generate_image",
                    "local_workflow_generate_video",
                }
            )
        if self.config.chat_tts_enabled and self.tts is not None:
            names.add("tts_generate")
        return frozenset(names)

    def bind(self, scope, images):
        """管理执行与会话归属来自服务器，QQ 入口不可借用此适配器。"""
        if scope.owner.entry != "admin":
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        return AdminMediaToolSession(self, scope, images)


class AdminMediaToolSession:
    """一次执行只允许一张图片与一段语音，失败调用同样占用额度。"""

    def __init__(self, owner, scope, images):
        """为当前执行构造独立额度与有限工具说明。"""
        self.owner, self.scope, self.images, self.used = owner, scope, images, set()
        descriptions = {
            "image_generate": (
                "直接使用管理员配置的图片生成模型生成一张图片，无需 Comfy 工作流。"
                "prompt 写完整画面要求，start_reply 写开画文案；结果保存为当前会话附件。"
                "只支持文生图，不能把当前附图作为编辑输入。与工作流生图共享一次额度。"
            ),
            "local_workflow_list": "列出已配置且可执行的本地工作流，绘图前必须先选择工作流。",
            "local_workflow_generate_image": (
                "使用所选工作流生成一张图片并保存为本次会话附件。每次请求最多一次。"
            ),
            "local_workflow_generate_video": "使用图生视频工作流将当前唯一原图生成视频并保存附件。",
            "tts_generate": (
                "使用固定 GPT-SoVITS 音色合成回复正文并保存语音附件，每次请求最多一次。"
            ),
        }
        self.definitions = tuple(
            ToolDefinition(
                name=tool.name,
                source="media",
                description=descriptions[tool.name],
                input_schema=project_local_schema(tool.name, tool.input_schema),
            )
            for tool in build_chat_tool_registry().list_tools(
                owner.allowed_tools & ADMIN_MEDIA_TOOLS
            )
        )

    def validate(self, call):
        """参数预检没有副作用，可以用于整批调用验证。"""
        if call.name not in {tool.name for tool in self.definitions}:
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        validate_local_call(call)

    async def call(self, call):
        """先检查共享绘图额度，直接模型生图和工作流不能各自生成一张。"""
        self.validate(call)
        parsed = ARGUMENT_MODELS[call.name].model_validate(call.arguments)
        if call.name == "local_workflow_list":
            workflows = await self.owner.workflow.list_workflows()
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                content=json.dumps(
                    {"workflows": [asdict(w) for w in workflows]}, ensure_ascii=False
                ),
            )
        quota = (
            "image"
            if call.name
            in {"image_generate", "local_workflow_generate_image", "local_workflow_generate_video"}
            else call.name
        )
        if quota in self.used:
            raise AgentError("AGENT_TOOL_LIMIT", status_code=422)
        self.used.add(quota)
        if call.name == "image_generate":
            asset = await self._direct_image(parsed)
        elif call.name in {"local_workflow_generate_image", "local_workflow_generate_video"}:
            asset = await self._image(parsed, video=call.name == "local_workflow_generate_video")
        else:
            speech = await self.owner.tts.generate(parsed.text)

            def read_speech():
                """有界读取可信服务返回的文件，不删除其他语音流程拥有的产物。"""
                with speech.path.open("rb") as file:
                    data = file.read(10 * 1024 * 1024 + 1)
                if (
                    speech.media_type != "audio/wav"
                    or len(data) != speech.bytes
                    or not 12 <= len(data) <= 10 * 1024 * 1024
                    or data[:4] != b"RIFF"
                    or data[8:12] != b"WAVE"
                ):
                    raise AgentError("MEDIA_INVALID_RESPONSE")
                return data

            data = await finish_database_write(read_speech)
            asset = await self.owner.store.put(
                owner=self.scope.owner,
                scope=self.scope,
                purpose="output",
                data=data,
                mime_type="audio/wav",
            )
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            attachments=(asset,),
            content=json.dumps(
                {"status": "ready", "mime_type": asset.mime_type, "bytes": asset.size_bytes}
            ),
        )

    async def _direct_image(self, parsed):
        """模型、预算和会话身份来自服务端，附件关联当前 Agent 执行以供历史查询。"""
        config = self.owner.config
        request = ChatMediaRequest(
            model=config.chat_image_model, session_id=self.scope.owner.session_id,
            request_id=f"agent-{self.scope.execution_id}", user="admin", output_type="IMAGE",
            prompt=parsed.prompt, image_format=config.chat_image_response_format,
            timeout_seconds=max(1, int(min(120, config.chat_image_timeout_seconds))),
            max_bytes=config.chat_image_max_bytes,
        )
        result = await self.owner.image_service.generate(
            request, self.scope.owner, scope=self.scope,
        )
        return result.attachments[0]

    async def _image(self, parsed, *, video=False):
        """图生图只取本执行绑定的唯一原图，并跟踪自己的上游提交。"""
        summary = await self.owner.workflow.describe_workflow(parsed.workflow_key)
        if (getattr(summary, "workflow_type", "text_to_image") == "image_to_video") != video:
            raise AgentError("AGENT_TOOL_ARGUMENTS_INVALID", status_code=422)
        if summary.requires_image and len(self.images) != 1:
            raise AgentError("MEDIA_IMAGE_REQUIRED", status_code=422)
        image = self.images[0] if summary.requires_image else None
        request = await self.owner.workflow.prepare_request(
            workflow_key=parsed.workflow_key,
            positive_prompt=parsed.positive_prompt,
            negative_prompt=getattr(parsed, "negative_prompt", ""),
            request_scope=self.scope.execution_id,
            input_image=DownloadedImage(image.media_type, image.data) if image else None,
        )
        execution = OwnedPrompt(self.owner.comfy)
        token = active_image_task.set(execution)
        completed = False
        try:
            generate = self.owner.workflow.generate_video if video else self.owner.workflow.generate
            generated = await generate(
                workflow_key=request.workflow_key,
                positive_prompt=request.positive_prompt,
                negative_prompt=request.negative_prompt,
                input_image=request.input_image,
                workflow_sha256=request.workflow_sha256,
            )
            completed = True
            if video:
                return await self.owner.store.put(
                    owner=self.scope.owner, scope=self.scope, purpose="output",
                    data=generated.data, mime_type="video/mp4",
                )
            image = await ImagePreparer().prepare_async(generated.data, generated.media_type)
            if self.owner.resources is not None:
                # 管理 Agent 的结果直接进入本会话素材库，不依赖 QQ 发送或临时附件寿命。
                await self.owner.resources.materials.put(
                    owner=self.scope.owner, name="工作流生成图片", data=image.data,
                    mime_type=image.media_type, category="generated",
                )
            return await self.owner.store.put(
                owner=self.scope.owner,
                scope=self.scope,
                purpose="output",
                data=image.data,
                mime_type=image.media_type,
            )
        except BaseException:
            if not completed:
                await finish_cleanup(execution.cancel_owned())
            raise
        finally:
            active_image_task.reset(token)
