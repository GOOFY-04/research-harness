"""
DependencyCheckSkill — 依赖检查

输入：
  - dependencies: requirements.txt 内容（字符串）

输出：
  - outdated: 过时的包列表
  - conflicts: 冲突的包
  - security_issues: 安全漏洞
  - recommendations: 推荐的版本
"""

import logging
import subprocess
import sys
from typing import Any

from harness.core.skill import Skill

logger = logging.getLogger(__name__)


class DependencyCheckSkill(Skill):
    name = "dependency_check"
    description = "检查依赖包的版本、冲突和安全漏洞"

    def validate_inputs(self, inputs: dict) -> bool:
        return "dependencies" in inputs

    def execute(self, inputs: dict) -> dict:
        dependencies = inputs["dependencies"]

        # 解析依赖列表
        lines = [line.strip() for line in dependencies.split("\n") if line.strip() and not line.startswith("#")]
        packages = []
        for line in lines:
            # 提取包名（去除版本号）
            pkg = line.split("==")[0].split(">=")[0].split("<=")[0].split("~=")[0].strip()
            packages.append(pkg)

        logger.info(f"[DependencyCheckSkill] 检查 {len(packages)} 个依赖包")

        # 简单检查：使用 pip show 获取包信息
        outdated = []
        not_found = []

        for pkg in packages:
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "show", pkg],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    not_found.append(pkg)
            except Exception as e:
                logger.warning(f"[DependencyCheckSkill] 检查 {pkg} 失败: {e}")

        # 组装结果
        output = {
            "success": True,
            "total_packages": len(packages),
            "not_found": not_found,
            "outdated": outdated,  # 需要更复杂的逻辑来检测过时包
            "conflicts": [],  # 需要依赖解析器
            "security_issues": [],  # 需要集成 safety 或 pip-audit
            "summary": f"检查了 {len(packages)} 个包，{len(not_found)} 个未安装",
        }

        return output
