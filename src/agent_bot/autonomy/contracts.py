"""持续身份、宿主策略、事件和计划的严格契约，模型不能提供权限或投递身份。"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_bot.jobs.contracts import JobOwner
from agent_bot.memory.contracts import MemoryKind

Action = Literal["plan", "recall", "draft", "consolidate", "review", "report"]


class Contract(BaseModel):
    """拒绝额外字段和非有限数值，不能通过模型输出添加未实现的行动能力。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)


class Identity(Contract):
    """固定身份与边界是版本化设定，情绪和任务进度不能写入这里。"""

    name: str = Field(min_length=1, max_length=100)
    role: str = Field(default="", max_length=6000)
    style: str = Field(default="", max_length=2000)
    relationships: str = Field(default="", max_length=2000)
    boundaries: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def context_size(self):
        """身份必须完整恢复，设定总长受限以保留原始证据和模型协议空间。"""
        if sum(len(value) for value in self.model_dump().values()) > 3000:
            raise ValueError("identity exceeds 3000 characters")
        return self


class LocalOwner(Contract):
    """独立主体绑定本地参与者和固定会话，身份不依赖外部平台账户。"""

    channel: Literal['local'] = 'local'
    agent_key: str = Field(min_length=1, max_length=100)
    participant_id: str = Field(min_length=1, max_length=100)
    session_id: str = Field(min_length=1, max_length=256)

    @property
    def owner_key(self) -> str:
        """本地所有权加渠道域，不能与已有平台绑定碰撞。"""
        payload = json.dumps(['local', self.agent_key, self.participant_id, self.session_id])
        return hashlib.sha256(payload.encode()).hexdigest()


class Policy(Contract):
    """宿主执行策略默认暂停；费用先按保守 token 上界预留，未知单价禁止模型行动。"""

    paused: bool = True
    model: str = Field(default="", max_length=200)
    allowed_actions: tuple[Action, ...] = ("recall", "review", "report")
    discover_tasks: bool = False
    auto_consolidate: bool = False
    auto_correct: bool = False
    notify_qq: bool = False
    max_model_calls_per_day: int = Field(default=10, ge=0, le=1000)
    daily_cost_limit: float = Field(default=0, ge=0, le=1000)
    input_cost_per_million: float | None = Field(default=None, ge=0, le=10000)
    output_cost_per_million: float | None = Field(default=None, ge=0, le=10000)
    max_attempts: int = Field(default=3, ge=1, le=5)
    min_report_interval_seconds: int = Field(default=3600, ge=60, le=604800)
    max_reports_per_day: int = Field(default=5, ge=0, le=100)
    quiet_start_hour: int = Field(default=23, ge=0, le=23)
    quiet_end_hour: int = Field(default=8, ge=0, le=23)
    episode_retention_days: int = Field(default=90, ge=1, le=3650)

    @model_validator(mode="after")
    def unique_actions(self):
        """权限集合不能含重复项，价格必须显式成对配置。"""
        if len(set(self.allowed_actions)) != len(self.allowed_actions):
            raise ValueError("duplicate actions")
        if (self.input_cost_per_million is None) != (self.output_cost_per_million is None):
            raise ValueError("both model prices are required")
        return self


class SelfState(Contract):
    """当前状态单独持久化；情绪是可观察的表达状态，不是意识或主观体验证据。"""

    current_goal: str = Field(default="", max_length=2000)
    current_task: str | None = None
    activity: str = Field(default="sleeping", max_length=100)
    emotion: Literal["neutral", "focused", "uncertain", "concerned"] = "neutral"
    recent_errors: tuple[str, ...] = Field(default=(), max_length=10)


class ProfileCreate(Contract):
    """管理员绑定机器人和会话目标，模型输出只能使用已有绑定。"""

    owner: LocalOwner | JobOwner
    identity: Identity
    policy: Policy = Field(default_factory=Policy)

    @model_validator(mode='after')
    def local_defaults(self):
        """新本地身份允许文字回复但仍默认暂停，旧渠道默认策略保持原样。"""
        if isinstance(self.owner, LocalOwner) and 'policy' not in self.model_fields_set:
            self.policy = Policy(allowed_actions=('draft', 'recall', 'review', 'report'))
        return self


