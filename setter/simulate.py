"""Chat with the setter in your terminal, playing the lead. Nothing is sent to Instagram.

    python -m setter.simulate
    python -m setter.simulate --name Jake

Commands: /reset starts over, /notes shows the saved lead notes, /quit exits.
"""

import argparse

from .brain import SetterBrain, Turn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="", help="the lead's first name, as GHL would know it")
    parser.add_argument("--booking-link", default="https://example.com/book", help="link to use in the simulator")
    args = parser.parse_args()

    brain = SetterBrain(booking_link=args.booking_link)
    history: list[Turn] = []
    notes: dict = {}
    print("You're the lead. Type a DM (/reset, /notes, /quit).\n")

    while True:
        try:
            text = input("LEAD > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/reset":
            history, notes = [], {}
            print("-- new conversation --\n")
            continue
        if text == "/notes":
            print(notes, "\n")
            continue

        history.append(Turn("lead", text))
        d = brain.decide(history, notes, args.name)
        notes = d.lead or notes
        for m in d.messages:
            history.append(Turn("setter", m))
            print(f"SETTER > {m}")
        info = f"[{d.action} · stage={d.stage}]"
        if d.escalation_reason:
            info += f" handoff: {d.escalation_reason}"
        print(f"  {info}\n")


if __name__ == "__main__":
    main()
