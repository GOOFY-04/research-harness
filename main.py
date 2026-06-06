#!/usr/bin/env python3
"""
research-harness — 面向长流程科研的智能体框架

用法：
  python main.py run --direction "你的研究方向"
  python main.py run --direction "..." --session my_session   # 指定 session 名
  python main.py resume --session session_20260519_120000     # 从断点继续
  python main.py status --session session_20260519_120000     # 查看进度
  python main.py list                                          # 列出所有 session
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml

# 加载 .env（优先级低于已有环境变量，不会覆盖系统设置）
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass  # python-dotenv 未安装时静默跳过

from harness.core import CheckpointManager, MemoryStore, WorkflowEngine
from harness.core.skill import get_global_registry
from harness.agents import (
    PlannerAgent,
    LiteratureAgent,
    MethodAgent,
    CoderAgent,
    ReviewerAgent,
    RevisionAgent,
    WriterAgent,
)
from harness.agents.executor import ExecutorAgent
from harness.agents.documenter import DocumenterAgent
from harness.skills import (
    CitationFormatSkill,
    CodeReviewSkill,
    DependencyCheckSkill,
    ExperimentTrackerSkill,
    LaTeXCompileSkill,
    PaperSummarySkill,
    PlotGenerationSkill,
    TestGenerationSkill,
)


# ------------------------------------------------------------------
# 日志配置
# ------------------------------------------------------------------

def setup_logging(level: str = "INFO", log_file: str = "harness.log") -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format=fmt, handlers=handlers)


# ------------------------------------------------------------------
# 配置加载
# ------------------------------------------------------------------

def load_config(config_path: str = "configs/default.yaml") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ------------------------------------------------------------------
# Agent 注册表
# ------------------------------------------------------------------

def build_agent_registry(config: dict, memory: MemoryStore) -> dict:
    """根据配置实例化所有 agent，返回 {name: agent} 字典。"""
    api_key = config["anthropic"].get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    agent_cfg = config.get("agents", {})

    def make(cls, name: str, **extra_kwargs):
        cfg = agent_cfg.get(name, {})
        kwargs = {
            "memory": memory,
            "api_key": api_key,
            "use_extended_thinking": cfg.get("use_extended_thinking", False),
            "thinking_budget": cfg.get("thinking_budget", 5000),
        }
        kwargs.update(extra_kwargs)
        return cls(**kwargs)

    return {
        "planner":    make(PlannerAgent,    "planner"),
        "literature": make(LiteratureAgent, "literature"),
        "method":     make(MethodAgent,     "method"),
        "coder":      make(CoderAgent,      "coder"),
        "reviewer":   make(ReviewerAgent,   "reviewer"),
        "revision":   make(RevisionAgent,   "revision"),
        "writer":     make(WriterAgent,     "writer"),
        "executor":   make(ExecutorAgent,   "executor", timeout=600),
        "documenter": make(DocumenterAgent, "documenter"),
    }


def setup_skills(skills_config: dict | None = None) -> None:
    """根据配置注册 skills 到全局注册表。仅注册 enabled=true 的 skill。"""
    # 加载 skills 配置（如果未传入，尝试从 configs/skills.yaml 加载）
    if skills_config is None:
        skills_yaml = Path(__file__).parent / "configs" / "skills.yaml"
        if skills_yaml.exists():
            with open(skills_yaml, "r", encoding="utf-8") as f:
                skills_config = yaml.safe_load(f)
        else:
            skills_config = {}

    skill_cfg = skills_config.get("skills", {}) if skills_config else {}

    # 注册所有内置 skills（按 enabled 标记过滤）
    all_skills = {
        "code_review": CodeReviewSkill(),
        "dependency_check": DependencyCheckSkill(),
        "test_generation": TestGenerationSkill(),
        "paper_summary": PaperSummarySkill(),
        "latex_compile": LaTeXCompileSkill(),
        "experiment_tracker": ExperimentTrackerSkill(),
        "citation_format": CitationFormatSkill(),
        "plot_generation": PlotGenerationSkill(),
    }

    registry = get_global_registry()
    registered = 0
    for name, skill in all_skills.items():
        if skill_cfg.get(name, {}).get("enabled", True):
            registry.register(skill)
            registered += 1
        else:
            logging.info(f"[main] skill '{name}' 已在配置中禁用，跳过注册")

    logging.info(f"[main] 已注册 {registered} 个 skills")


# ------------------------------------------------------------------
# 子命令实现
# ------------------------------------------------------------------

def cmd_run(args, config: dict) -> None:
    """启动新的研究流程（或从断点继续）。"""
    paths = config["paths"]
    memory = MemoryStore(paths["memory_dir"])
    agents = build_agent_registry(config, memory)

    # 设置 skills（按 skills.yaml 中的 enabled 标记过滤）
    setup_skills(config.get("skills"))
    skill_registry = get_global_registry()

    checkpoint = CheckpointManager(
        sessions_dir=paths["sessions_dir"],
        session_id=args.session,
    )

    # 为当前 session 添加独立的日志文件
    session_log = Path(checkpoint.session_dir) / "session.log"
    session_handler = logging.FileHandler(str(session_log), encoding="utf-8")
    session_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    session_handler.setLevel(logging.DEBUG)
    logging.root.addHandler(session_handler)
    logging.info(f"[main] Session 日志文件: {session_log}")

    # 将研究方向注入 planning 阶段的 inputs
    workflow_path = args.workflow or config["workflow"]["default"]
    engine = WorkflowEngine(workflow_path, checkpoint, agents, skill_registry)

    # 把 research_direction 注入到 planning 阶段
    # 通过在 state.metadata 中预置，再在 PlannerAgent.build_prompt 里读取
    state = checkpoint.load()
    if not state.get("workflow_name"):
        state["workflow_name"] = engine.spec.name
    if args.direction:
        state.setdefault("metadata", {})["research_direction"] = args.direction
        # 同时注入到 planning 阶段的 inputs（WorkflowEngine 会合并）
        state.setdefault("stage_inputs_override", {})["planning"] = {
            "research_direction": args.direction
        }
        checkpoint.save(state)

    resume = not args.no_resume
    final_state = engine.run(resume=resume)
    engine.status(final_state)

    # 输出论文草稿
    paper_output = checkpoint.get_stage_output(final_state, "paper_writing")
    if paper_output and not paper_output.get("parse_error"):
        out_dir = Path(paths["sessions_dir"]) / checkpoint.session_id / "output"
        out_dir.mkdir(parents=True, exist_ok=True)

        latex_path = out_dir / "paper.tex"
        latex_path.write_text(
            paper_output.get("full_paper_latex", "% 生成失败"),
            encoding="utf-8"
        )
        print(f"\n论文草稿已保存至: {latex_path}")

    # 输出代码文件
    code_output = checkpoint.get_stage_output(final_state, "coding")
    if code_output and not code_output.get("parse_error"):
        from harness.tools import write_code_files
        code_dir = Path(paths["sessions_dir"]) / checkpoint.session_id / "code"
        written = write_code_files(code_output.get("files", []), code_dir)
        if written:
            print(f"代码文件已写入: {code_dir} ({len(written)} 个文件)")

    # 输出 README.md
    doc_output = checkpoint.get_stage_output(final_state, "documentation")
    if doc_output and not doc_output.get("parse_error"):
        readme_path = Path(paths["sessions_dir"]) / checkpoint.session_id / "README.md"
        readme_path.write_text(doc_output.get("readme", ""), encoding="utf-8")
        print(f"项目文档已保存至: {readme_path}")


def cmd_reset_stage(args, config: dict) -> None:
    """将指定阶段重置为待执行，配合 resume 重跑。"""
    paths = config["paths"]
    checkpoint = CheckpointManager(
        sessions_dir=paths["sessions_dir"],
        session_id=args.session,
    )
    state = checkpoint.load()
    if not state.get("workflow_name"):
        print(f"Session '{args.session}' 不存在。")
        return

    stages = args.stages
    for stage_id in stages:
        if stage_id not in state["stages"] and stage_id not in state["completed_stages"]:
            print(f"  ? [{stage_id}] 不存在，跳过")
            continue
        checkpoint.reset_stage(state, stage_id)
        print(f"  ✓ [{stage_id}] 已重置")

    print(f"\n已重置 {len(stages)} 个阶段。运行以下命令重跑：")
    print(f"  python main.py resume --session {args.session}")


def cmd_repair(args, config: dict) -> None:
    """对已有 session 中 parse_error 的阶段重新解析，不重新调用 API。"""
    paths = config["paths"]
    checkpoint = CheckpointManager(
        sessions_dir=paths["sessions_dir"],
        session_id=args.session,
    )
    state = checkpoint.load()
    if not state.get("workflow_name"):
        print(f"Session '{args.session}' 不存在。")
        return

    # 构建 agent 注册表（只用 _parse_json，不调用 API）
    memory = MemoryStore(paths["memory_dir"])
    agents = build_agent_registry(config, memory)
    agent_map = {
        "planning":     agents["planner"],
        "literature":   agents["literature"],
        "method_design":  agents["method"],
        "coding":         agents["coder"],
        "self_review":    agents["reviewer"],
        "revision":       agents["revision"],
        "paper_writing":  agents["writer"],
        "code_execution": agents["executor"],
        "documentation":  agents["documenter"],
    }

    repaired = []
    for stage_id, stage_data in state.get("stages", {}).items():
        output = stage_data.get("output", {})
        if not isinstance(output, dict) or not output.get("parse_error"):
            continue
        raw = output.get("raw", "")
        if not raw:
            continue
        agent = agent_map.get(stage_id)
        if agent is None:
            continue
        fixed = agent._parse_json(raw)
        if not fixed.get("parse_error"):
            state["stages"][stage_id]["output"] = fixed
            repaired.append(stage_id)
            print(f"  ✓ [{stage_id}] 解析修复成功，字段: {list(fixed.keys())[:6]}")
        else:
            print(f"  ✗ [{stage_id}] 仍然解析失败")

    if repaired:
        checkpoint.save(state)
        print(f"\n已修复 {len(repaired)} 个阶段，checkpoint 已更新。")

        # 写出产物
        paper_output = checkpoint.get_stage_output(state, "paper_writing")
        if paper_output and not paper_output.get("parse_error"):
            out_dir = Path(paths["sessions_dir"]) / checkpoint.session_id / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            latex_path = out_dir / "paper.tex"
            latex_path.write_text(
                paper_output.get("full_paper_latex", "% 生成失败"),
                encoding="utf-8",
            )
            print(f"论文草稿已保存至: {latex_path}")

        code_output = checkpoint.get_stage_output(state, "coding")
        if code_output and not code_output.get("parse_error"):
            from harness.tools import write_code_files
            code_dir = Path(paths["sessions_dir"]) / checkpoint.session_id / "code"
            written = write_code_files(code_output.get("files", []), code_dir)
            if written:
                print(f"代码文件已写入: {code_dir} ({len(written)} 个文件)")
    else:
        print("没有需要修复的阶段。")


def cmd_resume(args, config: dict) -> None:
    """从指定 session 的断点继续。"""
    args.no_resume = False
    args.direction = None
    args.workflow = None
    cmd_run(args, config)


def cmd_status(args, config: dict) -> None:
    """查看指定 session 的进度。"""
    paths = config["paths"]
    checkpoint = CheckpointManager(
        sessions_dir=paths["sessions_dir"],
        session_id=args.session,
    )
    state = checkpoint.load()
    if not state.get("workflow_name"):
        print(f"Session '{args.session}' 不存在或尚未初始化。")
        return

    workflow_path = config["workflow"]["default"]
    # 仅用于打印状态，不需要真实 agents
    engine = WorkflowEngine(workflow_path, checkpoint, agent_registry={})
    engine.status(state)


def cmd_list(args, config: dict) -> None:
    """列出所有 session。"""
    paths = config["paths"]
    checkpoint = CheckpointManager(sessions_dir=paths["sessions_dir"])
    sessions = checkpoint.list_sessions()
    if not sessions:
        print("暂无 session 记录。")
        return
    print(f"\n{'Session ID':<35} {'工作流':<25} {'当前阶段':<20} {'更新时间'}")
    print("-" * 100)
    for s in sessions:
        print(f"{s['session_id']:<35} {(s['workflow'] or ''):<25} "
              f"{(s['current_stage'] or ''):<20} {s['updated_at'] or ''}")
    print()


# ------------------------------------------------------------------
# CLI 入口
# ------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="research-harness — 面向长流程科研的智能体框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="configs/default.yaml", help="配置文件路径")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = subparsers.add_parser("run", help="启动新的研究流程")
    p_run.add_argument("--direction", required=True, help="研究方向（自然语言描述）")
    p_run.add_argument("--session", default=None, help="指定 session 名称（默认自动生成）")
    p_run.add_argument("--workflow", default=None, help="工作流 YAML 路径")
    p_run.add_argument("--no-resume", action="store_true", help="强制全新开始，忽略已有 checkpoint")

    # resume
    p_resume = subparsers.add_parser("resume", help="从断点继续")
    p_resume.add_argument("--session", required=True, help="要继续的 session ID")
    p_resume.add_argument("--workflow", default=None, help="工作流 YAML 路径")

    # status
    p_status = subparsers.add_parser("status", help="查看 session 进度")
    p_status.add_argument("--session", required=True, help="session ID")

    # repair
    p_repair = subparsers.add_parser("repair", help="重新解析 parse_error 阶段（不重新调用 API）")
    p_repair.add_argument("--session", required=True, help="session ID")

    # reset-stage
    p_reset = subparsers.add_parser("reset-stage", help="重置指定阶段，配合 resume 重跑")
    p_reset.add_argument("--session", required=True, help="session ID")
    p_reset.add_argument("stages", nargs="+", help="要重置的阶段 ID（可多个，空格分隔）")

    # list
    subparsers.add_parser("list", help="列出所有 session")

    args = parser.parse_args()
    config = load_config(args.config)
    setup_logging(
        level=config.get("logging", {}).get("level", "INFO"),
        log_file=config.get("logging", {}).get("file", "harness.log"),
    )

    dispatch = {
        "run": cmd_run,
        "resume": cmd_resume,
        "repair": cmd_repair,
        "reset-stage": cmd_reset_stage,
        "status": cmd_status,
        "list": cmd_list,
    }
    dispatch[args.command](args, config)


if __name__ == "__main__":
    main()
