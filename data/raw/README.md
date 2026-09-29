Drop your training material in this folder, then run `python -m scripts.train`.

Accepted formats: .txt, .md, .csv, .json. For Word or PDF files, export or copy them to .txt first.
It doesn't need to be tidy. Exported DMs, copy-pasted threads, your "breakdown of messages" doc,
or notes on what worked all work. The training step uses Claude to pull out the individual
conversations and whether each one booked.

If you know the outcome of a conversation, add a line like "OUTCOME: booked" or
"OUTCOME: ghosted" near it. That makes the examples much better.

Everything in this folder except this README is gitignored, so client conversations stay off GitHub.
