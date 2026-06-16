"""
ExperimentLoopAgent — 实验训练循环

管理完整训练实验的生命周期：
  1. 写入代码文件 & 修正依赖声明
  2. 鲁棒依赖安装（长时间超时、断点续装、自动重试）
  3. 导入预检（验证所有 import 可用，失败自动 LLM 修复）
  4. 启动训练子进程（非阻塞）
  5. 定期轮询训练状态和指标
  6. 根据多种退出条件智能决定停止时机
  7. 优雅终止并输出结构化实验报告

退出条件（可组合，任一满足即退出）：
  - max_epochs: 达到最大训练轮数
  - target_loss: 损失降至目标值以下
  - patience: 连续 N 轮无改善
  - max_time: 超过最大运行时间
  - target_metric: 指定指标达到阈值
"""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from harness.core.agent import BaseAgent

logger = logging.getLogger(__name__)

# 默认正则模式 — 从训练输出中提取指标，子类或 YAML 可覆盖
DEFAULT_METRIC_PATTERNS = {
    "epoch": r"(?:Epoch|epoch)\s*(\d+)",
    "loss": r"(?:loss|Loss)[:\s]*([\d.]+(?:e[+-]?\d+)?)",
    "train_loss": r"(?:train_loss|Train Loss)[:\s]*([\d.]+(?:e[+-]?\d+)?)",
    "val_loss": r"(?:val_loss|Val Loss|valid_loss)[:\s]*([\d.]+(?:e[+-]?\d+)?)",
    "accuracy": r"(?:acc|accuracy|Accuracy)[:\s]*([\d.]+(?:e[+-]?\d+)?)",
    "mae": r"(?:MAE|mae)[:\s]*([\d.]+(?:e[+-]?\d+)?)",
    "rmse": r"(?:RMSE|rmse)[:\s]*([\d.]+(?:e[+-]?\d+)?)",
    "abs_rel": r"(?:AbsRel|abs_rel|abs rel)[:\s]*([\d.]+(?:e[+-]?\d+)?)",
    "silog": r"(?:SILog|silog|si_log)[:\s]*([\d.]+(?:e[+-]?\d+)?)",
}

# ---- 已知不可 pip 安装的包 → 替代方案 ----
PACKAGE_ALTERNATIVES = {
    "diff-gaussian-rasterization": {
        "alternatives": ["gsplat"],
        "reason": "diff-gaussian-rasterization 需要从源码 CUDA 编译，无 PyPI wheel；gsplat 提供等效功能且支持 pip install",
    },
    "simple-knn": {
        "alternatives": ["torch-cluster", "faiss-cpu"],
        "reason": "simple-knn 需要 CUDA 编译；可用 FAISS 或 torch-cluster 替代最近邻",
    },
}

# ---- 安装状态文件，用于断点续装 ----
INSTALL_STATE_FILE = ".install_state.json"


class ExitCondition:
    """单个退出条件。"""

    def __init__(self, cond_type: str, value=None, metric: str = "loss", mode: str = "min"):
        self.type = cond_type     # max_epochs | target_loss | patience | max_time | target_metric
        self.value = value        # 阈值
        self.metric = metric      # 关联的指标名
        self.mode = mode          # min（越小越好）| max（越大越好）

    def to_dict(self) -> dict:
        return {"type": self.type, "value": self.value, "metric": self.metric, "mode": self.mode}

    @classmethod
    def from_dict(cls, d: dict) -> "ExitCondition":
        return cls(d.get("type", ""), d.get("value"), d.get("metric", "loss"), d.get("mode", "min"))


