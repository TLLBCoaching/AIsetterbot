"""Loads the setter's knowledge (script, offer, voice, learned playbook, examples).

Everything here goes into the system prompt, which is cached, so it only costs
full price on the first call after it changes.
"""

import json
import re
from pathlib import Path

from .config import settings

# Order matters: this is the order the sections appear in the system prompt.
KNOWLEDGE_FILES = [
    ("offer.md", "THE OFFER"),
    ("script.md", "THE SETTER SCRIPT"),
    ("voice.md", "HOW WE TALK"),
    ("playbook.md", "LEARNED PLAYBOOK (distilled from real conversations)"),
]

MAX_EXAMPLE_CHARS = 60_000


def _read(path: Path) -> str:
    """Reads a knowledge file, dropping <!-- editor notes --> so they don't reach the prompt."""
    if not path.exists():
        return ""
    return re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S).strip()


def load_knowledge(knowledge_dir: Path | None = None) -> str:
    knowledge_dir = knowledge_dir or settings.knowledge_dir
    sections = []
    for filename, heading in KNOWLEDGE_FILES:
        body = _read(knowledge_dir / filename)
        if body:
            sections.append(f"<section name=\"{heading}\">\n{body}\n</section>")
    return "\n\n".join(sections)


def load_examples(examples_path: Path | None = None) -> str:
    """Renders the curated example conversations as few-shot references."""
    examples_path = examples_path or settings.examples_path
    if not examples_path.exists():
        return ""
    rendered, total = [], 0
    for line in examples_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ex = json.loads(line)
        transcript = "\n".join(f"{m['from'].upper()}: {m['text']}" for m in ex["messages"])
        block = (
            f"<example outcome=\"{ex.get('outcome', 'unknown')}\">\n"
            f"{transcript}\n"
            f"<why_it_worked>{ex.get('why', '')}</why_it_worked>\n"
            f"</example>"
        )
        if total + len(block) > MAX_EXAMPLE_CHARS:
            break
        rendered.append(block)
        total += len(block)
    return "\n\n".join(rendered)
