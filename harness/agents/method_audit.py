"""Evidence-linked model review; structural validation is not a mathematical proof."""
import hashlib
import json


AUDIT_VERSION = 3
BLOCKING = {"critical", "major"}


def candidate_digest(candidate):
    body = {key: value for key, value in candidate.items() if key != "consistency_audit"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def validate_review(review):
    if (not isinstance(review, dict) or type(review.get("valid")) is not bool
            or not isinstance(review.get("issues"), list) or review.get("parse_error")):
        raise ValueError("Invalid method audit response; candidate retained")
    for index, issue in enumerate(review["issues"]):
        if (not isinstance(issue, dict) or issue.get("severity") not in BLOCKING | {"minor"}
                or any(not isinstance(issue.get(key), str) or not issue[key].strip()
                       for key in ("invariant", "contradiction", "repair"))):
            raise ValueError(f"Invalid method audit issue {index}: severity/invariant/contradiction/repair required; candidate retained")
    blocked = any(issue["severity"] in BLOCKING for issue in review["issues"])
    if review["valid"] == blocked:
        raise ValueError("Contradictory method audit verdict; candidate retained")
    return review


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def quote_catalog(candidate):
    """Offer exact source excerpts by id so indentation need not be retyped."""
    body = {key: item for key, item in candidate.items() if key != "consistency_audit"}
    lines = dict.fromkeys(line.strip() for text in _strings(body)
                          for line in text.splitlines() if line.strip())
    return {f"Q{index}": line for index, line in enumerate(lines)}


def validate_verification(value, candidate, review):
    expected = {i for i, item in enumerate(review["issues"]) if item["severity"] in BLOCKING}
    decisions = value.get("decisions") if isinstance(value, dict) else None
    if not isinstance(decisions, list) or len(decisions) != len(expected):
        raise ValueError("Incomplete method audit verification; candidate retained")
    seen, normalized = set(), []
    catalog = quote_catalog(candidate)
    candidate_body = {key: item for key, item in candidate.items() if key != "consistency_audit"}
    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError("Invalid method audit verification; candidate retained")
        index = decision.get("issue_index")
        if type(index) is not int or index not in expected or index in seen:
            raise ValueError("Invalid method audit issue index; candidate retained")
        seen.add(index)
        if decision.get("decision") not in {"confirmed", "dismissed", "unresolved"}:
            raise ValueError("Invalid method audit decision; candidate retained")
        quote, reason = decision.get("candidate_quote"), decision.get("reason")
        if "candidate_quote_id" in decision:
            ref = decision["candidate_quote_id"]
            if not isinstance(ref, str) or ref not in catalog:
                raise ValueError("Unknown candidate quote id; candidate retained")
            if quote is not None and quote != catalog[ref]:
                raise ValueError("Candidate quote differs from selected source id; candidate retained")
            quote = catalog[ref]
        if (not isinstance(quote, str) or not quote.strip()
                or not any(quote in text for text in _strings(candidate_body))):
            raise ValueError(f"Method audit issue {index} needs one exact contiguous candidate quote, without ellipses or paraphrasing; candidate retained")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"Method audit issue {index} needs a verification reason; candidate retained")
        normalized.append({**decision, "candidate_quote": quote})
    return normalized


def resolve_issues(review, decisions):
    outcomes = {item["issue_index"]: item["decision"] for item in decisions}
    if "unresolved" in outcomes.values():
        raise ValueError("Method audit verification unresolved; candidate retained for verification")
    return [item for i, item in enumerate(review["issues"])
            if item["severity"] not in BLOCKING or outcomes.get(i) == "confirmed"]


def context_digest(question, direction):
    return hashlib.sha256(json.dumps([question, direction], ensure_ascii=False).encode("utf-8")).hexdigest()


def verify_record(candidate, audit, original_direction=None):
    """Recompute record consistency instead of trusting a cached valid=true."""
    if not isinstance(audit, dict) or audit.get("protocol_version") not in (2, AUDIT_VERSION):
        raise ValueError("Unsupported method audit record")
    if audit["protocol_version"] == AUDIT_VERSION:
        context = audit.get("context")
        if (not isinstance(context, dict)
                or not all(isinstance(context.get(key), str) for key in ("question", "original_direction"))
                or audit.get("context_sha256") != context_digest(context["question"], context["original_direction"])):
            raise ValueError("Method audit context hash mismatch")
        if original_direction is not None and context["original_direction"] != original_direction:
            raise ValueError("Method audit original research direction mismatch")
    if audit.get("candidate_sha256") != candidate_digest(candidate):
        raise ValueError("Method audit candidate hash mismatch")
    review = validate_review(audit.get("initial_review"))
    decisions = validate_verification({"decisions": audit.get("verification")}, candidate, review)
    issues = resolve_issues(review, decisions)
    valid = not any(item["severity"] in BLOCKING for item in issues)
    if audit.get("issues") != issues or audit.get("valid") is not valid:
        raise ValueError("Method audit record verdict mismatch")


