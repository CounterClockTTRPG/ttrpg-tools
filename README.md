# ttrpg-tools

An AD&D 2nd Edition campaign manager built as an **MCP server** for use with
Claude Code, plus a **Flask web dashboard** for viewing campaign state. The MCP
server exposes ~150 tools covering dice, party and combat tracking, world and
dungeon maps, lore, factions, survival, travel, treasure, rules lookups and
more; the dashboard renders the same data as browsable web pages (party status,
session chronicle, character sheets, maps, scene galleries).

Dungeon/combat maps are produced by the companion [`dungml`](https://github.com/CounterClockTTRPG/dungml)
service — run it alongside this if you want map rendering.

## Requirements

- Python 3.11 or newer
- An [OpenAI](https://platform.openai.com/) API key (scene/portrait text + image prompts)
- A [Replicate](https://replicate.com/) API token (portrait/scene image generation)
- *(optional)* A running `dungml` backend for dungeon/combat maps

## Install

```bash
git clone git@github.com:CounterClockTTRPG/ttrpg-tools.git
cd ttrpg-tools

# Create a virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configure

Create a `.env` file in the project root (it is git-ignored — never commit it):

```bash
OPENAI_API_KEY=sk-...
REPLICATE_API_TOKEN=r8_...

# Optional — only needed for dungml map integration:
DUNGML_API_BASE=http://127.0.0.1:8000
DUNGML_EMAIL=you@example.com
DUNGML_PASSWORD=your-dungml-password
DUNGML_PROJECT=your-project-name
```

## Run the MCP server

Register the server in Claude Code. A ready-made `.mcp.json` is included; adjust
the absolute paths to match where you cloned the repo:

```json
{
  "mcpServers": {
    "ttrpg": {
      "command": "/path/to/ttrpg-tools/.venv/bin/python3",
      "args": ["/path/to/ttrpg-tools/server.py"],
      "cwd": "/path/to/ttrpg-tools"
    }
  }
}
```

You can also run it directly to check it starts:

```bash
python3 server.py
```

## Run the dashboard

```bash
python3 dashboard.py            # http://127.0.0.1:5000
python3 dashboard.py --port 8080
python3 dashboard.py --campaign my-campaign
```

Pass `--ssl-cert FILE --ssl-key FILE` to serve over HTTPS. Routes include `/`
(scene gallery), `/party`, `/log`, `/characters`, `/locations`, `/maps`, and
`/sheets/<slug>`.

## Notes

- Campaign data lives under `campaigns/` and is git-ignored — your playthroughs,
  characters, and DM secrets stay local.
- Copyrighted reference material (rulebook text, monster manuals, the Greyhawk
  setting data) is git-ignored and regeneratable via the scripts in `tools/`.

## License

MIT — see [LICENSE](LICENSE).
