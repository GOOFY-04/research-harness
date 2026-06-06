"""
SkillIntegrator — 社区 skill 下载、验证、集成

功能：
  1. 从 GitHub/PyPI/HuggingFace 下载 skills
  2. 安全验证（静态代码扫描、许可证检查）
  3. 沙盒化执行
  4. 自动注册到 SkillRegistry

使用方式：
    integrator = SkillIntegrator(base_dir="skills/community")
    info = integrator.integrate(hunter_output)
"""

import ast
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 可信许可证列表
TRUSTED_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "PSF", "ISC", "CC0-1.0"}
WARN_LICENSES = {"GPL-2.0", "GPL-3.0", "AGPL-3.0", "LGPL-2.1", "LGPL-3.0"}

# 危险 AST 模式（静态扫描）
DANGEROUS_IMPORTS = {"os", "subprocess", "sys", "shutil", "socket", "ctypes", "importlib", "pickle", "eval", "exec", "compile", "__import__"}
DANGEROUS_CALLS = {"exec", "eval", "compile", "open", "__import__", "execfile", "input"}

# 下载/执行超时（秒）
DOWNLOAD_TIMEOUT = 120
EXECUTION_TIMEOUT = 60
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


class SkillIntegrator:
    """下载、验证和集成社区 skills。"""

    def __init__(self, base_dir: str = "skills/community"):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._registry = None  # 延迟绑定
        self._installed = self._load_installed_manifest()

    # ------------------------------------------------------------------
    # 核心流程
    # ------------------------------------------------------------------

    def integrate(self, hunter_output: dict) -> dict:
        """
        根据 SkillHunter 的输出完成 skill 集成。

        Args:
            hunter_output: SkillHunterAgent 的输出

        Returns:
            {"success": bool, "skill_name": str, "message": str, "path": str}
        """
        recommendation = hunter_output.get("recommendation", {})
        candidate_sources = hunter_output.get("candidate_sources", [])

        if not recommendation and not candidate_sources:
            return {"success": False, "message": "SkillHunter 未返回可用的 skill 来源"}

        # 逐一尝试候选项
        for candidate in candidate_sources + ([recommendation] if recommendation else []):
            source_type = candidate.get("type", "")
            source_name = candidate.get("name", "")
            source_url = candidate.get("url", "")

            if not source_url and not source_name:
                continue

            logger.info(f"[SkillIntegrator] 尝试下载: {source_name} ({source_type})")

            try:
                if source_type == "github":
                    result = self._integrate_github(source_url, source_name)
                elif source_type == "pypi":
                    result = self._integrate_pypi(source_name)
                elif source_type == "huggingface":
                    result = self._integrate_huggingface(source_url or source_name, source_name)
                else:
                    result = self._integrate_generic(source_url, source_name)

                if result["success"]:
                    # 验证安全性
                    safety = self.validate_safety(result["path"])
                    if safety["risk"] == "block":
                        logger.warning(f"[SkillIntegrator] 安全校验拦截: {safety['reasons']}")
                        shutil.rmtree(result["path"], ignore_errors=True)
                        continue

                    if safety["risk"] == "warn":
                        logger.warning(f"[SkillIntegrator] 安全警告: {safety['reasons']}")

                    # 注册
                    skill_name = self._register_skill(result["path"], source_name)
                    self._update_manifest(skill_name, {
                        "name": skill_name,
                        "source": source_type,
                        "source_name": source_name,
                        "url": source_url,
                        "path": str(result["path"]),
                        "safety_risk": safety["risk"],
                        "license": safety.get("license", "unknown"),
                    })
                    logger.info(f"[SkillIntegrator] skill 集成成功: {skill_name}")
                    return {"success": True, "skill_name": skill_name,
                            "message": f"已集成 {skill_name}", "path": str(result["path"])}

            except Exception as e:
                logger.error(f"[SkillIntegrator] 下载失败 {source_name}: {e}")
                continue

        return {"success": False, "message": "所有来源均集成失败"}

    def remove(self, skill_name: str) -> dict:
        """移除已安装的社区 skill。"""
        if skill_name not in self._installed:
            return {"success": False, "message": f"skill '{skill_name}' 未安装"}
        info = self._installed[skill_name]
        shutil.rmtree(info["path"], ignore_errors=True)
        del self._installed[skill_name]
        self._save_manifest()
        if self._registry:
            self._registry.unregister(skill_name)
        return {"success": True, "message": f"已移除 {skill_name}"}

    def list_installed(self) -> dict[str, dict]:
        """列出已安装的社区 skills。"""
        return dict(self._installed)

    # ------------------------------------------------------------------
    # 下载方法
    # ------------------------------------------------------------------

    def _integrate_github(self, url: str, name: str) -> dict:
        """从 GitHub 仓库下载 skill。支持完整 repo 或单文件。"""
        target_dir = self.base_dir / self._safe_name(name)

        # 检测是否是单文件 URL（blob）
        if "/blob/" in url:
            raw_url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
            target_dir.mkdir(parents=True, exist_ok=True)
            file_name = url.rsplit("/", 1)[-1]
            file_path = target_dir / file_name
            self._download_file(raw_url, file_path)
            return {"success": True, "path": str(target_dir)}

        # 完整仓库 — git clone (shallow)
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", url, str(target_dir)],
                capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT, check=True
            )
        except subprocess.CalledProcessError as e:
            # 如果 git clone 失败，尝试下载 zip
            logger.warning(f"git clone 失败，尝试下载 zip: {e}")
            return self._download_github_zip(url, name)

        return {"success": True, "path": str(target_dir)}

    def _download_github_zip(self, url: str, name: str) -> dict:
        """通过 zip 包下载 GitHub 仓库。"""
        target_dir = self.base_dir / self._safe_name(name)
        if target_dir.exists():
            shutil.rmtree(target_dir)

        repo_path = url.replace("https://github.com/", "").rstrip("/")
        zip_url = f"https://api.github.com/repos/{repo_path}/zipball/HEAD"

        zip_path = target_dir.with_suffix(".zip")
        try:
            self._download_file(zip_url, zip_path, headers={"Accept": "application/vnd.github.v3+json"})
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(target_dir)
            zip_path.unlink()

            # 如果解压后只有一个子目录，将其内容移到 target_dir
            entries = list(target_dir.iterdir())
            if len(entries) == 1 and entries[0].is_dir():
                inner = entries[0]
                for item in inner.iterdir():
                    shutil.move(str(item), str(target_dir / item.name))
                inner.rmdir()

            return {"success": True, "path": str(target_dir)}
        except Exception as e:
            return {"success": False, "path": str(target_dir), "error": str(e)}

    def _integrate_pypi(self, name: str) -> dict:
        """从 PyPI 安装 Python 包作为 skill。"""
        target_dir = self.base_dir / self._safe_name(name)
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", name, "--target", str(target_dir), "--no-deps"],
                capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT, check=True
            )
        except subprocess.CalledProcessError as e:
            # 某些包可能依赖没一起下载，再试带依赖的版本
            logger.warning(f"pip install --no-deps 失败，重试带依赖: {e}")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", name, "--target", str(target_dir)],
                    capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT, check=True
                )
            except subprocess.CalledProcessError as e2:
                return {"success": False, "path": str(target_dir), "error": str(e2)}

        # 创建元信息文件
        meta = {"name": name, "source": "pypi", "installed_at": target_dir.name}
        (target_dir / "skill_meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return {"success": True, "path": str(target_dir)}

    def _integrate_huggingface(self, model_id_or_url: str, name: str) -> dict:
        """从 HuggingFace 下载模型或工具。"""
        target_dir = self.base_dir / self._safe_name(name)
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        # 提取 model_id
        model_id = model_id_or_url.replace("https://huggingface.co/", "").rstrip("/")
        if model_id.endswith("/tree/main"):
            model_id = model_id[:-10]

        try:
            from huggingface_hub import snapshot_download
            snapshot_download(
                repo_id=model_id,
                local_dir=str(target_dir),
                local_dir_use_symlinks=False,
                # 不下载大模型权重，只下载工具代码
                ignore_patterns=["*.bin", "*.pt", "*.pth", "*.ckpt", "*.safetensors", "*.h5", "*.onnx"],
            )
        except ImportError:
            logger.warning("huggingface_hub 未安装，改用 HTTP 下载")
            self._download_hf_http(model_id, target_dir)
        except Exception as e:
            return {"success": False, "path": str(target_dir), "error": str(e)}

        return {"success": True, "path": str(target_dir)}

    def _download_hf_http(self, model_id: str, target_dir: Path) -> None:
        """备选方案：通过 hf API 直接下载文件列表。"""
        api_url = f"https://huggingface.co/api/models/{model_id}"
        req = urllib.request.Request(api_url, headers={"User-Agent": "research-harness/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
                info = json.loads(resp.read().decode())
        except Exception as e:
            logger.error(f"HuggingFace API 请求失败: {e}")
            return

        for sibling in info.get("siblings", []):
            fname = sibling.get("rfilename", "")
            if any(fname.endswith(ext) for ext in [".bin", ".pt", ".pth", ".ckpt", ".safetensors"]):
                continue
            file_url = f"https://huggingface.co/{model_id}/resolve/main/{fname}"
            file_path = target_dir / fname
            file_path.parent.mkdir(parents=True, exist_ok=True)
            self._download_file(file_url, file_path)

    def _integrate_generic(self, url: str, name: str) -> dict:
        """通用下载：通过 URL 直接下载文件。"""
        target_dir = self.base_dir / self._safe_name(name)
        target_dir.mkdir(parents=True, exist_ok=True)

        file_name = url.rsplit("/", 1)[-1] or "skill.py"
        file_path = target_dir / file_name
        self._download_file(url, file_path)
        return {"success": True, "path": str(target_dir)}

    # ------------------------------------------------------------------
    # 安全验证
    # ------------------------------------------------------------------

    def validate_safety(self, path: str) -> dict:
        """
        对下载的 skill 进行静态安全分析。

        Returns:
            {"risk": "ok"|"warn"|"block", "reasons": [...], "license": str}
        """
        risk = "ok"
        reasons = []
        license_found = "unknown"
        code_files = list(Path(path).rglob("*.py"))

        # 文件大小检查
        for f in code_files:
            if f.stat().st_size > MAX_FILE_SIZE:
                risk = "block"
                reasons.append(f"文件 {f.name} 超过大小限制 ({f.stat().st_size / 1024 / 1024:.1f}MB)")

        # 静态 AST 扫描
        for f in code_files[:50]:  # 限制扫描数量
            try:
                source = f.read_text(encoding="utf-8", errors="ignore")
                tree = ast.parse(source)
                dangerous = self._scan_ast(tree)
                if dangerous:
                    risk = max(risk, "warn", key=lambda x: {"ok": 0, "warn": 1, "block": 2}.get(x, 0))
                    reasons.append(f"{f.name} 包含潜在危险调用: {dangerous}")
            except SyntaxError:
                pass
            except Exception:
                pass

        # 许可证检查
        license_found = self._detect_license(path)
        if license_found == "unknown":
            risk = max(risk, "warn", key={"ok": 0, "warn": 1, "block": 2}.__getitem__)
            reasons.append("未检测到许可证")
        elif license_found in WARN_LICENSES:
            risk = max(risk, "warn", key={"ok": 0, "warn": 1, "block": 2}.__getitem__)
            reasons.append(f"许可证 {license_found} 可能有兼容性问题")

        return {"risk": risk, "reasons": reasons, "license": license_found}

    @staticmethod
    def _scan_ast(tree: ast.AST) -> list[str]:
        """扫描 AST 中的危险模式。"""
        dangerous = []

        class DangerVisitor(ast.NodeVisitor):
            def visit_Import(self, node):  # noqa: N802
                for alias in node.names:
                    if alias.name.split(".")[0] in DANGEROUS_IMPORTS:
                        dangerous.append(f"import {alias.name}")

            def visit_ImportFrom(self, node):  # noqa: N802
                if node.module and node.module.split(".")[0] in DANGEROUS_IMPORTS:
                    dangerous.append(f"from {node.module} import ...")

            def visit_Call(self, node):  # noqa: N802
                if isinstance(node.func, ast.Name) and node.func.id in DANGEROUS_CALLS:
                    dangerous.append(f"call {node.func.id}()")

        DangerVisitor().visit(tree)
        return dangerous

    @staticmethod
    def _detect_license(path: str) -> str:
        """检测项目根目录的许可证文件。"""
        root = Path(path)
        for filename in ["LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "COPYING.md",
                          "LICENCE", "LICENCE.md", "setup.cfg", "pyproject.toml"]:
            candidate = root / filename
            if not candidate.exists():
                candidate = next(root.rglob(filename), None)
            if candidate and candidate.is_file():
                content = candidate.read_text(encoding="utf-8", errors="ignore")[:2000].lower()
                for lic in sorted([*TRUSTED_LICENSES, *WARN_LICENSES], key=len, reverse=True):
                    if lic.lower() in content:
                        return lic
                return "custom"
        return "unknown"

    # ------------------------------------------------------------------
    # 沙盒执行
    # ------------------------------------------------------------------

    def sandbox_execute(self, skill_path: str, entry_function: str, inputs: dict) -> dict:
        """
        在隔离子进程中执行下载的 skill。

        Args:
            skill_path: skill 代码路径
            entry_function: 入口函数名（如 "execute"）
            inputs: 输入参数

        Returns:
            执行结果字典
        """
        wrapper_code = f'''
import json
import sys
import traceback
from pathlib import PurePosixPath as Path

sys.path.insert(0, {json.dumps(skill_path)})

# 受限内建（加强安全性）
_orig_import = __import__
def _safe_import(name, *args, **kwargs):
    forbidden = {{"socket", "ctypes", "code", "multiprocessing"}}
    if name.split(".")[0] in forbidden:
        raise ImportError(f"import {{name}} is not allowed in skill sandbox")
    return _orig_import(name, *args, **kwargs)

__builtins__["__import__"] = _safe_import

try:
    import skill as _skill
    result = getattr(_skill, {json.dumps(entry_function)})({json.dumps(inputs)})
    print("SANDBOX_RESULT_START")
    print(json.dumps({{"success": True, "result": result}}))
    print("SANDBOX_RESULT_END")
except Exception as e:
    traceback.print_exc()
    print("SANDBOX_RESULT_START")
    print(json.dumps({{"success": False, "error": str(e)}}))
    print("SANDBOX_RESULT_END")
'''

        # 如果 skill 不是单个 skill.py 文件，创建临时 wrapper
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(wrapper_code)
            wrapper_path = f.name

        # 如果 skill_path 下有一个名为 __init__.py 或带 entry 包装的文件，设为 package
        env = os.environ.copy()
        env["PYTHONPATH"] = skill_path

        try:
            proc = subprocess.run(
                [sys.executable, wrapper_path, skill_path],
                capture_output=True, text=True, timeout=EXECUTION_TIMEOUT,
                env=env,
                cwd=skill_path,
            )
            stdout = proc.stdout
            # 提取标记之间的 JSON
            match = re.search(r"SANDBOX_RESULT_START\s*\n(.*?)\nSANDBOX_RESULT_END", stdout, re.DOTALL)
            if match:
                return json.loads(match.group(1))
            return {"success": False, "error": f"执行输出格式异常: {stdout[:500]}", "stderr": proc.stderr[:500]}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"skill 执行超时 ({EXECUTION_TIMEOUT}s)"}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            Path(wrapper_path).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _register_skill(self, path: str, source_name: str) -> str:
        """将下载的代码注册为 skill。"""
        skill_name = self._safe_name(source_name)
        # 如果 SkillRegistry 可用，创建一个动态 skill wrapper
        if self._registry is None:
            from harness.core.skill import get_global_registry
            self._registry = get_global_registry()

        dynamic_skill = CommunitySkill(
            name=skill_name,
            description=f"社区 skill (来自 {source_name})",
            path=path,
        )
        self._registry.register(dynamic_skill)
        return skill_name

    @staticmethod
    def _safe_name(raw: str) -> str:
        """生成安全的 skill 名称。"""
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", raw.lower())
        safe = re.sub(r"_+", "_", safe).strip("_")
        return safe[:64] or "unnamed_skill"

    @staticmethod
    def _download_file(url: str, file_path: Path, headers: dict | None = None) -> None:
        """从 URL 下载文件，带超时和 User-Agent。"""
        default_headers = {"User-Agent": "research-harness/1.0"}
        if headers:
            default_headers.update(headers)
        req = urllib.request.Request(url, headers=default_headers)
        try:
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_bytes(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} downloading {url}")

    def _update_manifest(self, skill_name: str, info: dict) -> None:
        self._installed[skill_name] = info
        self._save_manifest()

    def _save_manifest(self) -> None:
        manifest_path = self.base_dir / "manifest.json"
        manifest_path.write_text(json.dumps(self._installed, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_installed_manifest(self) -> dict:
        manifest_path = self.base_dir / "manifest.json"
        if manifest_path.exists():
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        return {}


class CommunitySkill:
    """动态加载的社区 skill wrapper。"""

    def __init__(self, name: str, description: str, path: str):
        self.name = name
        self.description = description
        self.path = path
        self.validate_inputs = lambda inputs: True  # 默认通过

    def execute(self, inputs: dict) -> dict:
        """在沙盒中执行社区 skill。"""
        # 查找入口模块
        path_dir = Path(self.path)
        entry_file = None
        for candidate in ["skill.py", "main.py", "__init__.py", "run.py"]:
            if (path_dir / candidate).exists():
                entry_file = candidate.replace(".py", "")
                break
        if entry_file is None:
            # 找任意 .py 文件
            py_files = list(path_dir.glob("*.py"))
            if py_files:
                entry_file = py_files[0].stem
            else:
                return {"success": False, "error": f"在 {self.path} 中未找到入口 .py 文件"}

        integrator = SkillIntegrator()
        return integrator.sandbox_execute(self.path, "execute", inputs)


def auto_resolve_failure(
    stage_id: str,
    error_msg: str,
    task_description: str,
    existing_skills: list,
) -> Optional[dict]:
    """
    自动解决失败：调用 SkillHunter → SkillIntegrator 全流程。

    这是 WorkflowEngine 的自动触发入口。
    在阶段失败后自动调用，尝试从社区获取解决方案。

    Returns:
        如果成功集成新的 skill，返回 skill 信息；否则返回 None
    """
    try:
        from harness.agents.skill_hunter import SkillHunterAgent
        hunter = SkillHunterAgent()
        hunter_inputs = {
            "error_msg": error_msg,
            "task_description": task_description,
            "existing_skills": existing_skills,
        }
        hunter_result = hunter.run(
            stage_id=f"skill_hunt_{stage_id}",
            inputs=hunter_inputs,
            state={},
        )
        # 异步运行时的结果可能来自 run() 输出
        if isinstance(hunter_result, str):
            try:
                # 流式响应已经完成，hunter_result 是一个字符串
                import json
                hunter_result = json.loads(hunter_result)
            except (json.JSONDecodeError, TypeError):
                raw = hunter_result
                # 尝试提取 JSON
                import re
                match = re.search(r'\{[\s\S]*}', hunter_result)
                if match:
                    try:
                        hunter_result = json.loads(match.group())
                    except json.JSONDecodeError:
                        logger.error(f"无法解析 SkillHunter 输出: {raw[:500]}")
                        return None
                else:
                    logger.error(f"SkillHunter 输出不是 JSON: {raw[:500]}")
                    return None

        if isinstance(hunter_result, dict):
            pass  # 直接使用
        else:
            logger.error(f"SkillHunter 返回了非预期的类型: {type(hunter_result)}")
            return None

        recommendation = hunter_result.get("recommendation", {})
        if not recommendation:
            return None

        integrator = SkillIntegrator()
        result = integrator.integrate(hunter_result)

        if result["success"]:
            logger.info(f"[auto_resolve] skill 集成成功: {result['skill_name']}")
            return result
        else:
            logger.warning(f"[auto_resolve] skill 集成失败: {result.get('message')}")
            return None

    except Exception as e:
        logger.error(f"[auto_resolve] 自动解决失败: {e}")
        return None
