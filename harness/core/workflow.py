"""
WorkflowEngine — 工作流引擎 + 状态机

从 YAML 加载工作流定义，管理阶段依赖、条件分支、重试策略。
与 CheckpointManager 集成，支持断点续跑。
"""

import logging
import ast
from time import monotonic
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

from .checkpoint import CheckpointManager
from .io import validate_result

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
    repair_from: Optional[str] = None   # failed output may repair this upstream stage


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
    ):
        self.workflow_path = str(Path(workflow_path).resolve())
        self.spec = self._load_spec(workflow_path)
        self.checkpoint = checkpoint
        self.agents = agent_registry
        self.skill_registry = skill_registry
        self._stage_map = {s.id: s for s in self.spec.stages}

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def run(self, resume: bool = True, inputs_override: Optional[dict] = None,
            metadata: Optional[dict] = None) -> dict:
        """
        运行工作流。
        resume=True 时从上次 checkpoint 继续；False 时全新开始。
        返回最终 state。
        """
        state = self.checkpoint.load() if resume else self.checkpoint.new_state()
        if state.get("workflow_name") not in (None, self.spec.name):
            raise ValueError("Checkpoint belongs to a different workflow; use --no-resume")
        state["workflow_name"] = self.spec.name
        state["workflow_path"] = self.workflow_path
        state["session_dir"] = str(self.checkpoint.session_dir)
        changed_inputs = [sid for sid, value in (inputs_override or {}).items()
                          if value != state.get("stage_inputs_override", {}).get(sid, {})]
        state.setdefault("stage_inputs_override", {}).update(inputs_override or {})
        state.setdefault("metadata", {}).update(metadata or {})
        graph = {s.id: s.depends_on for s in self.spec.stages}
        for removed in set(state["stages"]) - set(graph):
            self.checkpoint.reset_stage(state, removed)
        previous_specs = state.get("stage_specs", {})
        state["stage_dependencies"] = graph
        for sid in changed_inputs:
            if sid in state["stages"]:
                self.checkpoint.reset_stage(state, sid)
        # Changed stage definitions invalidate cached consumers, including legacy checkpoints.
        for stage in self.spec.stages:
            old = previous_specs.get(stage.id)
            if self.checkpoint.is_stage_done(state, stage.id):
                output = self.checkpoint.get_stage_output(state, stage.id)
                try:
                    self._validate_output(stage, output)
                    if old is not None and old != asdict(stage):
                        raise ValueError("Stage definition changed")
                    if any(not self.checkpoint.is_stage_done(state, dep) for dep in stage.depends_on):
                        raise ValueError("Upstream stage is not complete")
                except (ValueError, TypeError, KeyError, SyntaxError):
                    self.checkpoint.reset_stage(state, stage.id)
        state["stage_specs"] = {s.id: asdict(s) for s in self.spec.stages}
        state["status"] = "running"
        self.checkpoint.save(state)

        logger.info(f"[workflow] 开始运行: {self.spec.name}")
        logger.info(f"[workflow] 已完成阶段: {state['completed_stages']}")

        for stage in self.spec.stages:
            state = self._run_stage(state, stage)
            if state["stages"].get(stage.id, {}).get("status") == StageStatus.FAILED:
                logger.error(f"[workflow] 阶段 {stage.id} 失败，终止流程")
                break

        statuses = [state["stages"].get(s.id, {}).get("status") for s in self.spec.stages]
        state["status"] = ("failed" if "failed" in statuses else "completed"
                           if all(s in ("done", "skipped") for s in statuses) else "blocked")
        if state["status"] == "completed":
            state["current_stage"] = None
        self.checkpoint.save(state)
        logger.info("[workflow] 流程结束: %s", state["status"])
        return state

    # ------------------------------------------------------------------
    # 阶段执行
    # ------------------------------------------------------------------

    def _run_stage(self, state: dict, stage: StageSpec) -> dict:
        # 已完成则跳过
        if self.checkpoint.is_stage_done(state, stage.id):
            logger.info(f"[{stage.id}] 已完成，跳过")
            return state

        # Conditional skips propagate to consumers, rather than leaving a false success.
        for dep in stage.depends_on:
            if state["stages"].get(dep, {}).get("status") == "skipped":
                state["stages"][stage.id] = {"status": "skipped", "reason": f"Dependency {dep} skipped"}
                self.checkpoint.save(state)
                return state
            if not self.checkpoint.is_stage_done(state, dep):
                logger.warning(f"[{stage.id}] 依赖 {dep} 未完成，跳过")
                return state

        # 检查条件
        try:
            skip = bool(stage.condition and self._eval_condition(stage.condition, state))
        except (ValueError, KeyError, TypeError, SyntaxError) as exc:
            return self.checkpoint.mark_stage_failed(state, stage.id, str(exc))
        if skip:
            logger.info(f"[{stage.id}] 条件满足，跳过")
            state["stages"][stage.id] = {"status": StageStatus.SKIPPED}
            self.checkpoint.save(state)
            return state

        # 获取 agent
        agent = self.agents.get(stage.agent)
        if agent is None:
            raise ValueError(f"未注册的 agent: {stage.agent}")

        # The final failed attempt is intentionally not repaired inside the
        # loop because that invocation has no execution slot left. On resume,
        # repair from its preserved output before rerunning unchanged code.
        prior = state["stages"].get(stage.id, {})
        prior_failure = prior.get("output") if prior.get("status") == StageStatus.FAILED else None
        if prior.get("status") == StageStatus.RUNNING and not isinstance(prior_failure, dict):
            prior_failure = next((item.get("output")
                                  for item in reversed(prior.get("attempt_history", []))
                                  if isinstance(item.get("output"), dict)
                                  and item["output"].get("success") is False), None)
        if stage.repair_from and isinstance(prior_failure, dict):
            try:
                state = self._repair_upstream(state, stage, prior_failure)
                logger.info("[%s] repaired upstream stage %s before resumed execution",
                            stage.id, stage.repair_from)
            except Exception as repair_error:
                message = f"Automatic resume repair failed: {repair_error}"
                logger.error("[%s] %s", stage.id, message)
                state["stages"][stage.id].setdefault("errors", []).append(message)
                state["stages"][stage.id]["error"] = message
                self.checkpoint.save(state)
                # Preserve the failed execution slot. Running the unchanged
                # upstream output would only repeat a known failure and spend
                # one of this invocation's retry attempts.
                return state

        # 构建输入：静态 inputs → 前序阶段输出 → CLI 注入的 override（优先级递增）
        # Retry allowance is per invocation; an interrupted/exhausted stage can resume.
        for attempt in range(stage.max_retries + 1):
            state = self.checkpoint.mark_stage_started(state, stage.id)
            logger.info(f"[{stage.id}] 开始执行 (第 {attempt + 1} 次)")
            try:
                output = None
                inputs = dict(stage.inputs)
                overrides = state.get("stage_inputs_override", {}).get(stage.id, {})
                for key, source in stage.input_from.items():
                    if key in overrides:
                        continue
                    parts = source.split(".")
                    value = self.checkpoint.get_stage_output(state, parts[0])
                    for part in parts[1:]:
                        if not isinstance(value, dict) or part not in value:
                            raise ValueError(f"Missing input field: {source}")
                        value = value[part]
                    inputs[key] = value
                inputs.update(overrides)
                if stage.timeout is not None:
                    inputs["_timeout"] = stage.timeout
                deadline = monotonic() + stage.timeout if stage.timeout else None
                if hasattr(agent, "_llm"):
                    agent._llm.deadline = deadline
                output = agent.run(stage_id=stage.id, inputs=inputs, state=state)
                if deadline and monotonic() > deadline:
                    raise TimeoutError(f"Stage deadline exceeded: {stage.id}")
                self._validate_output(stage, output)
                if stage.agent == "coder" and self.skill_registry:
                    results = {}
                    for name in self.skill_registry.auto_triggers:
                        skill_inputs = {"dependencies": output.get("dependencies", "")} if name == "dependency_check" else {
                            "code": "\n\n".join(f["content"] for f in output.get("files", [])
                                                 if f["path"].endswith(".py")), "language": "python"}
                        results[name] = self.call_skill(name, skill_inputs)
                        validate_result(results[name])
                    output["skill_results"] = results
                state = self.checkpoint.mark_stage_done(state, stage.id, output)
                logger.info(f"[{stage.id}] 完成")
                return state
            except Exception as e:
                error_msg = str(e)
                logger.error(f"[{stage.id}] 第 {attempt + 1} 次失败: {error_msg}")
                state = self.checkpoint.mark_stage_failed(state, stage.id, error_msg, getattr(e, "output", output))
                if attempt < stage.max_retries and stage.repair_from and isinstance(output, dict):
                    try:
                        state = self._repair_upstream(state, stage, output)
                    except Exception as repair_error:
                        message = f"Automatic repair failed: {repair_error}"
                        logger.error("[%s] %s", stage.id, message)
                        state["stages"][stage.id].setdefault("errors", []).append(message)
                        state["stages"][stage.id]["error"] = message
                        self.checkpoint.save(state)
                        return state
                if attempt >= stage.max_retries:
                    return state

        return state

    def _repair_upstream(self, state: dict, stage: StageSpec, failure_output: dict) -> dict:
        """Use concrete runtime feedback to update a completed generator stage."""
        source_id = stage.repair_from
        source = self._stage_map.get(source_id)
        if source is None or not self.checkpoint.is_stage_done(state, source_id):
            raise ValueError(f"Repair source is not complete: {source_id}")
        repair_agent = self.agents.get(source.agent)
        if repair_agent is None or not callable(getattr(repair_agent, "repair", None)):
            raise ValueError(f"Agent does not support automatic repair: {source.agent}")
        if hasattr(repair_agent, "_llm"):
            repair_agent._llm.deadline = None
        previous = self.checkpoint.get_stage_output(state, source_id)
        repaired = repair_agent.repair(
            stage_id=source_id,
            previous_output=previous,
            failure_output=failure_output,
            state=state,
        )
        self._validate_output(source, repaired)
        state["stages"][source_id]["output"] = repaired
        state["stages"][source_id]["repair_attempts"] = (
            state["stages"][source_id].get("repair_attempts", 0) + 1
        )
        self.checkpoint.save(state)
        logger.info("[%s] repaired upstream stage %s; retrying execution", stage.id, source_id)
        return state

    def _validate_output(self, stage: StageSpec, output: object) -> None:
        validate_result(output)
        agent = self.agents.get(stage.agent)
        if agent is not None and hasattr(agent, "validate_output"):
            agent.validate_output(output)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _load_spec(path: str | Path) -> WorkflowSpec:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not isinstance(raw.get("stages"), list):
            raise ValueError("Workflow needs a name and a stages list")

        stages = []
        for s in raw.get("stages", []):
            if (not isinstance(s, dict) or not isinstance(s.get("id"), str)
                    or not isinstance(s.get("agent"), str)):
                raise ValueError("Each stage needs string id and agent fields")
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
                repair_from=s.get("repair_from"),
            ))

        ids = [s.id for s in stages]
        if len(set(ids)) != len(ids) or not ids:
            raise ValueError("Workflow needs unique stage IDs and at least one stage")
        for stage in stages:
            if (not isinstance(stage.depends_on, list) or not all(isinstance(d, str) for d in stage.depends_on)
                    or not isinstance(stage.inputs, dict) or not isinstance(stage.input_from, dict)
                    or not all(isinstance(ref, str) and all(ref.split('.')) for ref in stage.input_from.values())
                    or not isinstance(stage.max_retries, int)
                    or stage.repair_from is not None and not isinstance(stage.repair_from, str)
                    or stage.timeout is not None and not isinstance(stage.timeout, (int, float))):
                raise ValueError(f"Invalid stage configuration: {stage.id}")
            if stage.max_retries < 0 or (stage.timeout is not None and stage.timeout <= 0):
                raise ValueError(f"Invalid retries/timeout: {stage.id}")
            # Input references are real dependencies, even if omitted from depends_on.
            stage.depends_on = list(dict.fromkeys(stage.depends_on +
                [ref.split('.')[0] for ref in stage.input_from.values()]))
            if any(dep not in ids for dep in stage.depends_on):
                raise ValueError(f"Unknown dependency: {stage.id}")
            if stage.repair_from is not None and stage.repair_from not in stage.depends_on:
                raise ValueError(f"repair_from must be an upstream dependency: {stage.id}")
        ordered = []
        remaining = list(stages)
        while remaining:
            ready = [s for s in remaining if set(s.depends_on) <= {x.id for x in ordered}]
            if not ready:
                raise ValueError("Workflow dependency cycle")
            ordered.extend(ready)
            remaining = [s for s in remaining if s not in ready]
        return WorkflowSpec(
            name=raw["name"],
            description=raw.get("description", ""),
            stages=ordered,
        )

    @staticmethod
    def _eval_condition(expr: str, state: dict) -> bool:
        """安全地求值跳过条件表达式，可访问 state 变量。"""
        tree = ast.parse(expr, mode="eval")
        allowed = (ast.Expression, ast.BoolOp, ast.UnaryOp, ast.Compare, ast.Subscript,
                   ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple, ast.Dict,
                   ast.And, ast.Or, ast.Not, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
                   ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot)
        if any(not isinstance(node, allowed) or isinstance(node, ast.Name) and node.id != "state"
               for node in ast.walk(tree)):
            raise ValueError("Conditions only support state indexing and boolean comparisons")
        return bool(eval(compile(tree, "<condition>", "eval"), {"__builtins__": {}}, {"state": state}))

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
