"""Trains the setter from your real DM history.

    python -m scripts.train              # extract conversations → pick examples → write the playbook
    python -m scripts.train --skip-extract   # reuse data/conversations.jsonl and just rebuild the rest

Steps:
1. Extract: reads everything in data/raw/ (.txt .md .csv .json .pdf) and uses Claude to pull out each
   individual lead conversation, anonymised, with its outcome and a quality score.
2. Examples: the best conversations that ended in a booked call go to data/examples.jsonl. The bot
   sees these as few-shot references.
3. Playbook: Claude studies every conversation, won and lost, and writes knowledge/playbook.md: what
   actually gets people to book, how objections were handled, and the patterns in the ones that went cold.

Then restart the server (or POST /admin/reload) to put the changes live.
"""

import argparse
import base64
import json
from pathlib import Path

import anthropic

from setter.config import ROOT, settings

RAW_DIR = ROOT / "data" / "raw"
CONVERSATIONS_PATH = ROOT / "data" / "conversations.jsonl"
PLAYBOOK_PATH = ROOT / "knowledge" / "playbook.md"
CHUNK_CHARS = 150_000
MAX_EXAMPLES = 12

CONVERSATION_SCHEMA = {
    "type": "object",
    "properties": {
        "conversations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "messages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {"type": "string", "enum": ["lead", "setter"]},
                                "text": {"type": "string"},
                            },
                            "required": ["from", "text"],
                            "additionalProperties": False,
                        },
                    },
                    "outcome": {"type": "string", "enum": ["booked", "not_booked", "existing_client", "unknown"]},
                    "quality": {"type": "integer", "description": "1-5: how well the setter ran it against the manual"},
                    "why": {"type": "string", "description": "One or two sentences on what made it work or fail."},
                },
                "required": ["messages", "outcome", "quality", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["conversations"],
    "additionalProperties": False,
}

EXTRACT_PROMPT = """\
Below is raw material exported from a fitness coaching business's DMs. It could be chat exports, \
copy-pasted threads, or a document that quotes conversations.

Pull out every separate conversation between a lead or client and the business's setter or coach. \
Only include real back-and-forth conversations. Skip style guides, rules and summaries with no actual \
conversation in them.

For each conversation:
- Keep the messages in order and keep the wording exactly, but anonymise it. Replace people's names with \
"mate" or leave them out, and remove phone numbers, emails, @handles, and any specific health or medical \
details.
- "setter" is anyone on the business side, and "lead" is the prospect or client.
- outcome: "booked" if they booked or agreed to a call, "not_booked" if it went cold or they said no, \
"existing_client" if it's coaching an existing client rather than setting, otherwise "unknown". \
Respect any explicit OUTCOME: markers in the material.
- quality: 1-5, how well the setter ran it against the manual below.
- why: one or two sentences.

If there are no conversations, return an empty list.

<setter_manual>
{manual}
</setter_manual>
"""

PLAYBOOK_PROMPT = """\
You're studying real Instagram DM conversations from a coaching business to train a new setter. \
The setter already follows the manual below. Your job is to write the LEARNED PLAYBOOK: what these \
specific conversations show that the manual doesn't already say.

Write it in markdown, under 1,200 words, with these sections:
## What gets people to book
## Openers that got replies
## Common objections and hesitations, and what worked
## Where conversations went cold (and what to do instead)
## Phrases and patterns to reuse
## Things to avoid

Be concrete. Quote short, anonymised lines from the conversations as examples. Don't repeat generic \
advice already in the manual. Don't include pricing figures, medical details, or anything that identifies \
a person.

<setter_manual>
{manual}
</setter_manual>

<conversations>
{conversations}
</conversations>
"""


