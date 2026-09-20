"""
CheckpointManager — 断点持久化

每个 session 的状态存储在 sessions/<session_id>/checkpoint.json
支持从任意阶段恢复，崩溃不丢进度。
"""

import json
import logging
from uuid import uuid4
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from .io import atomic_json, safe_path


class CheckpointManager:
    def __init__(self, sessions_dir: str | Path, session_id: Optional[str] = None):
        self.sessions_dir = Path(sessions_dir).resolve()

        if session_id is None:
            session_id = datetime.now().strftime("session_%Y%m%d_%H%M%S_") + uuid4().hex[:8]
        if "/" in session_id or "\\" in session_id:
            raise ValueError("session_id must be a single directory name")
        self.session_id = session_id
        self.session_dir = safe_path(self.sessions_dir, session_id)
        self.checkpoint_file = self.session_dir / "checkpoint.json"

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def load(self) -> dict:
        """加载 checkpoint，不存在则返回空状态。"""
        if not self.checkpoint_file.exists():
            return self.new_state()
        with open(self.checkpoint_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        if (not isinstance(state, dict) or not isinstance(state.get("stages"), dict)
                or not isinstance(state.get("completed_stages"), list)
                or any(not isinstance(info, dict) for info in state["stages"].values())):
            raise ValueError(f"Invalid checkpoint: {self.checkpoint_file}")
        state["session_dir"] = str(self.session_dir)
        return state

    def new_state(self) -> dict:
        state = self._empty_state()
        state["session_dir"] = str(self.session_dir)
        return state

    def save(self, state: dict) -> None:
        """原子写入：先写临时文件再替换，防止写到一半崩溃。"""
        state["_updated_at"] = datetime.now().isoformat()
        atomic_json(self.checkpoint_file, state)

    # ------------------------------------------------------------------
    # 阶段级操作
    # ------------------------------------------------------------------

    def mark_stage_started(self, state: dict, stage_id: str) -> dict:
        previous = state["stages"].get(stage_id, {})
        attempt_history = list(previous.get("attempt_history", []))
        if previous.get("status") == "failed":
            attempt_history.append({
                "attempt": previous.get("attempts", 0),
                "error": previous.get("error"),
                "errors": list(previous.get("errors", [])),
                "output": previous.get("output"),
                "finished_at": previous.get("finished_at"),
            })
        state["current_stage"] = stage_id
        state["stages"][stage_id] = {
            "status": "running",
            "started_at": datetime.now().isoformat(),
            "output": None,
            "error": None,
            "attempts": previous.get("attempts", 0) + 1,
            "errors": previous.get("errors", []),
            "attempt_history": attempt_history,
        }
        self.save(state)
        return state

    def mark_stage_done(self, state: dict, stage_id: str, output: Any) -> dict:
        state["stages"][stage_id].update({
            "status": "done",
            "finished_at": datetime.now().isoformat(),
            "output": output,
        })
        if stage_id not in state["completed_stages"]:
            state["completed_stages"].append(stage_id)
        self.save(state)
        return state

    def mark_stage_failed(self, state: dict, stage_id: str, error: str, output=None) -> dict:
        state["stages"].setdefault(stage_id, {}).update({
            "status": "failed",
            "finished_at": datetime.now().isoformat(),
            "error": error,
            "output": output,
        })
        state["stages"][stage_id].setdefault("errors", []).append(error)
        if stage_id in state["completed_stages"]:
            state["completed_stages"].remove(stage_id)
        self.save(state)
        return state

    def reset_stage(self, state: dict, stage_id: str) -> dict:
        """将某个阶段重置为待执行状态，使 resume 时可以重跑。"""
        affected = {stage_id}
        graph = state.get("stage_dependencies", {})
        while True:
            expanded = affected | {sid for sid, deps in graph.items() if affected.intersection(deps)}
            if expanded == affected:
                break
            affected = expanded
        for sid in affected:
            state["stages"].pop(sid, None)
        state["completed_stages"] = [s for s in state["completed_stages"] if s not in affected]
        if state.get("current_stage") in affected:
            state["current_stage"] = None
        state["status"] = "pending"
        self.save(state)
        return state

    def is_stage_done(self, state: dict, stage_id: str) -> bool:
        return (stage_id in state["completed_stages"]
                and state["stages"].get(stage_id, {}).get("status") == "done")

    def get_stage_output(self, state: dict, stage_id: str) -> Any:
        return state["stages"].get(stage_id, {}).get("output")

    # ------------------------------------------------------------------
    # Session 管理
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[dict]:
        """列出所有 session 及其状态摘要。"""
        sessions = []
        if not self.sessions_dir.exists():
            return sessions
        for d in sorted(self.sessions_dir.iterdir()):
            if not d.is_dir():
                continue
            cp = d / "checkpoint.json"
            if cp.exists():
                try:
                    with open(cp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if not isinstance(data, dict):
                        raise ValueError("Expected JSON object")
                except (OSError, ValueError) as exc:
                    logging.warning("Skipping corrupt checkpoint %s: %s", cp, exc)
                    continue
                sessions.append({
                    "session_id": d.name,
                    "workflow": data.get("workflow_name"),
                    "current_stage": data.get("current_stage"),
                    "completed": data.get("completed_stages", []),
                    "updated_at": data.get("_updated_at"),
                })
        return sessions

    @staticmethod
    def _empty_state() -> dict:
        return {
            "workflow_name": None,
            "current_stage": None,
            "completed_stages": [],
            "stages": {},
            "metadata": {},
            "status": "pending",
            "_created_at": datetime.now().isoformat(),
            "_updated_at": None,
        }
