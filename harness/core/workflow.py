"""
WorkflowEngine — 工作流引擎 + 状态机

从 YAML 加载工作流定义，管理阶段依赖、条件分支、重试策略。
与 CheckpointManager 集成，支持断点续跑。
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from .checkpoint import CheckpointManager

logger = logging.getLogger(__name__)


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StageSpec:
    """从 YAML 解析出的阶段定义。"""
    id: str
    name: str
    agent: str                          # 使用哪个 agent
    depends_on: list[str] = field(default_factory=list)
    max_retries: int = 2
    inputs: dict[str, Any] = field(default_factory=dict)   # 静态输入
    input_from: dict[str, str] = field(default_factory=dict)  # 从其他阶段输出取值
    condition: Optional[str] = None     # 跳过条件（Python 表达式）
    timeout: Optional[int] = None       # 秒


@dataclass
class WorkflowSpec:
    name: str
    description: str
    stages: list[StageSpec]


class WorkflowEngine:
    def __init__(
        self,
        workflow_path: str | Path,
        checkpoint: CheckpointManager,
        agent_registry: dict[str, Any],  # agent_name -> agent 实例
        skill_registry: Optional[Any] = None,  # SkillRegistry 实例
        auto_skill_hunt: bool = False,  # 失败时自动搜索社区 skills
    ):
        self.spec = self._load_spec(workflow_path)
        self.checkpoint = checkpoint
        self.agents = agent_registry
        self.skill_registry = skill_registry
        self.auto_skill_hunt = auto_skill_hunt
        self._stage_map = {s.id: s for s in self.spec.stages}

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def run(self, resume: bool = True) -> dict:
        """
        运行工作流。
        resume=True 时从上次 checkpoint 继续；False 时全新开始。
        返回最终 state。
        """
        state = self.checkpoint.load() if resume else {}
        if not state.get("workflow_name"):
            state = self.checkpoint.load()  # 拿到 _empty_state 结构
            state["workflow_name"] = self.spec.name
            self.checkpoint.save(state)

        # 将 session_dir 注入 state，供 ExecutorAgent 等需要写磁盘的 agent 使用
        state["session_dir"] = str(self.checkpoint.session_dir)

        logger.info(f"[workflow] 开始运行: {self.spec.name}")
        logger.info(f"[workflow] 已完成阶段: {state['completed_stages']}")

        # 迭代执行，直到没有阶段触发 rerun
        max_iterations = 5  # 防止无限循环
        for iteration in range(max_iterations):
            logger.info(f"[workflow] 第 {iteration + 1} 次迭代")
            any_rerun = False

            for stage in self.spec.stages:
                state, rerun_triggered = self._run_stage(state, stage)
                if rerun_triggered:
                    any_rerun = True
                    logger.info(f"[workflow] 阶段 {stage.id} 触发重新执行，开始新一轮迭代")
                    break  # 立即开始新一轮迭代

                if state["stages"].get(stage.id, {}).get("status") == StageStatus.FAILED:
                    logger.error(f"[workflow] 阶段 {stage.id} 失败，终止流程")
                    logger.info("[workflow] 流程结束")
                    return state

            if not any_rerun:
                logger.info("[workflow] 所有阶段已完成，无需重新执行")
                break
        else:
            logger.warning(f"[workflow] 达到最大迭代次数 {max_iterations}，停止执行")

        logger.info("[workflow] 流程结束")
        return state

    # ------------------------------------------------------------------
    # 阶段执行
    # ------------------------------------------------------------------

    def _run_stage(self, state: dict, stage: StageSpec) -> tuple[dict, bool]:
        """
        执行单个阶段。
        返回 (更新后的state, 是否触发了重新执行)
        """
        rerun_triggered = False

        # 已完成则跳过
        if self.checkpoint.is_stage_done(state, stage.id):
            logger.info(f"[{stage.id}] 已完成，跳过")
            return state, rerun_triggered

        # 检查依赖
        for dep in stage.depends_on:
            if not self.checkpoint.is_stage_done(state, dep):
                logger.warning(f"[{stage.id}] 依赖 {dep} 未完成，跳过")
                return state, rerun_triggered

        # 检查条件
        if stage.condition and self._eval_condition(stage.condition, state):
            logger.info(f"[{stage.id}] 条件满足，跳过")
            state["stages"][stage.id] = {"status": StageStatus.SKIPPED}
            self.checkpoint.save(state)
            return state, rerun_triggered

        # 获取 agent
        agent = self.agents.get(stage.agent)
        if agent is None:
            raise ValueError(f"未注册的 agent: {stage.agent}")

        # 构建输入：静态 inputs → 前序阶段输出 → CLI 注入的 override（优先级递增）
        inputs = dict(stage.inputs)
        for key, source in stage.input_from.items():
            # source 格式: "stage_id.field" 或 "stage_id"
            parts = source.split(".", 1)
            src_output = self.checkpoint.get_stage_output(state, parts[0])
            inputs[key] = src_output.get(parts[1]) if len(parts) > 1 and isinstance(src_output, dict) else src_output
        # 合并 main.py 通过 state["stage_inputs_override"] 注入的参数
        overrides = state.get("stage_inputs_override", {}).get(stage.id, {})
        inputs.update(overrides)

        # 执行（带重试）
        attempts = state["stages"].get(stage.id, {}).get("attempts", 0)
        session_dir = state.get("session_dir", "")
        for attempt in range(attempts, stage.max_retries + 1):
            state = self.checkpoint.mark_stage_started(state, stage.id)
            logger.info(f"[{stage.id}] 开始执行 (第 {attempt + 1} 次)")
            # 清空对话缓存
            if hasattr(agent, "clear_conversations"):
                agent.clear_conversations()
            try:
                output = agent.run(stage_id=stage.id, inputs=inputs, state=state)
                state = self.checkpoint.mark_stage_done(state, stage.id, output)
                logger.info(f"[{stage.id}] 完成")
                # 保存对话记录
                if hasattr(agent, "save_conversations"):
                    agent.save_conversations(session_dir, stage.id)

                # 检查是否需要重新执行某些阶段（用于迭代）
                if isinstance(output, dict) and output.get("needs_revision") and output.get("rerun_stages"):
                    rerun_list = output["rerun_stages"]
                    logger.info(f"[{stage.id}] 触发重新执行: {rerun_list}")
                    for rerun_stage_id in rerun_list:
                        if rerun_stage_id in state["completed_stages"]:
                            state["completed_stages"].remove(rerun_stage_id)
                            logger.info(f"[workflow] 清除阶段 {rerun_stage_id} 的完成状态")
                        if rerun_stage_id in state["stages"]:
                            state["stages"][rerun_stage_id] = {"status": StageStatus.PENDING, "attempts": 0}
                    self.checkpoint.save(state)
                    rerun_triggered = True

                return state, rerun_triggered
            except Exception as e:
                error_msg = str(e)
                logger.error(f"[{stage.id}] 第 {attempt + 1} 次失败: {error_msg}")
                # 即使失败也保存对话记录（便于调试）
                if hasattr(agent, "save_conversations"):
                    agent.save_conversations(session_dir, stage.id)
                if attempt >= stage.max_retries:
                    state = self.checkpoint.mark_stage_failed(state, stage.id, error_msg)

                    # 自动社区 skill 获取：失败后尝试从开源社区寻找解决方案
                    if self.auto_skill_hunt:
                        resolved = self._try_auto_resolve(stage, error_msg, state)
                        if resolved:
                            logger.info(f"[{stage.id}] 社区 skill 集成成功，重置阶段重试")
                            state["stages"][stage.id] = {"status": StageStatus.PENDING, "attempts": 0}
                            self.checkpoint.save(state)
                            rerun_triggered = True
                            return state, rerun_triggered

                    return state, rerun_triggered

        return state, rerun_triggered

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _load_spec(path: str | Path) -> WorkflowSpec:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        stages = []
        for s in raw.get("stages", []):
            stages.append(StageSpec(
                id=s["id"],
                name=s.get("name", s["id"]),
                agent=s["agent"],
                depends_on=s.get("depends_on", []),
                max_retries=s.get("max_retries", 2),
                inputs=s.get("inputs", {}),
                input_from=s.get("input_from", {}),
                condition=s.get("condition"),
                timeout=s.get("timeout"),
            ))

        return WorkflowSpec(
            name=raw["name"],
            description=raw.get("description", ""),
            stages=stages,
        )

    @staticmethod
    def _eval_condition(expr: str, state: dict) -> bool:
        """安全地求值跳过条件表达式，可访问 state 变量。"""
        try:
            return bool(eval(expr, {"__builtins__": {}}, {"state": state}))  # noqa: S307
        except Exception:
            return False

    def status(self, state: dict) -> None:
        """打印当前工作流状态。"""
        print(f"\n{'='*50}")
        print(f"工作流: {self.spec.name}")
        print(f"{'='*50}")
        for stage in self.spec.stages:
            info = state["stages"].get(stage.id, {})
            status = info.get("status", "pending")
            icon = {"done": "✓", "running": "→", "failed": "✗", "skipped": "○", "pending": "·"}.get(status, "?")
            print(f"  {icon} [{stage.id}] {stage.name}  ({status})")
        print()

    def call_skill(self, skill_name: str, inputs: dict) -> dict:
        """
        调用一个 skill。

        Args:
            skill_name: skill 名称
            inputs: 输入参数

        Returns:
            skill 执行结果
        """
        if self.skill_registry is None:
            raise ValueError("SkillRegistry 未初始化")
        logger.info(f"[workflow] 调用 skill: {skill_name}")
        return self.skill_registry.execute(skill_name, inputs)

    def list_skills(self) -> list[dict[str, str]]:
        """列出所有可用的 skills。"""
        if self.skill_registry is None:
            return []
        return self.skill_registry.list_skills()

    def _try_auto_resolve(self, stage: StageSpec, error_msg: str, state: dict) -> bool:
        """
        在阶段失败后自动调用 SkillHunter → SkillIntegrator 全流程。

        尝试从社区获取新 skills 来解决问题，集成成功后返回 True。

        Args:
            stage: 失败的阶段
            error_msg: 错误信息
            state: 工作流状态

        Returns:
            True 如果成功集成了新的 skill
        """
        try:
            from harness.tools.skill_integrator import auto_resolve_failure

            # 收集上下文
            task_description = f"Stage: {stage.id} ({stage.name}), Agent: {stage.agent}, Error: {error_msg}"
            existing_skills = self.list_skills()

            logger.info(f"[{stage.id}] 启动自动社区 skill 搜索...")
            result = auto_resolve_failure(
                stage_id=stage.id,
                error_msg=error_msg,
                task_description=task_description,
                existing_skills=existing_skills,
            )

            if result and result["success"]:
                # 刷新 skill_registry 引用
                if self.skill_registry is not None:
                    from harness.core.skill import get_global_registry
                    self.skill_registry = get_global_registry()
                logger.info(f"[{stage.id}] 社区 skill 集成成功: {result['skill_name']}")
                return True

            logger.info(f"[{stage.id}] 未找到合适的社区 skill")
            return False

        except ImportError as e:
            logger.warning(f"[{stage.id}] SkillIntegrator 导入失败: {e}")
            return False
        except Exception as e:
            logger.warning(f"[{stage.id}] 自动社区 skill 搜索异常: {e}")
            return False
