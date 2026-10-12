"""文生图测试端口：附件不得进入上传或旧生成参数。"""

from types import SimpleNamespace

from agent_bot.services.workflow_execution import WorkflowImageRequest, WorkflowSummary


class TextWorkflowServiceStub(SimpleNamespace):
    async def describe_workflow(self, workflow_key: str) -> WorkflowSummary:
        """旧测试固定使用文生图工作流。"""
        return WorkflowSummary(workflow_key, workflow_key, "")

    async def prepare_request(
        self, *, workflow_key, positive_prompt, negative_prompt, image_urls, request_scope,
    ) -> WorkflowImageRequest:
        """构造旧参数，不发起任何上传。"""
        return WorkflowImageRequest(
            workflow_key=workflow_key, positive_prompt=positive_prompt,
            negative_prompt=negative_prompt,
        )
