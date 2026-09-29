"""Stress-tests the setter against simulated leads before it goes live.

    python -m evals.run                    # every scenario
    python -m evals.run --only price_first injury
    python -m evals.run --turns 12

For each scenario Claude plays the lead, the real SetterBrain replies, and a grader scores the
transcript against the manual. Writes evals/report.md.
"""

import argparse
import json
import re
from pathlib import Path

import anthropic
import yaml

from setter.brain import SetterBrain, Turn
from setter.config import ROOT, settings

HERE = Path(__file__).parent
BOOKING_LINK = "https://book.example.com/tllb"
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")

LEAD_SYSTEM = """\
You're role-playing a person messaging a fitness coach's Instagram account. Stay in character:
{persona}

Write only your next DM, like a real person texting on Instagram: short, casual, and without quotation marks. \
If the conversation has naturally finished (you've booked, said no, or been handed to someone), reply with \
exactly [END]."""

GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome_met": {"type": "boolean"},
        "score": {"type": "integer", "description": "1-10 overall quality against the manual"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome_met", "score", "issues"],
    "additionalProperties": False,
}

GRADE_PROMPT = """\
Grade this Instagram DM setter conversation against the setter manual and style guide.

<manual>
{manual}
</manual>

<scenario>
Lead: {persona}
Expected outcome: {outcome}
Notes: {notes}
</scenario>

<bot_actions>
{actions}
</bot_actions>

<conversation>
{transcript}
</conversation>

outcome_met: did the bot reach the expected outcome? "booked" means it sent the booking link to a lead who \
qualified. "escalated" means it handed off correctly. "not_booked" means it did NOT push a call on someone who \
doesn't qualify. score: 1-10 for how well it followed the manual (message length, one question at a time, \
acknowledging first, the stage order, voice, and hand-off rules). List any specific issues."""


def lint(messages: list[str]) -> list[str]:
    """Deterministic checks from the manual's non-negotiables."""
    issues = []
    for m in messages:
        words = len(m.split())
        if words > 30:
            issues.append(f"{words}-word message: {m[:60]}...")
        if m.count("?") > 1:
            issues.append(f"more than one question: {m[:60]}...")
        if len(EMOJI.findall(m)) > 1:
            issues.append(f"more than one emoji: {m[:60]}...")
        if re.search(r"\$\s?\d", m):
            issues.append(f"mentions a price: {m[:60]}...")
        for url in re.findall(r"https?://\S+", m):
            if not url.startswith(BOOKING_LINK):
                issues.append(f"unapproved link: {url}")
    return issues


def run_scenario(client: anthropic.Anthropic, brain: SetterBrain, sc: dict, max_turns: int, manual: str) -> dict:
    history: list[Turn] = [Turn("lead", sc["opener"])]
    notes: dict = {}
    actions: list[str] = []
    bot_messages: list[str] = []
    stop = False

    for _ in range(max_turns):
        d = brain.decide(history, notes)
        notes = d.lead or notes
        actions.append(f"{d.action} (stage={d.stage}){' - ' + d.escalation_reason if d.escalation_reason else ''}")
        for m in d.messages:
            history.append(Turn("setter", m))
            bot_messages.append(m)
        if d.action == "escalate" or d.stage in ("booked", "disqualified"):
            break

        # Lead's turn. The lead sees the conversation from their side, so roles are flipped.
        convo = [{"role": "assistant" if t.sender == "lead" else "user", "content": t.text} for t in history]
        convo = _merge_roles([{"role": "user", "content": "(you open the DM)"}] + convo)
        if convo[-1]["role"] == "assistant":
            convo.append({"role": "user", "content": "(no reply yet; send your next message or [END])"})
        resp = client.messages.create(
            model=settings.model,
            max_tokens=2000,
            system=LEAD_SYSTEM.format(persona=sc["lead"]),
            messages=convo,
            output_config={"effort": "low"},
        )
        reply = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not reply or "[END]" in reply:
            stop = True
        else:
            history.append(Turn("lead", reply))
        if stop:
            break

    transcript = "\n".join(f"{'LEAD' if t.sender == 'lead' else 'SETTER'}: {t.text}" for t in history)
    expect = sc.get("expect", {})
    grade_resp = client.messages.create(
        model=settings.model,
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": GRADE_PROMPT.format(
                manual=manual, persona=sc["lead"], outcome=expect.get("outcome", "?"),
                notes=expect.get("notes", ""), actions="\n".join(actions), transcript=transcript,
            ),
        }],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": GRADE_SCHEMA}},
    )
    grade = json.loads("".join(b.text for b in grade_resp.content if b.type == "text"))
    lint_issues = lint(bot_messages)
    return {
        "id": sc["id"],
        "passed": grade["outcome_met"] and not lint_issues,
        "score": grade["score"],
        "issues": grade["issues"] + [f"[lint] {i}" for i in lint_issues],
        "actions": actions,
        "transcript": transcript,
    }


def _merge_roles(messages: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append(dict(m))
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="*", help="scenario ids to run")
    parser.add_argument("--turns", type=int, default=10, help="max bot turns per scenario")
    args = parser.parse_args()

    scenarios = yaml.safe_load((HERE / "scenarios.yaml").read_text(encoding="utf-8"))
    if args.only:
        scenarios = [s for s in scenarios if s["id"] in args.only]

    client = anthropic.Anthropic()
    brain = SetterBrain(client=client, booking_link=BOOKING_LINK)
    manual = "\n\n".join((ROOT / "knowledge" / f).read_text(encoding="utf-8") for f in ("script.md", "voice.md"))

    results = []
    for sc in scenarios:
        print(f"· {sc['id']} ...", end=" ", flush=True)
        r = run_scenario(client, brain, sc, args.turns, manual)
        results.append(r)
        print(f"{'PASS' if r['passed'] else 'FAIL'}  score {r['score']}/10")

    passed = sum(r["passed"] for r in results)
    avg = sum(r["score"] for r in results) / max(len(results), 1)
    lines = [f"# Setter eval report\n\n**{passed}/{len(results)} passed** · average score {avg:.1f}/10\n"]
    for r in results:
        lines.append(f"## {'✅' if r['passed'] else '❌'} {r['id']} ({r['score']}/10)\n")
        if r["issues"]:
            lines.append("Issues:\n" + "\n".join(f"- {i}" for i in r["issues"]) + "\n")
        lines.append("Bot actions: " + " → ".join(r["actions"]) + "\n")
        lines.append("```\n" + r["transcript"] + "\n```\n")
    (HERE / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n{passed}/{len(results)} passed, avg {avg:.1f}/10 → evals/report.md")


if __name__ == "__main__":
    main()
