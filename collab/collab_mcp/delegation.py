"""should_delegate — 任务协作评分 + 队友能力匹配（压力测试清单第 5 项）。

输入任务描述，输出 0-100 的协作评分与建议；v1.7.3 起支持按能力标签
（skills: "domain:action"）精确匹配队友，标签缺失时回退到 capabilities
描述文本做子串匹配，供 LLM 做语义兜底。
"""

import re

from .config import COLLAB_DIR
from .utils import ok, safe_read_json

# 倾向于"应该协作/拆解"的信号
HIGH_SIGNALS = [
    "重构", "复杂", "架构", "大型", "多文件", "多个文件", "多模块",
    "跨模块", "联调", "接口", "代码审查", "评审", "并发", "分布式",
    "依赖分析", "新功能", "大改", "规模",
]

# 倾向于"自己干更快"的机械性信号
LOW_SIGNALS = [
    "统一", "格式化", "日志格式", "改日志", "重命名", "改文案",
    "批量", "机械", "简单", "小改动", "改注释", "排版", "拼写", "复制粘贴",
]


def _score(description: str) -> dict:
    """返回评分结果（分数 0-100、级别、命中的信号）。"""
    score = 40
    hit_high = [k for k in HIGH_SIGNALS if k in description]
    hit_low = [k for k in LOW_SIGNALS if k in description]

    # 文件数量信号：3 个及以上文件 → 明显该拆
    m = re.search(r"(\d+)\s*个文件", description)
    if m:
        n = int(m.group(1))
        if n >= 3:
            score += min(25, 8 * (n - 2))
        elif n == 1:
            score -= 10

    score += min(40, 15 * len(hit_high))
    score -= min(40, 12 * len(hit_low))

    if any(k in description for k in ("协作", "分工", "派给", "队友", "多人")):
        score += 15

    score = max(0, min(100, score))
    if score >= 70:
        level = "必须协作拆解"
    elif score >= 45:
        level = "建议协作"
    else:
        level = "自己执行更快"

    return {
        "score": score,
        "level": level,
        "hit_high": hit_high,
        "hit_low": hit_low,
    }


def _parse_tags(tags_str: str) -> list[str]:
    """把逗号分隔的标签串解析为列表（如 "code-review:python,testing:unit"）。"""
    return [t.strip() for t in tags_str.split(",") if t.strip()]


def _load_teammates() -> list[dict]:
    """读取队友注册表；无注册表时返回空列表。"""
    registry_file = COLLAB_DIR / "teammates.json"
    if not registry_file.exists():
        return []
    registry = safe_read_json(registry_file) or {}
    return list(registry.values())


def _fallback_hits(tag: str, caps_text: str) -> bool:
    """标签回退匹配：标签整体、domain 段或 action 段出现在描述里。"""
    if not caps_text:
        return False
    lowered = caps_text.lower()
    parts = [tag] + tag.split(":", 1)
    return any(p and p.lower() in lowered for p in parts)


def _match_required_skills(teammates: list[dict], required: list[str]) -> list[dict]:
    """按 required_skills 标签精确匹配队友；缺失标签回退描述匹配。"""
    matches = []
    for t in teammates:
        skills = t.get("skills", []) or []
        caps_text = t.get("capabilities", "") or ""
        matched = [s for s in required if s in skills]
        missing = [s for s in required if s not in skills]
        fallback_hits = [s for s in missing if _fallback_hits(s, caps_text)]
        score = 50 + 25 * len(matched) - 15 * len(missing) + 10 * len(fallback_hits)
        matches.append({
            "name": t.get("name", "?"),
            "matched": matched,
            "missing": missing,
            "fallback_hits": fallback_hits,
            "capabilities": caps_text,
            "score": max(0, score),
        })
    matches.sort(key=lambda m: (-len(m["matched"]), -len(m["fallback_hits"]), -m["score"]))
    return matches


def _match_by_description(teammates: list[dict], description: str) -> list[dict]:
    """无 required_skills 时的轻量扫描：描述命中队友技能标签即推荐。"""
    matches = []
    lowered = description.lower()
    for t in teammates:
        skills = t.get("skills", []) or []
        matched = [s for s in skills if s.lower() in lowered]
        if not matched:
            matched = [
                s for s in skills
                if any(p and p.lower() in lowered for p in s.split(":"))
            ]
        if matched:
            matches.append({
                "name": t.get("name", "?"),
                "matched": matched,
                "missing": [],
                "fallback_hits": [],
                "capabilities": t.get("capabilities", "") or "",
                "score": 60,
            })
    matches.sort(key=lambda m: -len(m["matched"]))
    return matches


async def should_delegate(
    task_description: str,
    required_skills: str = "",
) -> str:
    """评估一个任务是否值得拆解协作（压力测试清单第 5 项）。

    根据任务描述中的信号（文件数量、复杂度关键词、机械性关键词）给出
    0-100 协作评分：>=70 必须协作拆解；45-69 建议协作；<45 自己执行更快。
    v1.7.3 起：传 required_skills（逗号分隔的 "domain:action" 标签）时，
    会读取 teammates.json 按标签精确匹配并给出 suggest_teammate；
    标签缺失时回退到 capabilities 描述文本（供 LLM 语义兜底）。
    不传 required_skills 时仍保留原有关键词评分，并附加轻量标签扫描。

    Args:
        task_description: 任务描述（标题 + 内容即可）
        required_skills: 可选，逗号分隔的能力标签（如 "code-review:python,testing:unit"）
    """
    result = _score(task_description)
    suggest = "any" if result["score"] >= 70 else ("指定队友" if result["score"] >= 45 else "自己执行")
    teammates = _load_teammates()
    required = _parse_tags(required_skills)

    if required:
        matches = _match_required_skills(teammates, required)
        best = matches[0] if matches else None
        suggest_teammate = (
            best["name"]
            if best and (best["matched"] or best["fallback_hits"])
            else ""
        )
        # 精确覆盖全部必需标签 → 明显应该派给该队友
        if best and best["matched"] and not best["missing"]:
            result["score"] = max(result["score"], 70)
            result["level"] = "建议协作"
            suggest = best["name"]
        teammate_match = matches[:3]
    else:
        teammate_match = _match_by_description(teammates, task_description)
        suggest_teammate = teammate_match[0]["name"] if teammate_match else ""

    return ok({
        "score": result["score"],
        "level": result["level"],
        "suggest_assignee": suggest,
        "hit_high": result["hit_high"],
        "hit_low": result["hit_low"],
        "suggest_teammate": suggest_teammate,
        "teammate_match": teammate_match,
    })