def call_claude(client: anthropic.Anthropic, content: list[dict] | str, schema: dict | None = None) -> str:
    output_config: dict = {"effort": "high"}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    with client.beta.messages.stream(
        model=settings.model,
        max_tokens=64000,
        messages=[{"role": "user", "content": content}],
        thinking={"type": "adaptive"},
        output_config=output_config,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request")
    if message.stop_reason == "max_tokens":
        raise RuntimeError("Output was cut off. Split the source file into smaller files")
    return "".join(b.text for b in message.content if b.type == "text")


def raw_inputs() -> list[tuple[str, list[dict]]]:
    """Yields (label, content blocks) for each file or text chunk in data/raw/."""
    inputs = []
    for path in sorted(RAW_DIR.iterdir()):
        if path.name == "README.md" or path.name.startswith("."):
            continue
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            data = base64.standard_b64encode(path.read_bytes()).decode()
            inputs.append((path.name, [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}]))
        elif suffix in {".txt", ".md", ".csv", ".json"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            for i in range(0, len(text), CHUNK_CHARS):
                chunk = text[i : i + CHUNK_CHARS]
                inputs.append((f"{path.name}#{i // CHUNK_CHARS + 1}", [{"type": "text", "text": f"<material>\n{chunk}\n</material>"}]))
        else:
            print(f"  skipping {path.name} (unsupported type; export it as .txt or .pdf)")
    return inputs


def extract(client: anthropic.Anthropic, manual: str) -> list[dict]:
    conversations = []
    for label, blocks in raw_inputs():
        print(f"  extracting {label} ...")
        out = call_claude(client, blocks + [{"type": "text", "text": EXTRACT_PROMPT.format(manual=manual)}], CONVERSATION_SCHEMA)
        found = json.loads(out)["conversations"]
        for c in found:
            c["source"] = label
        print(f"    {len(found)} conversations")
        conversations.extend(found)
    return conversations


def pick_examples(conversations: list[dict]) -> list[dict]:
    booked = [c for c in conversations if c["outcome"] == "booked" and c["quality"] >= 4 and len(c["messages"]) >= 4]
    booked.sort(key=lambda c: (-c["quality"], len(c["messages"])))
    return booked[:MAX_EXAMPLES]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-extract", action="store_true", help="reuse data/conversations.jsonl")
    args = parser.parse_args()

    client = anthropic.Anthropic()
    manual = "\n\n".join((ROOT / "knowledge" / f).read_text(encoding="utf-8") for f in ("script.md", "voice.md"))

    if args.skip_extract:
        conversations = [json.loads(l) for l in CONVERSATIONS_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    else:
        print("1/3 Extracting conversations from data/raw/")
        conversations = extract(client, manual)
        write_jsonl(CONVERSATIONS_PATH, conversations)
    if not conversations:
        print("No conversations found. Add DM exports to data/raw/ and run again.")
        return

    counts = {}
    for c in conversations:
        counts[c["outcome"]] = counts.get(c["outcome"], 0) + 1
    print(f"   {len(conversations)} conversations: {counts}")

    print("2/3 Picking examples")
    examples = pick_examples(conversations)
    write_jsonl(settings.examples_path, examples)
    print(f"   {len(examples)} examples → {settings.examples_path.relative_to(ROOT)}")

    print("3/3 Writing the learned playbook")
    rendered = "\n\n".join(
        f"<conversation outcome=\"{c['outcome']}\" quality=\"{c['quality']}\">\n"
        + "\n".join(f"{m['from'].upper()}: {m['text']}" for m in c["messages"])
        + "\n</conversation>"
        for c in conversations
        if c["outcome"] != "existing_client"
    )
    if not rendered:
        print("   no sales conversations to learn from (only existing-client chats), so the playbook is unchanged")
        return
    playbook = call_claude(client, PLAYBOOK_PROMPT.format(manual=manual, conversations=rendered))
    PLAYBOOK_PATH.write_text(
        "<!-- Generated by `python -m scripts.train`. Review it, then edit freely. Rerunning training overwrites it. -->\n\n"
        + playbook.strip()
        + "\n",
        encoding="utf-8",
    )
    print(f"   → {PLAYBOOK_PATH.relative_to(ROOT)}")
    print("Done. Review the playbook, then restart the server or POST /admin/reload.")


if __name__ == "__main__":
    main()
