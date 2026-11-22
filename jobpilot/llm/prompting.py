"""Versioned prompt files (`llm/prompts/*.md`) and safe rendering.

File format:
    ---
    name: extract_profile
    version: 1
    ---
    ## system
    ...instructions...
    ## user
    ...template with {{placeholders}}...

Untrusted text (resumes, job descriptions) is only ever inserted inside XML-style
delimiters, and any closing delimiter inside it is neutralised, so the data can't
"close the tag" and smuggle instructions (prompt-injection defence, spec 11).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from jobpilot.llm.base import Prompt

PROMPTS_DIR = Path(__file__).parent / "prompts"
_FRONT = re.compile(r"^---\s*\n(?P<meta>.*?)\n---\s*\n(?P<body>.*)$", re.DOTALL)
_SECTION = re.compile(r"^## (system|user)\s*$", re.MULTILINE)
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str
    version: int
    system: str
    user: str

    @property
    def ref(self) -> str:
        return f"{self.name}@v{self.version}"

    def render(self, **values: str) -> Prompt:
        missing = set(_PLACEHOLDER.findall(self.user)) - values.keys()
        if missing:
            raise KeyError(f"prompt {self.ref} missing values: {sorted(missing)}")
        user = _PLACEHOLDER.sub(lambda m: values[m.group(1)], self.user)
        return Prompt(system=self.system, user=user, name=self.ref)


@cache
def load_prompt(name: str) -> PromptTemplate:
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    match = _FRONT.match(text)
    if not match:
        raise ValueError(f"prompt {name}: missing front matter")
    meta = dict(line.split(":", 1) for line in match.group("meta").splitlines() if ":" in line)
    parts = _SECTION.split(match.group("body"))
    sections = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    return PromptTemplate(
        name=meta.get("name", name).strip(),
        version=int(meta.get("version", "1").strip()),
        system=sections["system"],
        user=sections["user"],
    )


def fence(tag: str, text: str) -> str:
    """Wrap untrusted text in <tag>…</tag>, neutralising any delimiter inside it."""
    cleaned = re.sub(rf"</?\s*{tag}\b[^>]*>", f"[{tag} tag removed]", text, flags=re.IGNORECASE)
    return f"<{tag}>\n{cleaned}\n</{tag}>"
