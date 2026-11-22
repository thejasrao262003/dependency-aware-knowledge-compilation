# Recording script: DAKC in under 3 minutes

A screen recording built from this example. It uses two windows side by side:
a terminal and Claude Code. The numbers quoted are the real ones from
[results/walkthrough.md](results/walkthrough.md).

## Before you record (about 5 minutes, off camera)

```bash
pip install 'dakc[mcp]'
python examples/fastapi-docs/walkthrough.py                  # clones FastAPI's docs into .work/
cd examples/fastapi-docs/.work
git -C fastapi checkout -q b0a616d9^                         # the docs just before the featured commit
rm -rf demo-kb && mkdir demo-kb
printf '[dakc]\nsource_dir = "../fastapi/docs/en/docs"\nprovider = "mock"\n' > demo-kb/dakc.toml
dakc compile -w demo-kb > /dev/null                          # build 1: about 4 seconds
claude mcp add fastapi-kb -- dakc mcp --workspace "$PWD/demo-kb"
```

The `mock` provider produces the same knowledge as the free default provider,
but it counts every call an LLM would receive. That makes the cost visible on
screen. Use a large terminal font, and clear the screen before each scene.

## Scenes

**0:00–0:15. The problem** (title card or voice-over)
> "Docs change every day. An AI-generated knowledge base built from them has
> to be rebuilt on every edit, which is expensive, or it drifts out of date.
> DAKC rebuilds only what an edit invalidates, and proves the result is
> correct."

**0:15–0:35. The knowledge base**
```bash
dakc status -w demo-kb
```
> "These are FastAPI's 159 documentation pages, compiled into 11,000 facts and
> 2,300 entity pages. A full build is about 13,800 LLM calls."

**0:35–1:00. Ask the agent** (Claude Code)
> *What happens when a form upload exceeds FastAPI's parsing limits, and how
> can I limit the number of uploaded files?*

Let it answer. It says the docs cover body-size limits but nothing about
limiting the number of files.

**1:00–1:15. The docs change**
```bash
git -C fastapi checkout -q b0a616d9      # a real FastAPI commit: "Add configurable form parsing limits"
dakc check -w demo-kb
```
> "One real commit: sixteen lines added to one page."

**1:15–1:45. The key moment: what has to be rebuilt?**
```bash
dakc compile --dry-run -w demo-kb
```
Point at these lines:

- `Passages to (re-)extract: 8`
- `Affected Knowledge Pages: 9`
- `Unaffected Knowledge Pages: 2327`
- `Estimated LLM Calls: 20 (exact)`

> "Twenty LLM calls instead of thirteen thousand eight hundred. Eight
> paragraphs, nine pages, and nothing else is touched. And DAKC shows this
> before spending anything."

Optionally, cut to the diagram in `results/walkthrough.md`.

**1:45–2:00. Compile**
```bash
dakc compile -w demo-kb
```

**2:00–2:30. Ask again** (Claude Code)
> *I just pulled the latest docs. Is the knowledge base up to date? Then answer
> the question again, with sources.*

The agent previews and compiles, or confirms the knowledge base is current.
It then answers with `max_files` (default 1,000) and "Exceeding a parser limit
returns HTTP 400", citing `advanced/request-body-limits.md`. Ask it to trace
the answer if it doesn't.

**2:30–2:50. Proof**
```bash
dakc verify --against-full -w demo-kb
```
> "And the incremental result is identical to a full rebuild. Replayed over
> FastAPI's last fifty documentation commits, that held at every single one."

**2:50–3:00. Close**
> "`pip install dakc`. It's open source, Apache 2.0. Links below."

## Tips

- If an agent answer runs long, cut it in editing rather than re-asking.
  Agent wording varies from run to run.
- `results/agent-session.md` shows what a typical session looks like, so you
  know what to expect.
- Remove the registration afterwards: `claude mcp remove fastapi-kb`.
