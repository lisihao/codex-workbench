from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Protocol


RESEARCH_POLICY_VERSION = "research-skill/v2"
RESEARCH_SKILL_NAME = "Research"
RESEARCH_SKILL_REQUIRED_FILES = (
    "SKILL.md",
    "UrlVerificationProtocol.md",
    "Workflows/StandardResearch.md",
    "Workflows/DeepInvestigation.md",
)


class ResearchContract(Protocol):
    objective: str
    task_type: str
    complexity: str


@dataclass(frozen=True)
class ResearchRoute:
    mode: str
    reason: str

    @property
    def required(self) -> bool:
        return self.mode != "none"

    def to_dict(self) -> dict[str, object]:
        return {
            "policy": RESEARCH_POLICY_VERSION,
            "skill": RESEARCH_SKILL_NAME if self.required else None,
            "required": self.required,
            "mode": self.mode,
            "reason": self.reason,
        }


_DEEP_PARALLEL = re.compile(
    r"(?:deep|extensive|parallel)\s+research|multi[- ]research(?:er| agent)|"
    r"深度并行研究|广泛研究|多研究代理|多代理研究",
    re.IGNORECASE,
)
_SOURCE_GROUNDED = re.compile(
    r"\bresearch\b|\binvestigat(?:e|ion)\b|\bpaper\b|\barxiv\b|"
    r"\bbenchmark\b|\bupstream\b|\breference architecture\b|"
    r"\bbest practice\b|\btechnology selection\b|\bmigration strategy\b|"
    r"\bcompetitive analysis\b|\bfeasibility\b|\bstate of the art\b|"
    r"研究|调研|深入分析|深度分析|原始资料|论文|上游|参考架构|"
    r"技术选型|迁移方案|性能瓶颈|性能优化|基准测试|最佳实践|"
    r"竞品|可行性|技术趋势|现状分析|安全审计|兼容性评估",
    re.IGNORECASE,
)


def route_research(contract: ResearchContract) -> ResearchRoute:
    objective = contract.objective.strip()
    if _DEEP_PARALLEL.search(objective):
        return ResearchRoute(
            "deep-parallel",
            "the objective explicitly requests deep, extensive, or parallel research",
        )
    if contract.task_type in {"architecture", "exploration"}:
        return ResearchRoute(
            "standard",
            f"{contract.task_type} planning requires source-grounded analysis by default",
        )
    if contract.complexity == "high":
        return ResearchRoute(
            "standard",
            "high-complexity planning requires a source-grounded research pass",
        )
    if _SOURCE_GROUNDED.search(objective):
        return ResearchRoute(
            "standard",
            "the objective contains a source-grounded research trigger",
        )
    return ResearchRoute(
        "none",
        "bounded local work does not require external research",
    )


def managed_research_skill_status(process_home: str | Path | None) -> dict[str, object]:
    if not process_home:
        return {
            "ok": None,
            "policy": RESEARCH_POLICY_VERSION,
            "name": RESEARCH_SKILL_NAME,
            "reason": "not an authority process",
            "path": None,
        }
    root = Path(process_home) / ".agents" / "skills" / "research"
    missing = [name for name in RESEARCH_SKILL_REQUIRED_FILES if not (root / name).is_file()]
    return {
        "ok": not missing,
        "policy": RESEARCH_POLICY_VERSION,
        "name": RESEARCH_SKILL_NAME,
        "reason": "managed Research skill is available"
        if not missing
        else f"managed Research skill is missing required files: {', '.join(missing)}",
        "path": str(root),
    }


def research_planner_directive(contract: ResearchContract) -> str:
    route = route_research(contract)
    invocation = (
        f"Mandatory skill invocation: ${RESEARCH_SKILL_NAME}. Read its complete SKILL.md and routed workflow before decomposing."
        if route.required
        else "Research skill invocation: not required for this bounded local task."
    )
    return f"""Research routing policy: {RESEARCH_POLICY_VERSION}.
Research route: mode={route.mode}; reason={route.reason}.
{invocation}
- Research is mandatory for architecture and exploration, high-complexity decisions, papers or upstream/reference implementations, technology selection and migration, performance or benchmark claims, current best practices, compatibility/security assessments, competitive analysis, feasibility, and explicit deep analysis.
- When research is required, prefer primary sources, verify every delivered URL and its supporting content, and distinguish source evidence from engineering inference.
- Standard research is performed by the current Sol planner with native read-only search and source reading. Do not launch extra research agents merely because analysis is difficult.
- Only when the user explicitly requests deep, extensive, or parallel research may the DAG contain multiple independent research nodes plus a synthesis dependency. Those nodes remain subject to the declared scope, authenticated subscription availability, and Claude reserve policy.
- Ordinary repository inspection, localized debugging, and implementation planning that do not need external evidence must not invoke the Research skill."""
