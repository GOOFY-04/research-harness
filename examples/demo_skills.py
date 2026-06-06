#!/usr/bin/env python3
"""
演示如何使用 research-harness 的 Skills 系统
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from harness.core.skill import get_global_registry
from harness.skills import CodeReviewSkill, TestGenerationSkill


def demo_code_review():
    """演示代码审查 skill"""
    print("=" * 60)
    print("演示 1: 代码审查 (code_review)")
    print("=" * 60)

    # 注册 skill
    registry = get_global_registry()
    registry.register(CodeReviewSkill())

    # 待审查的代码
    code = """
def calculate_average(numbers):
    total = 0
    for num in numbers:
        total += num
    return total / len(numbers)

def process_data(data):
    result = []
    for i in range(len(data)):
        result.append(data[i] * 2)
    return result
"""

    # 调用 skill
    result = registry.execute("code_review", {
        "code": code,
        "language": "python"
    })

    # 打印结果
    if result.get("success"):
        print(f"\n代码质量评分: {result.get('score', 0)}/100")
        print(f"\n总体评价: {result.get('summary', '')}")

        issues = result.get("issues", [])
        if issues:
            print(f"\n发现 {len(issues)} 个问题:")
            for issue in issues:
                print(f"  [{issue['severity']}] 行 {issue.get('line', '?')}: {issue['message']}")

        suggestions = result.get("suggestions", [])
        if suggestions:
            print(f"\n改进建议:")
            for i, suggestion in enumerate(suggestions, 1):
                print(f"  {i}. {suggestion}")
    else:
        print(f"审查失败: {result.get('error', '未知错误')}")


def demo_test_generation():
    """演示测试生成 skill"""
    print("\n" + "=" * 60)
    print("演示 2: 测试生成 (test_generation)")
    print("=" * 60)

    # 注册 skill
    registry = get_global_registry()
    registry.register(TestGenerationSkill())

    # 待测试的代码
    code = """
class Calculator:
    def add(self, a, b):
        return a + b

    def divide(self, a, b):
        if b == 0:
            raise ValueError("除数不能为零")
        return a / b
"""

    # 调用 skill
    result = registry.execute("test_generation", {
        "code": code,
        "test_framework": "pytest"
    })

    # 打印结果
    if result.get("success"):
        print(f"\n生成的测试代码:\n")
        print(result.get("test_code", ""))
    else:
        print(f"生成失败: {result.get('error', '未知错误')}")


def demo_list_skills():
    """列出所有可用的 skills"""
    print("\n" + "=" * 60)
    print("所有可用的 Skills")
    print("=" * 60)

    registry = get_global_registry()
    skills = registry.list_skills()

    if skills:
        for skill in skills:
            print(f"\n{skill['name']}")
            print(f"  描述: {skill['description']}")
    else:
        print("暂无已注册的 skills")


if __name__ == "__main__":
    print("\n🚀 research-harness Skills 系统演示\n")

    # 演示 1: 代码审查
    demo_code_review()

    # 演示 2: 测试生成
    demo_test_generation()

    # 列出所有 skills
    demo_list_skills()

    print("\n" + "=" * 60)
    print("演示完成！")
    print("=" * 60)
