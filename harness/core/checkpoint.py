"""
CheckpointManager — 断点持久化

每个 session 的状态存储在 sessions/<session_id>/checkpoint.json
支持从任意阶段恢复，崩溃不丢进度。
"""

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class CheckpointManager:
    def __init__(self, sessions_dir: str | Path, session_id: Optional[str] = None):
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        if session_id is None:
            session_id = datetime.now().strftime("session_%Y%m%d_%H%M%S")
        self.session_id = session_id
        self.session_dir = self.sessions_dir / session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_file = self.session_dir / "checkpoint.json"

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def load(self) -> dict:
        """加载 checkpoint，不存在或损坏则返回空状态。"""
        if not self.checkpoint_file.exists():
            return self._empty_state()
        try:
            with open(self.checkpoint_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            logger = __import__("logging").getLogger(__name__)
            logger.warning(
                "[CheckpointManager] checkpoint 文件损坏，将使用空状态。"
                f"备份已保存至 {self.checkpoint_file}.bak"
            )
            try:
                shutil.copy(str(self.checkpoint_file), str(self.checkpoint_file) + ".bak")
            except IOError:
                pass
            return self._empty_state()
        if not isinstance(data, dict):
            logger = __import__("logging").getLogger(__name__)
            logger.warning("[CheckpointManager] checkpoint 格式异常（非 dict），使用空状态")
            return self._empty_state()
        return data

    def save(self, state: dict) -> None:
        """原子写入：先写临时文件再替换，防止写到一半崩溃。"""
        state["_updated_at"] = datetime.now().isoformat()
        tmp = self.checkpoint_file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        shutil.move(str(tmp), str(self.checkpoint_file))

    # ------------------------------------------------------------------
    # 阶段级操作
    # ------------------------------------------------------------------

    def mark_stage_started(self, state: dict, stage_id: str) -> dict:
        state["current_stage"] = stage_id
        state["stages"][stage_id] = {
            "status": "running",
            "started_at": datetime.now().isoformat(),
            "output": None,
            "error": None,
            "attempts": state["stages"].get(stage_id, {}).get("attempts", 0) + 1,
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

    def mark_stage_failed(self, state: dict, stage_id: str, error: str) -> dict:
        state["stages"][stage_id].update({
            "status": "failed",
            "finished_at": datetime.now().isoformat(),
            "error": error,
        })
        self.save(state)
        return state

    def reset_stage(self, state: dict, stage_id: str) -> dict:
        """将某个阶段重置为待执行状态，使 resume 时可以重跑。"""
        if stage_id in state["completed_stages"]:
            state["completed_stages"].remove(stage_id)
        state["stages"].pop(stage_id, None)
        self.save(state)
        return state

    def is_stage_done(self, state: dict, stage_id: str) -> bool:
        return stage_id in state["completed_stages"]

    def get_stage_output(self, state: dict, stage_id: str) -> Any:
        return state["stages"].get(stage_id, {}).get("output")

    # ------------------------------------------------------------------
    # Session 管理
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[dict]:
        """列出所有 session 及其状态摘要。"""
        sessions = []
        for d in sorted(self.sessions_dir.iterdir()):
            if not d.is_dir():
                continue
            cp = d / "checkpoint.json"
            if cp.exists():
                with open(cp, "r", encoding="utf-8") as f:
                    data = json.load(f)
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
            "_created_at": datetime.now().isoformat(),
            "_updated_at": None,
        }
