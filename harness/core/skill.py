"""
Skill 系统 — 可插拔的功能模块

Skill 是比 Agent 更轻量的功能单元，可以被 Agent 调用来完成特定任务。
例如：代码审查、依赖检查、测试生成、可视化等。

使用方式：
    registry = SkillRegistry()
    registry.register(CodeReviewSkill())
    result = registry.execute("code_review", inputs={"code": "..."})
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

logger = logging.getLogger(__name__)


class Skill(ABC):
    """
    Skill 基类。

    子类需要实现：
        - name: str (类属性，skill 的唯一标识)
        - description: str (类属性，功能描述)
        - execute(inputs: dict) -> dict (执行逻辑)
    """

    name: str = ""
    description: str = ""

    @abstractmethod
    def execute(self, inputs: dict) -> dict:
        """
        执行 skill 的核心逻辑。

        Args:
            inputs: 输入参数字典

        Returns:
            输出结果字典
        """
        pass

    def validate_inputs(self, inputs: dict) -> bool:
        """
        验证输入参数是否合法（子类可选实现）。

        Returns:
            True 表示合法，False 表示不合法
        """
        return True


class SkillRegistry:
    """
    Skill 注册表，管理所有可用的 skills。
    """

    def __init__(self):
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """注册一个 skill。"""
        if not skill.name:
            raise ValueError(f"Skill {skill.__class__.__name__} 必须定义 name 属性")

        if skill.name in self._skills:
            logger.warning(f"[SkillRegistry] 覆盖已存在的 skill: {skill.name}")

        self._skills[skill.name] = skill
        logger.info(f"[SkillRegistry] 注册 skill: {skill.name}")

    def unregister(self, name: str) -> None:
        """注销一个 skill。"""
        if name in self._skills:
            del self._skills[name]
            logger.info(f"[SkillRegistry] 注销 skill: {name}")

    def get(self, name: str) -> Optional[Skill]:
        """获取一个 skill。"""
        return self._skills.get(name)

    def list_skills(self) -> list[dict[str, str]]:
        """列出所有已注册的 skills。"""
        return [
            {"name": skill.name, "description": skill.description}
            for skill in self._skills.values()
        ]

    def execute(self, name: str, inputs: dict) -> dict:
        """
        执行一个 skill。

        Args:
            name: skill 名称
            inputs: 输入参数

        Returns:
            执行结果

        Raises:
            ValueError: skill 不存在或输入不合法
        """
        skill = self.get(name)
        if skill is None:
            raise ValueError(f"Skill '{name}' 未注册")

        if not skill.validate_inputs(inputs):
            raise ValueError(f"Skill '{name}' 输入参数不合法: {inputs}")

        logger.info(f"[SkillRegistry] 执行 skill: {name}")
        try:
            result = skill.execute(inputs)
            logger.info(f"[SkillRegistry] skill '{name}' 执行成功")
            return result
        except Exception as e:
            logger.error(f"[SkillRegistry] skill '{name}' 执行失败: {e}")
            return {"error": str(e), "success": False}


# 全局单例
_global_registry: Optional[SkillRegistry] = None


def get_global_registry() -> SkillRegistry:
    """获取全局 skill 注册表（单例模式）。"""
    global _global_registry
    if _global_registry is None:
        _global_registry = SkillRegistry()
    return _global_registry