class ProfileUpdate(Contract):
    """身份、策略和自我状态均以所见版本提交，暂停另有无版本紧急入口。"""

    expected_revision: int = Field(ge=1)
    identity: Identity | None = None
    policy: Policy | None = None
    state: SelfState | None = None


class EpisodeCreate(Contract):
    """仅可信入口可提交事件身份和任务意图，引用与模型推测不能伪装本人发言。"""

    event_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=8000)
    occurred_at: datetime
    task_requested: bool = False
    source: Literal["self_report", "third_party"] = "self_report"

    @model_validator(mode="after")
    def trusted_event(self):
        """事件必须携带时区，第三方资料不能触发用户任务发现。"""
        if self.occurred_at.utcoffset() is None or not self.text.strip():
            raise ValueError("invalid episode")
        self.occurred_at = self.occurred_at.astimezone(timezone.utc)
        if self.task_requested and self.source != "self_report":
            raise ValueError("third party cannot request tasks")
        return self


class Step(Contract):
    """模型可计划的动作仅限内建能力，不能执行命令、网页指定地址或任意远程工具。"""

    action: Action
    text: str = Field(default="", max_length=4000)
    tags: tuple[str, ...] = Field(default=(), max_length=8)


class Plan(Contract):
    """有界顺序计划逐步保存，依赖前面步骤输出，不允许递归规划。"""

    steps: tuple[Step, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def no_recursive_plan(self):
        """规划只能在宿主启动步骤出现，防止模型递归消耗预算。"""
        if any(step.action in {"plan", "consolidate"} for step in self.steps):
            raise ValueError("recursive or unbound plan")
        return self


class TaskCreate(Contract):
    """任务幂等键来自调用方事件；没有明确步骤时先调用受预算限制的规划器。"""

    request_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=4000)
    steps: tuple[Step, ...] = Field(default=(), max_length=8)
    run_at: datetime | None = None

    @model_validator(mode="after")
    def schedule(self):
        """绝对时间保留真实时区，显式计划不能注入缺少情节绑定的整理动作。"""
        if self.run_at is not None and self.run_at.utcoffset() is None:
            raise ValueError("task time requires timezone")
        if any(step.action == "consolidate" for step in self.steps):
            raise ValueError("consolidation requires bound episode")
        return self


class FactProposal(Contract):
    """事实提取必须逐字引用事件来源；更正还需绑定旧事实及版本。"""

    key: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=1000)
    excerpt: str = Field(min_length=1, max_length=1000)
    kind: MemoryKind = "fact"
    tags: tuple[str, ...] = Field(min_length=1, max_length=8)
    correction_id: int | None = Field(default=None, ge=1)
    expected_revision: int | None = Field(default=None, ge=1)
    duplicate_id: int | None = Field(default=None, ge=1)
    duplicate_revision: int | None = Field(default=None, ge=1)
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def correction_version(self):
        """模型不能提交不带版本的覆盖，过期时间也必须明确时区。"""
        if (self.correction_id is None) != (self.expected_revision is None):
            raise ValueError("correction requires id and revision")
        if (self.duplicate_id is None) != (self.duplicate_revision is None):
            raise ValueError("duplicate requires id and revision")
        if self.correction_id is not None and self.duplicate_id is not None:
            raise ValueError("correction and duplication are distinct")
        if self.valid_until is not None and self.valid_until.utcoffset() is None:
            raise ValueError("fact time requires timezone")
        return self


class Consolidation(Contract):
    """摘要与可验证事实分开；摘要不作为事实来源，空事实列表表示无需长期保留。"""

    summary: str = Field(min_length=1, max_length=2000)
    facts: tuple[FactProposal, ...] = Field(default=(), max_length=8)


class Decision(Contract):
    """审批须绑定任务或提案的当前版本，不能审批未来变化后的动作。"""

    expected_revision: int = Field(ge=1)
    approve: bool


class LocalMessage(Contract):
    """浏览器只提交正文与幂等键，来源身份和时间由宿主绑定。"""

    request_id: str = Field(min_length=1, max_length=123)
    text: str = Field(min_length=1, max_length=4000)
