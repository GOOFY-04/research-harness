"""Generate a grounded README and deterministic requirements text."""

import json
import logging

from harness.core.agent import BaseAgent
from harness.core.io import strip_outer_fence

logger = logging.getLogger(__name__)


class DocumenterAgent(BaseAgent):
    required_fields = {"readme": str, "requirements": str}
    model = "claude-sonnet-4-6"
    max_tokens = 4096

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        return ""

    def run(self, stage_id: str, inputs: dict, state: dict) -> dict:
        research_question = inputs.get("research_question", "")
        method_name = inputs.get("method_name", "")
        method_overview = inputs.get("method_overview", "")
        files = inputs.get("files", [])
        entry_point = inputs.get("entry_point", "")
        dependencies = inputs.get("dependencies", "")
        run_instructions = inputs.get("run_instructions", "")
        execution_summary = inputs.get("execution_summary", "")
        if not isinstance(execution_summary, str):
            execution_summary = json.dumps(execution_summary, ensure_ascii=False, indent=2)

        readme_prompt = f"""You are a technical documentation writer. Generate a concise README.md for this research project.

Research question: {research_question}
Method name: {method_name}
Intended method overview: {method_overview}
Code file paths:
{json.dumps([f['path'] for f in files], ensure_ascii=False, indent=2)}
Entry point: {entry_point}
Exact dependency specification:
{dependencies}
Run instructions:
{run_instructions}
Verified execution evidence:
{execution_summary}

Include only these useful sections when the supplied data supports them:
1. Project title and scope
2. Intended method overview
3. Installation using the exact supplied dependencies
4. Quick start using the exact entry point and run instructions
5. Code structure based only on supplied paths
6. Verified experiment result and limitations

Rules:
- Return Markdown only, optionally wrapped in one markdown fence.
- Treat the method overview as an intended design, not proof of implementation.
- A smoke test establishes only that code runs on its test inputs.
- Do not invent repository URLs, authors, publication status, citations, licenses,
  copyright notices, years, benchmark claims, or implementation details.
- Do not add Citation or License sections because no exact metadata was supplied.
- Report supplied numeric metrics, including negative deltas, and state their scope.
- Do not call a synthetic run a published result or evidence of real-world quality.
"""

        logger.info("[DocumenterAgent] generating README.md")
        readme_raw = self._call_llm(readme_prompt)
        readme = strip_outer_fence(readme_raw, ("markdown", "md"))
        output = {"readme": readme, "requirements": dependencies}
        self.validate_output(output)

        if self.memory:
            self.memory.append(
                topic=stage_id,
                content={"method": method_name, "readme_length": len(readme)},
                tags=["DocumenterAgent", stage_id],
            )
        return output

    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        return {"readme": raw_text}
