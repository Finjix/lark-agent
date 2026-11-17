"""The game projects the skill reviews for.

Each has a fixed spec, the Markdown in references/<project>-rules.md (written from the project's review standard), and a dynamic spec
built from its review groups' chat (rules.py). A fixed spec that is not written yet is a placeholder: its file starts with PLACEHOLDER,
and the project is reviewed against its dynamic spec alone.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .errors import Blocked


@dataclass(frozen=True)
class Project:
    """`destination`: where the material goes, as the review prompt says it ("before it is …")."""
    id: str
    name: str
    spec: str
    destination: str


PROJECTS: dict[str, Project] = {
    "GOF": Project(
        id="GOF", name="GOF (无尽冬日, Whiteout Survival China)", spec="gof-rules.md",
        destination="submitted to Chinese ad platforms (Ocean Engine / Douyin, Tencent and others) for approval. Their reviewers reject material for a single wrong word, frame or second",
    ),
    "GOT": Project(
        id="GOT", name="GOT", spec="got-rules.md",
        destination="delivered for advertising. Its markets and ad platforms are not settled yet, so apply the rules below and no single platform's own list; besides the red lines, the rules ask that viewers can follow the gameplay in the finished video",
    ),
}

PLACEHOLDER = "<!-- audit-auto: placeholder -->"


def project_of(value: object) -> Project:
    pid = value.strip().upper() if isinstance(value, str) else ""
    project = PROJECTS.get(pid)
    if not project:
        raise Blocked(f"Unknown project: {'' if value is None else value} (only GOF and GOT)")
    return project


@dataclass
class FixedSpec:
    file: str
    text: str
    placeholder: bool


_COMMENT = re.compile(r"<!--[\s\S]*?-->\n?")


def fixed_spec(root: str, project: Project) -> FixedSpec:
    """The project's fixed spec as the model is given it: the Markdown without its comments (notes for whoever maintains the file)."""
    file = os.path.join(root, "references", project.spec)
    try:
        with open(file, encoding="utf-8", newline="") as handle:
            raw = handle.read()
    except OSError:
        raise Blocked(f"The skill is incomplete: {project.id}'s fixed spec (references/{project.spec}) is missing; ask the administrator") from None
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    placeholder = raw.lstrip().startswith(PLACEHOLDER)
    return FixedSpec(file=file, placeholder=placeholder, text="" if placeholder else _COMMENT.sub("", raw).strip())
