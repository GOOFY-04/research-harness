#!/usr/bin/env python3
"""
验证 research-harness 新功能是否正常工作
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_imports():
    """测试所有新模块是否能正常导入"""
    print("=" * 60)
    print("测试 1: 模块导入")
    print("=" * 60)

    try:
        from harness.core.skill import Skill, SkillRegistry, get_global_registry
        print("✓ harness.core.skill 导入成功")

        from harness.agents.executor import ExecutorAgent
        print("✓ harness.agents.executor 导入成功")

        from harness.agents.documenter import DocumenterAgent
        print("✓ harness.agents.documenter 导入成功")

        from harness.skills import CodeReviewSkill, DependencyCheckSkill, TestGenerationSkill
        print("✓ harness.skills 导入成功")

        return True
    except Exception as e:
        print(f"✗ 导入失败: {e}")
        return False


def test_skill_registry():
    """测试 Skill 注册表"""
    print("\n" + "=" * 60)
    print("测试 2: Skill 注册表")
    print("=" * 60)

    try:
        from harness.core.skill import get_global_registry
        from harness.skills import CodeReviewSkill, DependencyCheckSkill, TestGenerationSkill

        registry = get_global_registry()

        # 注册 skills
        registry.register(CodeReviewSkill())
        registry.register(DependencyCheckSkill())
        registry.register(TestGenerationSkill())

        # 列出 skills
        skills = registry.list_skills()
        print(f"✓ 已注册 {len(skills)} 个 skills:")
        for skill in skills:
            print(f"  - {skill['name']}: {skill['description'][:50]}...")

        # 测试获取 skill
        skill = registry.get("code_review")
        if skill:
            print(f"✓ 成功获取 skill: {skill.name}")
        else:
            print("✗ 获取 skill 失败")
            return False

        return True
    except Exception as e:
        print(f"✗ Skill 注册表测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_dependency_check_skill():
    """测试依赖检查 skill"""
    print("\n" + "=" * 60)
    print("测试 3: DependencyCheckSkill")
    print("=" * 60)

    try:
        from harness.core.skill import get_global_registry

        registry = get_global_registry()

        # 调用 skill
        result = registry.execute("dependency_check", {
            "dependencies": "torch==2.0.0\nnumpy>=1.20.0\npandas"
        })

        if result.get("success"):
            print(f"✓ 依赖检查成功")
            print(f"  总包数: {result.get('total_packages', 0)}")
            print(f"  未安装: {len(result.get('not_found', []))}")
            print(f"  总结: {result.get('summary', '')}")
        else:
            print(f"✗ 依赖检查失败: {result.get('error', '未知错误')}")
            return False

        return True
    except Exception as e:
        print(f"✗ DependencyCheckSkill 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_workflow_integration():
    """测试工作流集成"""
    print("\n" + "=" * 60)
    print("测试 4: 工作流集成")
    print("=" * 60)

    try:
        from harness.core.workflow import WorkflowEngine
        from harness.core.checkpoint import CheckpointManager
        from harness.core.skill import get_global_registry

        # 检查工作流定义
        workflow_path = Path(__file__).parent.parent / "workflows" / "research.yaml"
        if not workflow_path.exists():
            print(f"✗ 工作流文件不存在: {workflow_path}")
            return False

        print(f"✓ 工作流文件存在: {workflow_path}")

        # 读取工作流定义
        import yaml
        with open(workflow_path, "r", encoding="utf-8") as f:
            workflow_def = yaml.safe_load(f)

        stages = workflow_def.get("stages", [])
        stage_ids = [s["id"] for s in stages]

        print(f"✓ 工作流包含 {len(stages)} 个阶段:")
        for stage_id in stage_ids:
            marker = "⭐" if stage_id in ["code_execution", "documentation"] else " "
            print(f"  {marker} {stage_id}")

        # 检查新阶段是否存在
        if "code_execution" in stage_ids and "documentation" in stage_ids:
            print("✓ 新阶段已添加到工作流")
        else:
            print("✗ 新阶段未添加到工作流")
            return False

        return True
    except Exception as e:
        print(f"✗ 工作流集成测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_agent_instantiation():
    """测试 Agent 实例化"""
    print("\n" + "=" * 60)
    print("测试 5: Agent 实例化")
    print("=" * 60)

    try:
        from harness.agents.executor import ExecutorAgent
        from harness.agents.documenter import DocumenterAgent

        # 实例化 ExecutorAgent
        executor = ExecutorAgent(timeout=600, enable_code_review=False)
        print(f"✓ ExecutorAgent 实例化成功 (timeout={executor.timeout})")

        # 实例化 DocumenterAgent
        documenter = DocumenterAgent()
        print(f"✓ DocumenterAgent 实例化成功")

        return True
    except Exception as e:
        print(f"✗ Agent 实例化失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """运行所有测试"""
    print("\n🔍 research-harness 功能验证\n")

    tests = [
        ("模块导入", test_imports),
        ("Skill 注册表", test_skill_registry),
        ("DependencyCheckSkill", test_dependency_check_skill),
        ("工作流集成", test_workflow_integration),
        ("Agent 实例化", test_agent_instantiation),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n✗ 测试 '{name}' 异常: {e}")
            results.append((name, False))

    # 打印总结
    print("\n" + "=" * 60)
    print("测试总结")
    print("=" * 60)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "✓ 通过" if result else "✗ 失败"
        print(f"{status}: {name}")

    print(f"\n总计: {passed}/{total} 测试通过")

    if passed == total:
        print("\n🎉 所有测试通过！新功能已就绪。")
        return 0
    else:
        print(f"\n⚠️  {total - passed} 个测试失败，请检查。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