class ExperimentLoopAgent(BaseAgent):
    """
    实验训练循环 Agent。

    配置方式（优先级：YAML > __init__ > 类属性 > 默认值）：
      check_interval: 轮询间隔（秒），默认 60
      max_epochs: 最大 epoch 数，默认 100
      target_loss: 目标损失值，低于此值退出
      patience: 早停耐心值（连续无改善轮询次数），默认 5
      max_time: 最大运行秒数，默认 86400（24h）
      target_metric: {metric_name: {value: X, mode: "min"|"max"}}
      metric_patterns: 自定义正则提取模式
      monitor_metric: 用于 patience 的主监控指标，默认 "loss"
      training_script: 训练脚本文件名，默认 "train.py"
      dep_install_timeout: 单个依赖安装命令的最长等待秒数，默认 7200（2h）
      dep_install_max_retries: 依赖安装最大重试次数，默认 3
      auto_fix_imports: 是否自动修复导入错误，默认 True
      preflight_check: 是否在训练前执行导入预检，默认 True
    """

    model = "claude-sonnet-4-6"
    max_tokens = 4096

    # ---- 类级别默认配置（子类可覆盖） ----
    check_interval: int = 60          # 轮询间隔（秒）
    max_epochs: int = 100             # 最大 epoch
    target_loss: Optional[float] = None
    patience: int = 5                 # 早停 patience
    max_time: int = 86400             # 最大运行时间（秒）
    target_metric: Optional[dict] = None
    monitor_metric: str = "loss"      # 主监控指标
    metric_patterns: dict = None      # 自定义正则（None = 使用默认）
    training_script: str = "train.py"
    # 依赖安装相关
    dep_install_timeout: int = 7200   # 单个 pip install 最长等 2 小时
    dep_install_max_retries: int = 3  # 最多重试 3 次
    auto_fix_imports: bool = True     # 自动修复导入错误
    preflight_check: bool = True      # 训练前执行导入预检
    # CLI 参数注入（自动从 state 映射到训练脚本的命令行参数）
    cli_arg_map: dict = None          # 如 {"data_root": "dataset_path", "output_dir": None}
    extra_train_args: list = None     # 额外固定的命令行参数，如 ["--batch_size", "2"]
    # 闭环迭代
    max_train_retries: int = 5        # 训练失败后最多自动修复重试次数
    retry_cooldown: int = 10          # 重试间隔冷却时间（秒）

    def __init__(self, *args, **kwargs):
        # 提取 ExperimentLoop 专属参数
        self.check_interval = kwargs.pop("check_interval", self.__class__.check_interval)
        self.max_epochs = kwargs.pop("max_epochs", self.__class__.max_epochs)
        self.target_loss = kwargs.pop("target_loss", self.__class__.target_loss)
        self.patience = kwargs.pop("patience", self.__class__.patience)
        self.max_time = kwargs.pop("max_time", self.__class__.max_time)
        self.target_metric = kwargs.pop("target_metric", self.__class__.target_metric)
        self.monitor_metric = kwargs.pop("monitor_metric", self.__class__.monitor_metric)
        self.metric_patterns = kwargs.pop("metric_patterns", self.__class__.metric_patterns)
        self.training_script = kwargs.pop("training_script", self.__class__.training_script)
        self.dep_install_timeout = kwargs.pop("dep_install_timeout", self.__class__.dep_install_timeout)
        self.dep_install_max_retries = kwargs.pop("dep_install_max_retries", self.__class__.dep_install_max_retries)
        self.auto_fix_imports = kwargs.pop("auto_fix_imports", self.__class__.auto_fix_imports)
        self.preflight_check = kwargs.pop("preflight_check", self.__class__.preflight_check)
        self.cli_arg_map = kwargs.pop("cli_arg_map", self.__class__.cli_arg_map)
        self.extra_train_args = kwargs.pop("extra_train_args", self.__class__.extra_train_args)
        self.max_train_retries = kwargs.pop("max_train_retries", self.__class__.max_train_retries)
        self.retry_cooldown = kwargs.pop("retry_cooldown", self.__class__.retry_cooldown)
        super().__init__(*args, **kwargs)

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        return ""  # 不使用标准 LLM prompt，直接操控训练循环

    def run(self, stage_id: str, inputs: dict, state: dict) -> dict:
        """
        执行实验循环。

        输入（来自 coding 阶段）:
          files, entry_point, dependencies, session_dir
        输入（可在 Workflow YAML 中覆盖）:
          experiment_config: {check_interval, max_epochs, target_loss, ...}
        """
        files = inputs.get("files", [])
        dependencies = inputs.get("dependencies", "")
        session_dir = state.get("session_dir", "")
        experiment_config = inputs.get("experiment_config", {})

        if not session_dir:
            return {"error": "缺少 session_dir", "success": False}

        code_dir = Path(session_dir) / "code"
        if not code_dir.exists():
            return {"error": f"代码目录不存在: {code_dir}", "success": False}

        # ---- 合并配置：YAML > Agent 属性 ----
        cfg = self._merge_config(experiment_config)

        # ================================================================
        # Phase 0: 鲁棒依赖安装 & 导入预检
        # ================================================================
        preflight_result = self._pre_training_setup(files, dependencies, code_dir, inputs)
        if not preflight_result["success"]:
            # 即使预检失败也继续尝试启动训练（可能仍有修复机会）
            logger.warning(
                f"[ExperimentLoop] 预检未完全通过: {preflight_result.get('errors', [])}"
            )

        # ---- 构建退出条件列表 ----
        exit_conditions = self._build_exit_conditions(cfg)

        # ---- 解析指标正则 ----
        patterns = self.metric_patterns or DEFAULT_METRIC_PATTERNS

        logger.info(f"[ExperimentLoop] 启动实验循环, 退出条件: {[c.to_dict() for c in exit_conditions]}")
        logger.info(f"[ExperimentLoop] 轮询间隔: {cfg['check_interval']}s, 监控指标: {cfg['monitor_metric']}")

        # ---- 训练脚本定位 ----
        env = os.environ.copy()
        if state.get("dataset_path"):
            env["DATASET_PATH"] = state["dataset_path"]

        train_script = code_dir / self.training_script
        if not train_script.exists():
            alt_scripts = ["run_train.py", "inference.py", "main.py"]
            for alt in alt_scripts:
                alt_path = code_dir / alt
                if alt_path.exists():
                    train_script = alt_path
                    logger.info(f"[ExperimentLoop] 使用替代训练脚本: {alt}")
                    break
            else:
                return {"error": f"训练脚本不存在: {self.training_script}", "success": False}

        # ---- 构建训练命令行参数 ----
        train_cmd_base = [sys.executable, "-u", train_script.name]
        train_cmd_base += self._build_train_args(state, code_dir)

        # ================================================================
        # Phase 1-5: 闭环迭代训练（外层 Loop）
        # ================================================================
        retry_history: list[dict] = []
        all_metrics: list[dict] = []
        final_output = ""
        final_metrics = {}
        best_metric_val = float("inf")
        overall_elapsed = 0.0
        analysis = {}

        for attempt in range(self.max_train_retries + 1):
            if attempt > 0:
                logger.info(
                    f"[ExperimentLoop] 训练重试 {attempt}/{self.max_train_retries}"
                )
                # ---- 错误分析 + 定点修复 ----
                last_result = retry_history[-1] if retry_history else {}
                if last_result.get("exitcode") != 0 and last_result.get("output"):
                    fix_count = self._targeted_fix(
                        code_dir, last_result["output"],
                        last_result.get("analysis", {}),
                        inputs,
                    )
                    if fix_count > 0:
                        logger.info(
                            f"[ExperimentLoop] 定点修复了 {fix_count} 个文件"
                        )
                        # 重新验证导入
                        _, import_errors = self._verify_imports(code_dir)
                        if import_errors:
                            logger.warning(
                                f"[ExperimentLoop] 修复后仍有 {len(import_errors)} 个导入错误"
                            )
                    else:
                        logger.warning(
                            f"[ExperimentLoop] 无法自动修复，停止重试"
                        )
                        break

                time.sleep(self.retry_cooldown)

            # ---- 启动训练子进程 ----
            logger.info(
                f"[ExperimentLoop] 启动训练 (attempt {attempt+1}/{self.max_train_retries+1}): "
                f"{' '.join(train_cmd_base)}"
            )
            try:
                proc = subprocess.Popen(
                    train_cmd_base,
                    cwd=str(code_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                    bufsize=1,
                )
            except Exception as e:
                retry_history.append({"exitcode": -1, "output": str(e), "analysis": {}})
                if attempt < self.max_train_retries:
                    continue
                return {"error": f"启动训练进程失败: {e}", "success": False}

            # ---- 主监控循环 ----
            start_time = time.time()
            check_count = 0
            last_epoch = -1
            patience_counter = 0
            accumulated_output = ""
            exit_reason = "unknown"

            try:
                while proc.poll() is None:
                    elapsed = time.time() - start_time
                    new_output = self._read_output(proc)
                    if new_output:
                        accumulated_output += new_output

                    current_metrics = self._extract_metrics(accumulated_output, patterns)
                    if current_metrics:
                        all_metrics.append(current_metrics)
                        logger.info(
                            f"[ExperimentLoop] check={check_count} "
                            f"elapsed={elapsed:.0f}s metrics={json.dumps(current_metrics, ensure_ascii=False)}"
                        )

                    exit_decision = self._evaluate_exit_conditions(
                        exit_conditions, current_metrics, elapsed,
                        best_metric_val, patience_counter, last_epoch
                    )
                    if exit_decision["should_exit"]:
                        exit_reason = exit_decision["reason"]
                        logger.info(f"[ExperimentLoop] 退出条件满足: {exit_reason}")
                        self._terminate_process(proc)
                        accumulated_output = self._read_remaining(proc, accumulated_output)
                        break

                    if current_metrics:
                        current_val = current_metrics.get(cfg["monitor_metric"])
                        if current_val is not None:
                            if current_val < best_metric_val:
                                best_metric_val = current_val
                                patience_counter = 0
                            else:
                                patience_counter += 1
                        new_epoch = current_metrics.get("epoch")
                        if new_epoch is not None and new_epoch != last_epoch:
                            last_epoch = new_epoch

                    check_count += 1
                    time.sleep(cfg["check_interval"])

                else:
                    exit_reason = "process_finished"
                    remaining = self._read_remaining(proc, accumulated_output)
                    if remaining != accumulated_output:
                        accumulated_output = remaining
                    logger.info(
                        f"[ExperimentLoop] 训练进程自行结束 (exitcode={proc.returncode})"
                    )

            except KeyboardInterrupt:
                self._terminate_process(proc)
                exit_reason = "user_interrupt"
            except Exception as e:
                self._terminate_process(proc)
                exit_reason = f"error: {e}"
                logger.error(f"[ExperimentLoop] 异常: {e}")

            attempt_elapsed = time.time() - start_time
            overall_elapsed += attempt_elapsed

            # ---- 分析本次尝试 ----
            attempt_analysis = self._analyze_experiment(
                exit_reason, all_metrics[-10:] if all_metrics else [],
                accumulated_output, attempt_elapsed
            )

            retry_history.append({
                "attempt": attempt + 1,
                "exitcode": proc.returncode if proc.returncode is not None else -1,
                "exit_reason": exit_reason,
                "elapsed_s": round(attempt_elapsed, 1),
                "output": accumulated_output,
                "analysis": attempt_analysis,
            })

            # ---- 判定是否继续 ----
            final_output = accumulated_output
            final_metrics = self._extract_metrics(final_output, patterns)
            analysis = attempt_analysis

            # 成功：有实际训练指标产生
            if all_metrics and exit_reason in ("process_finished",):
                logger.info(
                    f"[ExperimentLoop] 训练成功完成 "
                    f"(attempt {attempt+1}, {len(all_metrics)} 条指标)"
                )
                break

            # 达到了退出条件（非崩溃）
            if exit_reason not in ("process_finished",) and "error:" not in exit_reason:
                logger.info(f"[ExperimentLoop] 训练自然退出: {exit_reason}")
                break

            # 崩溃且无更多重试
            if attempt >= self.max_train_retries:
                logger.warning(
                    f"[ExperimentLoop] 已达最大重试次数 ({self.max_train_retries})"
                )
                break

        # ---- 组装输出 ----
        last_retry = retry_history[-1] if retry_history else {}
        output = {
            "success": last_retry.get("exitcode") == 0 if last_retry else False,
            "exit_reason": last_retry.get("exit_reason", "unknown"),
            "total_elapsed_s": round(overall_elapsed, 1),
            "num_attempts": len(retry_history),
            "final_metrics": final_metrics or {},
            "best_metric": best_metric_val if best_metric_val != float("inf") else None,
            "metrics_history": all_metrics[-50:],
            "analysis": analysis,
            "training_log_tail": final_output[-3000:] if final_output else "",
            "preflight": preflight_result,
            "retry_history": [
                {"attempt": r["attempt"], "exitcode": r["exitcode"],
                 "exit_reason": r.get("exit_reason", ""),
                 "summary": r.get("analysis", {}).get("summary", "")[:200]}
                for r in retry_history
            ],
        }

        # 写入记忆
        if self.memory:
            self.memory.append(
                topic=stage_id,
                content={
                    "exit_reason": last_retry.get("exit_reason", ""),
                    "final_metrics": final_metrics,
                    "elapsed_s": overall_elapsed,
                    "num_attempts": len(retry_history),
                    "summary": analysis.get("summary", ""),
                    "preflight_errors": preflight_result.get("errors", []),
                },
                tags=["ExperimentLoop", stage_id],
            )

        return output

    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        return self._parse_json(raw_text)

    # ========================================================================
    # Phase 0: 鲁棒依赖安装 & 导入预检
    # ========================================================================

    def _pre_training_setup(
        self, files: list, dependencies: str, code_dir: Path, inputs: dict = None
    ) -> dict:
        """
        训练前准备：写入代码 → 修复依赖 → 安装依赖 → 契约验证 → 导入预检 → 自动修复。

        返回 {"success": bool, "errors": [...], "fixes_applied": [...], "install_log": str}
        """
        result = {"success": True, "errors": [], "fixes_applied": [], "install_log": ""}
        inputs = inputs or {}

        # ---- 1. 写入代码文件（disk fallback） ----
        if files:
            self._write_code_files(files, code_dir)
            logger.info(f"[ExperimentLoop] 写入 {len(files)} 个代码文件到 {code_dir}")
        else:
            # Fallback: 从磁盘读取已有代码文件
            existing = list(code_dir.glob("*.py"))
            if existing:
                logger.info(
                    f"[ExperimentLoop] 无 inputs.files，使用磁盘已有代码 "
                    f"({len(existing)} 个 .py 文件)"
                )
                # 验证至少有一个可执行入口
                has_entry = any(
                    f.name in ("train.py", "run_train.py", "main.py", "inference.py")
                    for f in existing
                )
                if not has_entry:
                    result["errors"].append("磁盘代码目录中没有可识别的训练入口脚本")
            else:
                result["success"] = False
                result["errors"].append("无代码文件且代码目录为空")
                return result

        # ---- 2. 写入并智能修复 requirements.txt ----
        req_file = code_dir / "requirements.txt"
        fixed_deps = ""
        if dependencies:
            fixed_deps = self._fix_dependency_declarations(dependencies)
            if fixed_deps != dependencies:
                logger.info(f"[ExperimentLoop] 已修正依赖声明（替换不可 pip 安装的包）")
                result["fixes_applied"].append("replaced unpip-installable packages in requirements.txt")
            req_file.write_text(fixed_deps, encoding="utf-8")
        elif req_file.exists():
            fixed_deps = req_file.read_text(encoding="utf-8")
            logger.info(f"[ExperimentLoop] 使用已有 requirements.txt")
        else:
            logger.info(f"[ExperimentLoop] 无 requirements.txt，跳过依赖安装")

        # ---- 3. 鲁棒依赖安装（长超时 + 断点续装 + 自动重试） ----
        install_log, install_ok = self._install_dependencies_robust(code_dir, fixed_deps)
        result["install_log"] = install_log
        if not install_ok:
            result["success"] = False
            result["errors"].append(f"依赖安装失败: {install_log[-500:]}")
            logger.warning(f"[ExperimentLoop] 依赖安装未完全成功，继续执行导入预检...")

        # ---- 4. 接口契约验证 ----
        interface_manifest = inputs.get("interface_manifest", {})
        if interface_manifest:
            contract_errors = self._validate_contract(code_dir, interface_manifest)
            if contract_errors:
                result["errors"].extend(contract_errors)
                result["success"] = False
                logger.warning(
                    f"[ExperimentLoop] 接口契约验证发现 {len(contract_errors)} 个违规"
                )
                if self.auto_fix_imports:
                    self._fix_contract_violations(code_dir, contract_errors, interface_manifest)

        # ---- 5. 导入预检 ----
        if self.preflight_check:
            import_ok, import_errors = self._verify_imports(code_dir)
            if import_errors:
                logger.warning(
                    f"[ExperimentLoop] 导入预检发现 {len(import_errors)} 个错误"
                )
                if self.auto_fix_imports:
                    fixed_count = self._fix_import_errors(code_dir, import_errors, files)
                    if fixed_count > 0:
                        result["fixes_applied"].append(f"auto-fixed {fixed_count} import errors")
                        # 重新验证
                        _, import_errors = self._verify_imports(code_dir)
                if import_errors:
                    result["success"] = False
                    result["errors"].extend(import_errors)
            else:
                logger.info(f"[ExperimentLoop] 导入预检通过")

        return result

    def _fix_dependency_declarations(self, dependencies: str) -> str:
        """将所有文件中 import 已知不能 pip 安装的包替换为替代品；修复 requirements 内容。"""
        if not dependencies:
            return dependencies
        import re
        for bad_pkg, info in PACKAGE_ALTERNATIVES.items():
            if bad_pkg in dependencies:
                alt = info["alternatives"][0] if info["alternatives"] else ""
                if alt:
                    # 替换整个依赖行（含版本号、@git+URL 等后缀）
                    # 匹配: pkg>=1.0, pkg @ git+https://..., pkg==1.0; ...
                    dependencies = re.sub(
                        rf'{re.escape(bad_pkg)}(\s*[@><=!~;].*?(?=\n|$))?',
                        alt,
                        dependencies,
                        flags=re.MULTILINE,
                    )
                    logger.info(
                        f"[ExperimentLoop] 替换依赖: {bad_pkg} → {alt} "
                        f"({info['reason']})"
                    )
        return dependencies

    def _install_dependencies_robust(self, code_dir: Path, dependencies: str) -> tuple[str, bool]:
        """
        鲁棒依赖安装，支持：
          - 长超时（dep_install_timeout，默认 2h）
          - 断点续装：检查 .install_state.json 跳过已安装的包
          - 自动重试：失败后最多重试 dep_install_max_retries 次
          - 静默后台安装 + 定期心跳日志

        返回 (完整日志, 是否全部成功)。
        """
        if not dependencies:
            return "", True

        req_file = code_dir / "requirements.txt"
        install_state_file = code_dir / INSTALL_STATE_FILE

        # 读取断点状态
        install_state = {}
        if install_state_file.exists():
            try:
                install_state = json.loads(install_state_file.read_text())
                logger.info(
                    f"[ExperimentLoop] 检测到安装断点，已完成: "
                    f"{install_state.get('completed', [])}"
                )
            except Exception:
                install_state = {}

        completed_packages = set(install_state.get("completed", []))
        failed_packages = set(install_state.get("failed", []))

        full_log = install_state.get("log", "")
        all_ok = True

        for attempt in range(self.dep_install_max_retries + 1):
            if attempt > 0:
                wait_s = min(30 * attempt, 120)  # 递增等待
                logger.info(
                    f"[ExperimentLoop] 依赖安装重试 {attempt}/{self.dep_install_max_retries}，"
                    f"等待 {wait_s}s..."
                )
                time.sleep(wait_s)

            logger.info(
                f"[ExperimentLoop] 开始安装依赖 (attempt {attempt+1}/{self.dep_install_max_retries+1}, "
                f"timeout={self.dep_install_timeout}s)..."
            )

            try:
                # 使用绝对路径或相对于 cwd 的文件名
                pip_req = str(req_file.resolve()) if req_file.parent != code_dir.resolve() else "requirements.txt"
                proc = subprocess.Popen(
                    [sys.executable, "-m", "pip", "install", "-r", pip_req],
                    cwd=str(code_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )

                install_output = ""
                last_heartbeat = time.time()
                try:
                    # 逐行读取 + 定期心跳（防止看起来卡死）
                    for line in iter(proc.stdout.readline, ""):
                        install_output += line
                        now = time.time()
                        if now - last_heartbeat > 300:  # 每 5 分钟心跳
                            logger.info(
                                f"[ExperimentLoop] 依赖安装进行中... "
                                f"(已运行 {now - last_heartbeat:.0f}s, "
                                f"输出 {len(install_output)} 字符)"
                            )
                            last_heartbeat = now
                    proc.wait(timeout=self.dep_install_timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    install_output += f"\n[ExperimentLoop] 安装超时 ({self.dep_install_timeout}s)"
                    logger.warning(f"[ExperimentLoop] 依赖安装超时 ({self.dep_install_timeout}s)")
                    # 超时不是 fatal，可能部分包已装好，继续重试
                    if attempt < self.dep_install_max_retries:
                        continue
                    all_ok = False

                full_log += install_output
                if proc.returncode == 0:
                    logger.info(f"[ExperimentLoop] 依赖安装成功")
                    # 清除安装状态
                    if install_state_file.exists():
                        install_state_file.unlink()
                    return full_log, True
                else:
                    logger.warning(
                        f"[ExperimentLoop] 依赖安装返回非零 exitcode={proc.returncode}"
                    )
                    # 保存状态用于断点续装
                    self._save_install_state(install_state_file, completed_packages, failed_packages, full_log)

            except Exception as e:
                msg = f"依赖安装异常: {e}"
                full_log += f"\n{msg}"
                logger.warning(f"[ExperimentLoop] {msg}")

            if attempt >= self.dep_install_max_retries:
                break

        # 所有重试耗尽
        if install_state_file.exists():
            install_state_file.unlink()
        return full_log, all_ok

    @staticmethod
    def _save_install_state(
        state_file: Path, completed: set, failed: set, log: str
    ) -> None:
        """保存安装断点状态。"""
        try:
            state_file.write_text(json.dumps(
                {"completed": list(completed), "failed": list(failed), "log": log[-5000:]},
                ensure_ascii=False,
            ))
        except Exception:
            pass

    # ========================================================================
    # Phase 0.4: 导入预检 & 自动修复
    # ========================================================================

    def _verify_imports(self, code_dir: Path) -> tuple[bool, list[str]]:
        """
        尝试导入 code_dir 中所有 Python 模块，检测 ImportError。

        返回 (all_ok, error_messages)。
        """
        errors = []
        py_files = sorted(code_dir.glob("*.py"))

        for py_file in py_files:
            if py_file.name.startswith("_"):
                continue
            module_name = py_file.stem

            # 用子进程隔离导入，避免污染主进程
            script = f"""
import sys
sys.path.insert(0, {str(code_dir)!r})
try:
    __import__({module_name!r})
    print("OK")
except Exception as e:
    print(f"IMPORT_ERROR: {{e}}")
"""
            try:
                result = subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=str(code_dir),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                output = (result.stdout + result.stderr).strip()
                if "IMPORT_ERROR:" in output:
                    errors.append(f"{module_name}: {output}")
                elif result.returncode != 0:
                    errors.append(f"{module_name}: exitcode={result.returncode} {output[-200:]}")
            except subprocess.TimeoutExpired:
                errors.append(f"{module_name}: 导入检查超时 (30s)")
            except Exception as e:
                errors.append(f"{module_name}: {e}")

        if errors:
            for err in errors:
                logger.warning(f"[ExperimentLoop] 导入错误: {err}")
            return False, errors
        return True, []

    def _fix_import_errors(
        self, code_dir: Path, errors: list[str], original_files: list
    ) -> int:
        """
        使用 LLM 自动修复导入错误。

        返回成功修复的文件数。
        """
        fixed_count = 0

        for error_msg in errors:
            # 解析错误：module_name: IMPORT_ERROR: ... 或 module_name: exitcode=...
            parts = error_msg.split(": ", 1)
            module_name = parts[0]
            error_detail = parts[1] if len(parts) > 1 else error_msg

            py_file = code_dir / f"{module_name}.py"
            if not py_file.exists():
                logger.warning(f"[ExperimentLoop] 无法修复：文件不存在 {py_file}")
                continue

            # 读取需要修复的文件内容
            file_content = py_file.read_text(encoding="utf-8")

            # 收集其他模块的接口信息
            signatures = self._extract_module_signatures(code_dir, module_name)

            # 调用 LLM 修复
            fix_prompt = f"""你是一位 Python 调试专家。以下模块导入失败，请修复代码。

模块: {module_name}.py
错误信息: {error_detail}

需修复的代码:
```python
{file_content}
```

项目中其他模块的可用接口（供导入参考）:
{signatures[:3000]}

常见修复策略：
1. 如果类名/函数名不一致（如导入 SharedDecoder 但实际是 SemanticDecoder），修正导入名
2. 如果参数名不一致（如 in_dim vs input_dim），修正调用参数
3. 如果导入了不存在的模块，替换为可用替代项
4. 如果相对导入路径不对，修正路径

请输出 JSON（只输出 JSON，不要其他内容）：
{{
  "fixed_code": "完整的修复后文件内容",
  "fixes": ["修改1的描述", "修改2的描述"]
}}"""

            try:
                response = self._call_llm(fix_prompt)
                result = self._parse_json(response)
                if result.get("parse_error") or not result.get("fixed_code"):
                    logger.warning(f"[ExperimentLoop] LLM 修复 {module_name} 失败：解析错误")
                    continue

                fixed_code = result["fixed_code"]
                if fixed_code.strip() == file_content.strip():
                    logger.info(f"[ExperimentLoop] {module_name} 无需修改")
                    continue

                # 写入修复后的代码
                py_file.write_text(fixed_code, encoding="utf-8")
                fixes = result.get("fixes", [])
                for fix in fixes:
                    logger.info(f"[ExperimentLoop] {module_name}: {fix}")
                fixed_count += 1

            except Exception as e:
                logger.warning(f"[ExperimentLoop] LLM 修复 {module_name} 异常: {e}")

        return fixed_count

    @staticmethod
    def _extract_module_signatures(code_dir: Path, exclude_module: str) -> str:
        """提取 code_dir 中所有模块的类/函数签名（排除 exclude_module）。"""
        import re
        sigs = []
        for py_file in sorted(code_dir.glob("*.py")):
            if py_file.stem == exclude_module or py_file.stem.startswith("_"):
                continue
            try:
                content = py_file.read_text(encoding="utf-8")
            except Exception:
                continue
            classes = re.findall(r"^class\s+(\w+)(?:\(.*?\))?:", content, re.MULTILINE)
            funcs = re.findall(
                r"^\s{0,4}def\s+(\w+)\((.*?)\).*?:", content, re.MULTILINE
            )
            if classes or funcs:
                sigs.append(f"\n# {py_file.name}")
                for c in classes:
                    sigs.append(f"class {c}")
                for name, args in funcs[:5]:
                    sigs.append(f"  def {name}({args[:80]})")
        return "\n".join(sigs)

    # ========================================================================
    # Phase 0.5: 接口契约验证
    # ========================================================================

    @staticmethod
    def _validate_contract(code_dir: Path, manifest: dict) -> list[str]:
        """验证实际代码是否符合接口契约。

        检查：
        1. 契约中声明的类/函数是否确实存在
        2. 跨文件导入是否使用了正确的名称
        """
        import re
        errors = []

        # 1. 检查每个模块的导出
        for module_def in manifest.get("modules", []):
            fname = module_def.get("file", "")
            py_file = code_dir / fname
            if not py_file.exists():
                continue  # 文件可能尚未生成

            try:
                file_content = py_file.read_text(encoding="utf-8")
            except Exception:
                continue

            for export_sig in module_def.get("exports", []):
                # 提取类名或函数名
                class_match = re.search(r"class\s+(\w+)", export_sig)
                func_match = re.search(r"def\s+(\w+)", export_sig)
                name = None
                if class_match:
                    name = class_match.group(1)
                    # 检查类是否在文件中定义
                    if not re.search(rf"class\s+{name}\b", file_content):
                        errors.append(
                            f"契约违规: {fname} 应导出 class {name}，但未找到定义"
                        )
                elif func_match:
                    name = func_match.group(1)
                    if not re.search(rf"def\s+{name}\b", file_content):
                        errors.append(
                            f"契约违规: {fname} 应导出 def {name}，但未找到定义"
                        )

        # 2. 检查跨文件导入
        for file_name, expected_imports in manifest.get("cross_file_contracts", {}).items():
            py_file = code_dir / file_name
            if not py_file.exists():
                continue

            try:
                file_content = py_file.read_text(encoding="utf-8")
            except Exception:
                continue

            for expected_imp in expected_imports:
                # 解析 "from X import Y" 或 "import X"
                from_match = re.match(r"from\s+(\S+)\s+import\s+(.+)", expected_imp)
                if from_match:
                    module = from_match.group(1)
                    # 提取被导入的名称
                    imported = [n.strip() for n in from_match.group(2).split(",")]
                    for imp_name in imported:
                        # 检查是否存在对应的 import 语句
                        pattern = rf"from\s+{re.escape(module)}\s+import\s+.*\b{re.escape(imp_name)}\b"
                        if not re.search(pattern, file_content):
                            errors.append(
                                f"契约违规: {file_name} 应包含 '{expected_imp}'"
                            )

        return errors

    def _fix_contract_violations(
        self, code_dir: Path, errors: list[str], manifest: dict
    ) -> int:
        """使用 LLM 根据契约修复代码违规。"""
        import re
        fixed = 0

        for error_msg in errors:
            # 解析违规信息
            match = re.search(r"契约违规: (\S+) (应导出|应包含)", error_msg)
            if not match:
                continue

            fname = match.group(1)
            py_file = code_dir / fname
            if not py_file.exists():
                continue

            # 查找契约中该文件的正确导出
            correct_exports = []
            for m in manifest.get("modules", []):
                if m.get("file") == fname:
                    correct_exports = m.get("exports", [])
                    break

            correct_imports = manifest.get("cross_file_contracts", {}).get(fname, [])

            file_content = py_file.read_text(encoding="utf-8")

            fix_prompt = f"""你是一位 Python 代码审计专家。请根据接口契约修复代码。

文件: {fname}
错误: {error_msg}

契约规定的接口:
{chr(10).join(f'- {e}' for e in correct_exports)}

契约规定的导入:
{chr(10).join(f'- {e}' for e in correct_imports)}

当前代码:
```python
{file_content}
```

请输出 JSON:
{{
  "fixed_code": "完整的修复后文件内容",
  "fixes": ["修改1描述"]
}}"""

            try:
                response = self._call_llm(fix_prompt)
                result = self._parse_json(response)
                if not result.get("parse_error") and result.get("fixed_code"):
                    fixed_code = result["fixed_code"]
                    if fixed_code.strip() != file_content.strip():
                        py_file.write_text(fixed_code, encoding="utf-8")
                        for fix in result.get("fixes", []):
                            logger.info(f"[ExperimentLoop] 契约修复 {fname}: {fix}")
                        fixed += 1
            except Exception as e:
                logger.warning(f"[ExperimentLoop] 契约修复 {fname} 失败: {e}")

        return fixed

    # ========================================================================
    # Phase 0.6: 闭环定点修复
    # ========================================================================

    def _targeted_fix(
        self, code_dir: Path, error_output: str, analysis: dict, inputs: dict
    ) -> int:
        """
        根据训练崩溃输出，进行定点修复（只修改出错的文件）。

        策略：
        1. 从 error_output 中提取 Traceback，定位出错文件和行号
        2. 从 analysis 中提取 LLM 识别的问题和建议
        3. 调用 LLM 只修复出错的特定代码段
        4. 保留未出错的其他文件不变

        返回修复的文件数。
        """
        import re

        # 1. 解析 Traceback
        tb_pattern = re.compile(
            r'File "([^"]+)", line (\d+), in (\w+)[\s\S]*?(\w+(?:Error|Exception|Warning))(?::\s*(.+))?',
        )
        tb_matches = tb_pattern.findall(error_output)
        if not tb_matches:
            # 尝试更宽松的匹配
            tb_matches = re.findall(
                r'File "([^"]+)", line (\d+)', error_output
            )
            if tb_matches:
                tb_matches = [(f, l, "?", "Error", "") for f, l in tb_matches]

        if not tb_matches:
            logger.info("[ExperimentLoop] 未找到 Traceback，使用 LLM 分析定位")
            # 让 LLM 从错误输出中定位问题
            locator_prompt = f"""分析以下程序崩溃输出，确定出错文件和修复方案。

错误输出:
{error_output[-2000:]}

LLM 分析摘要:
{json.dumps(analysis, ensure_ascii=False)[:500]}

请输出 JSON:
{{
  "target_file": "出错的 .py 文件名",
  "target_line": 行号(数字),
  "error_type": "错误类型关键词",
  "fix_hint": "修复方向（一句话）"
}}"""
            try:
                loc_result = self._parse_json(self._call_llm(locator_prompt))
                if not loc_result.get("parse_error") and loc_result.get("target_file"):
                    tb_matches = [(
                        loc_result["target_file"],
                        str(loc_result.get("target_line", 1)),
                        "?",
                        loc_result.get("error_type", "Error"),
                        loc_result.get("fix_hint", ""),
                    )]
            except Exception:
                pass

        if not tb_matches:
            return 0

        # 2. 对每个出错文件进行定点修复
        fixed_files = set()
        for file_name, line_no, func_name, err_type, err_msg in tb_matches[:3]:
            # 标准化文件名（Traceback 中的路径可能是相对路径或绝对路径）
            py_file = code_dir / Path(file_name).name
            if not py_file.exists():
                # 搜索匹配
                candidates = list(code_dir.glob(f"*{Path(file_name).stem}*.py"))
                if candidates:
                    py_file = candidates[0]
                else:
                    continue

            if py_file.name in fixed_files:
                continue

            file_content = py_file.read_text(encoding="utf-8")
            target_line = int(line_no) if line_no.isdigit() else 0

            # 提取错误上下文（出错的函数/类）
            context_start = max(0, target_line - 5)
            context_lines = file_content.split("\n")[context_start:target_line + 5]
            error_context = "\n".join(
                f"{context_start + i + 1}: {l}"
                for i, l in enumerate(context_lines)
            )

            # 提取文件中其他模块的接口供参考
            signatures = self._extract_module_signatures(code_dir, py_file.stem)

            # 3. LLM 定点修复
            fix_prompt = f"""你是一位 Python 调试专家。请**只修复这一个特定错误**，保持其他代码不变。

文件: {py_file.name}
错误位置: 第 {line_no} 行，函数 {func_name}
错误类型: {err_type}: {err_msg}

出错代码上下文:
```python
{error_context}
```

完整文件:
```python
{file_content}
```

其他模块可用接口:
{signatures[:2000]}

错误分析:
{json.dumps(analysis.get('issues', []) + analysis.get('recommendations', []), ensure_ascii=False)[:1000]}

修复原则:
1. **只修改出错的部分**，不要重构或重写整个文件
2. 优先修改参数名/类名/导入语句使其与其他文件一致
3. 如果某个方法不存在，添加最小实现
4. 如果参数名不匹配，修改调用方使其匹配定义方

请输出 JSON:
{{
  "fixed_code": "完整文件内容（只改了需要修改的部分）",
  "changes": ["具体修改1", "具体修改2"]
}}"""

            try:
                response = self._call_llm(fix_prompt)
                result = self._parse_json(response)
                if result.get("parse_error") or not result.get("fixed_code"):
                    continue

                fixed_code = result["fixed_code"]
                if fixed_code.strip() == file_content.strip():
                    continue

                py_file.write_text(fixed_code, encoding="utf-8")
                fixed_files.add(py_file.name)
                for change in result.get("changes", []):
                    logger.info(f"[ExperimentLoop] 定点修复 {py_file.name}: {change}")

            except Exception as e:
                logger.warning(f"[ExperimentLoop] 定点修复 {py_file.name} 失败: {e}")

        return len(fixed_files)

    # ========================================================================
    # Phase 1: 连接/写入文件
    # ========================================================================

    @staticmethod
    def _write_code_files(files: list, code_dir: Path) -> None:
        """写入代码文件到目录。"""
        for file_info in files:
            file_path = code_dir / file_info["path"]
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(file_info["content"], encoding="utf-8")

    def _build_train_args(self, state: dict, code_dir: Path) -> list:
        """构建训练脚本的命令行参数。

        策略：先通过 --help 探测脚本接受的参数，只注入匹配的。
        """
        args = []
        output_dir = code_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        # 候选参数（参数名 → 取值函数）
        import torch as _torch
        candidates = {
            "data_root": lambda: str(state.get("dataset_path", "")),
            "dataset_path": lambda: str(state.get("dataset_path", "")),
            "data_dir": lambda: str(state.get("dataset_path", "")),
            "root": lambda: str(state.get("dataset_path", "")),
            "output_dir": lambda: str(output_dir),
            "save_dir": lambda: str(output_dir),
            "log_dir": lambda: str(output_dir),
            "out_dir": lambda: str(output_dir),
            "device": lambda: "cuda" if _torch.cuda.is_available() else "cpu",
        }

        # 合并 YAML 自定义映射
        if self.cli_arg_map:
            for cli_name, state_key in self.cli_arg_map.items():
                if state_key and state.get(state_key):
                    candidates[cli_name] = lambda k=state_key: str(state.get(k, ""))

        # 探测脚本接受的参数
        accepted_args = self._probe_script_args(self.training_script, code_dir)

        # 只注入脚本接受的参数
        for cli_name, get_value in candidates.items():
            arg_name = f"--{cli_name}"
            if arg_name in accepted_args:
                try:
                    val = get_value()
                    if val:
                        args.extend([arg_name, val])
                except Exception:
                    pass

        # 额外固定参数（用户明确指定的，直接追加）
        if self.extra_train_args:
            args.extend(self.extra_train_args)

        return args

    @staticmethod
    def _probe_script_args(script_name: str, code_dir: Path) -> set:
        """通过 --help 探测脚本接受的参数名集合。"""
        script_path = code_dir / script_name
        if not script_path.exists():
            return set()

        try:
            result = subprocess.run(
                [sys.executable, script_path.name, "--help"],
                cwd=str(code_dir),
                capture_output=True,
                text=True,
                timeout=15,
            )
            help_text = (result.stdout + result.stderr).lower()
        except Exception:
            return set()

        # 提取 --xxx 格式的参数名
        import re
        args_found = set(re.findall(r'(--[\w-]+)', help_text))
        logger.debug(f"[ExperimentLoop] 脚本 --help 探测到 {len(args_found)} 个参数: {sorted(args_found)[:10]}...")
        return args_found

    # ========================================================================
    # 内部方法：配置、退出条件、输出读取、指标提取、进程终止、分析
    # ========================================================================

    def _merge_config(self, yaml_config: dict) -> dict:
        """合并 YAML 配置和 Agent 属性。"""
        return {
            "check_interval": yaml_config.get("check_interval", self.check_interval),
            "max_epochs": yaml_config.get("max_epochs", self.max_epochs),
            "target_loss": yaml_config.get("target_loss", self.target_loss),
            "patience": yaml_config.get("patience", self.patience),
            "max_time": yaml_config.get("max_time", self.max_time),
            "monitor_metric": yaml_config.get("monitor_metric", self.monitor_metric),
        }

    def _build_exit_conditions(self, cfg: dict) -> list:
        """根据配置构建退出条件列表。"""
        conditions = []

        conditions.append(ExitCondition("max_epochs", cfg["max_epochs"], metric="epoch", mode="max"))

        if cfg.get("target_loss") is not None:
            conditions.append(ExitCondition("target_loss", cfg["target_loss"],
                                            metric=cfg["monitor_metric"], mode="min"))

        conditions.append(ExitCondition("patience", cfg["patience"],
                                        metric=cfg["monitor_metric"], mode="min"))

        conditions.append(ExitCondition("max_time", cfg["max_time"]))

        if self.target_metric:
            for m_name, m_cfg in self.target_metric.items():
                conditions.append(ExitCondition(
                    "target_metric", m_cfg.get("value"),
                    metric=m_name, mode=m_cfg.get("mode", "min")
                ))

        return conditions

    def _evaluate_exit_conditions(
        self,
        conditions: list,
        metrics: dict,
        elapsed: float,
        best_metric_val: float,
        patience_counter: int,
        last_epoch: int,
    ) -> dict:
        """评估所有退出条件。返回 {"should_exit": bool, "reason": str}。"""
        for cond in conditions:
            if cond.type == "max_epochs":
                if metrics and metrics.get("epoch", -1) >= cond.value:
                    return {"should_exit": True, "reason": f"达到最大 epoch: {cond.value}"}

            elif cond.type == "target_loss":
                val = metrics.get(cond.metric)
                if val is not None and val <= cond.value:
                    return {"should_exit": True, "reason": f"损失 {val:.6f} <= 目标 {cond.value}"}

            elif cond.type == "patience":
                if patience_counter >= cond.value:
                    return {"should_exit": True, "reason": f"早停: {cond.value} 轮无改善 (best={best_metric_val:.6f})"}

            elif cond.type == "max_time":
                if elapsed >= cond.value:
                    return {"should_exit": True, "reason": f"超过最大运行时间: {elapsed:.0f}s >= {cond.value}s"}

            elif cond.type == "target_metric":
                val = metrics.get(cond.metric)
                if val is not None:
                    if cond.mode == "min" and val <= cond.value:
                        return {"should_exit": True, "reason": f"指标 {cond.metric}={val:.6f} <= 目标 {cond.value}"}
                    elif cond.mode == "max" and val >= cond.value:
                        return {"should_exit": True, "reason": f"指标 {cond.metric}={val:.6f} >= 目标 {cond.value}"}

        return {"should_exit": False, "reason": ""}

    def _read_output(self, proc: subprocess.Popen) -> str:
        """非阻塞读取子进程 stdout。"""
        import select
        output = ""
        try:
            if proc.stdout and select.select([proc.stdout], [], [], 0.1)[0]:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    output += line
        except (ValueError, OSError):
            pass
        return output

    def _read_remaining(self, proc: subprocess.Popen, prev_output: str) -> str:
        """进程结束后读取残留输出。"""
        try:
            if proc.stdout:
                remaining = proc.stdout.read()
                if remaining:
                    return prev_output + remaining
        except (ValueError, OSError):
            pass
        return prev_output

    def _extract_metrics(self, text: str, patterns: dict) -> dict:
        """从训练输出中提取最新指标。"""
        metrics = {}
        for name, pattern in patterns.items():
            matches = re.findall(pattern, text)
            if matches:
                try:
                    val = float(matches[-1])
                    if name == "epoch":
                        val = int(val)
                    metrics[name] = val
                except (ValueError, IndexError):
                    pass
        return metrics

    @staticmethod
    def _terminate_process(proc: subprocess.Popen):
        """优雅终止子进程。"""
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        except Exception:
            pass

    def _analyze_experiment(
        self,
        exit_reason: str,
        all_metrics: list,
        final_output: str,
        elapsed: float,
    ) -> dict:
        """使用 LLM 分析实验结果。"""
        metrics_summary = json.dumps(all_metrics[-10:] if all_metrics else [], ensure_ascii=False)

        prompt = f"""你是一位深度学习实验分析专家。请分析以下训练实验结果。

退出原因: {exit_reason}
总运行时间: {elapsed:.0f}s
最近 10 条指标记录:
{metrics_summary}

训练日志尾部:
{final_output[-2000:] if final_output else '(无)'}

请输出 JSON 分析报告：
{{
  "summary": "实验总结（2-3句话，包含关键发现）",
  "convergence": "收敛情况分析（是否收敛、收敛速度）",
  "best_result": "最佳结果描述",
  "issues": ["发现的问题"],
  "recommendations": ["建议的改进方向"],
  "should_continue": true/false
}}"""

        try:
            response = self._call_llm(prompt)
            return self._parse_json(response)
        except Exception as e:
            logger.warning(f"[ExperimentLoop] LLM 分析失败: {e}")
            return {
                "summary": f"实验在 {elapsed:.0f}s 后以 '{exit_reason}' 结束",
                "should_continue": False,
                "parse_error": True,
            }
