#!/usr/bin/env python3
"""TTRPG MCP Server — AD&D 2nd Edition campaign manager.

Register in Claude Code settings:
  {
    "mcpServers": {
      "ttrpg": {
        "command": "python3",
        "args": ["/home/raf/claude/ttrpg2/server.py"],
        "cwd": "/home/raf/claude/ttrpg2"
      }
    }
  }
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP
from tools import dice, party, combat, combat_map, map_state, world, world_map, lore, log, lookup, images, campaign_mgmt, secrets, rules, survival, factions, weather, travel, rumors, hirelings, quests, audit, holdings, greyhawk, wordlist, pre_check, lore_facts

mcp = FastMCP("TTRPG Campaign Manager")

dice.register(mcp)
party.register(mcp)
combat.register(mcp)
combat_map.register(mcp)
map_state.register(mcp)
world.register(mcp)
world_map.register(mcp)
lore.register(mcp)
log.register(mcp)
lookup.register(mcp)
images.register(mcp)
campaign_mgmt.register(mcp)
secrets.register(mcp)
rules.register(mcp)
survival.register(mcp)
factions.register(mcp)
weather.register(mcp)
travel.register(mcp)
rumors.register(mcp)
hirelings.register(mcp)
quests.register(mcp)
audit.register(mcp)
holdings.register(mcp)
greyhawk.register(mcp)
wordlist.register(mcp)
pre_check.register(mcp)
lore_facts.register(mcp)

if __name__ == "__main__":
    mcp.run()