def run_audit(candidate, question, call, pending, persist, *, original_direction=""):
    """Resume review/verification independently; unresolved findings never pass."""
    digest = candidate_digest(candidate)
    context_hash = context_digest(question, original_direction)
    if (pending.get("protocol_version") != AUDIT_VERSION or pending.get("candidate_sha256") != digest
            or pending.get("context_sha256") != context_hash):
        pending = {"protocol_version": AUDIT_VERSION, "candidate_sha256": digest,
                   "context_sha256": context_hash,
                   "context": {"question": question, "original_direction": original_direction}}
    review = pending.get("initial_review")
    if review is None:
        prompt = f"""你是方法一致性审计员。检查候选内部矛盾及其对原始研究约束的忠实性，不评价创新性或尚待实验验证的效果。
研究问题：{question}
原始研究约束（优先于规划阶段的转述）：{original_direction}
候选方法：{json.dumps(candidate, ensure_ascii=False)}
逐项对照原始设定中的公式符号、目标总体、主基线、场景范围、聚合单位和固定参数。
候选内部一致但改变了原题也属于 major 问题。不得以实验尚未执行为由忽略定义偏离。
对非线性公式实际代入零点和一个非零点核对边界与方向；不能仅重复候选声称的性质。
检查定义、边界、更新方向、约束、文字与伪代码。在线预测只能用历史标签；
使用当前或未来标签选参或构造同一预测属于 major 问题。
必须追踪被影响的预测下标：y_t -> 状态_(t+1) -> C_(t+1) 是合法更新，
y_t -> C_t 才是泄漏。输出 C_t 后观测 y_t 不自动构成泄漏。
必须按候选的变量定义判断，用数值代入或具体数据流指出矛盾。不要把
“可能”“需要确认”、性能猜测、命名习惯或缺少实验当成已证明的内部矛盾。
输出 JSON：{{"valid":true,"issues":[{{"severity":"critical|major|minor",
"invariant":"不变量","contradiction":"具体矛盾","repair":"最小修复"}}]}}。
有 critical/major 问题时 valid=false，否则 true。最多三个问题，每个字段不超过80字。
"""
        if pending.get("review_error"):
            prompt += ("\n修正上次响应格式，不改变审计范围：" + pending["review_error"]
                       + "\n上次响应：" + json.dumps(pending.get("last_review_response"), ensure_ascii=False)[:4000])
        review = call(prompt)
        try:
            validate_review(review)
        except ValueError as exc:
            persist({**pending, "last_review_response": review, "review_error": str(exc)})
            raise
        pending = {**pending, "initial_review": review}
        persist(pending)
    else:
        validate_review(review)

    blockers = [{"issue_index": i, **issue} for i, issue in enumerate(review["issues"])
                if issue["severity"] in BLOCKING]
    decisions = pending.get("verification")
    if decisions is None:
        if blockers:
            prompt = f"""独立复核下列审计意见。审计员可能犯错；以候选原文及原始研究约束为准。
研究问题：{question}
原始研究约束：{original_direction}
候选方法：{json.dumps(candidate, ensure_ascii=False)}
待复核意见：{json.dumps(blockers, ensure_ascii=False)}
对原题偏离的指控，同时对照原始要求与候选；候选内部自洽不能消除偏离。
对每项用实际公式的数值代入、循环执行示例或标签依赖顺序核验；确认阻塞必须给出
具体反例，排除意见必须解释为何不成立。若信息不足用 unresolved，不能猜测通过。
不要把优化目标和安全回退混同，不要用参数名字的习惯含义替代候选中的定义。
检查泄漏时明确被影响的预测下标：用 y_t 更新 C_(t+1) 的参数是合法的，
用 y_t 构造 C_t 才是泄漏；必须找到 y_t 到 C_t 的实际依赖才能 confirmed。
只输出 JSON：{{"decisions":[{{"issue_index":0,
"decision":"confirmed|dismissed|unresolved", "candidate_quote":"逐字引用候选中的原文片段",
"reason":"简短的数值/执行核验，说明实际与预期是否矛盾"}}]}}。
每项且仅一项，candidate_quote 只复制一段连续原文，优先一行，不得用省略号拼接，
不得改变符号、大小写或空格。reason 不超过180字；无需全文重写或额外意见。
也可用 candidate_quote_id 替代 candidate_quote，从下列原文目录选择一个 id（如 Q0）；
此时不要重写 candidate_quote，系统会逐字取回该片段。id 只是引用位置，不自动证明意见成立。
原文目录：{json.dumps(quote_catalog(candidate), ensure_ascii=False)}
"""
            if pending.get("verification_error"):
                prompt += ("\n修正上次响应格式：" + pending["verification_error"]
                           + "\n上次响应：" + json.dumps(pending.get("last_verification_response"), ensure_ascii=False)[:4000])
            verification = call(prompt)
            try:
                decisions = validate_verification(verification, candidate, review)
            except ValueError as exc:
                persist({**pending, "last_verification_response": verification,
                         "verification_error": str(exc)})
                raise
        else:
            decisions = []
        if any(item["decision"] == "unresolved" for item in decisions):
            persist({**pending, "last_verification": decisions})
            raise ValueError("Method audit verification unresolved; candidate retained for verification")
        pending = {**pending, "verification": decisions}
        persist(pending)
    else:
        validate_verification({"decisions": decisions}, candidate, review)
    issues = resolve_issues(review, decisions)
    result = {**pending, "valid": not any(item["severity"] in BLOCKING for item in issues),
              "issues": issues}
    verify_record(candidate, result)
    return result
