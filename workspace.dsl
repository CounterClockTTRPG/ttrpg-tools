workspace "TTRPG Campaign System" "AD&D 2nd Edition campaign toolkit: MCP server for an LLM DM, a Flask dashboard for the player, and on-disk campaign state." {

    !identifiers hierarchical

    model {

        dm     = person "Dungeon Master (LLM)" "Claude, running a session. Calls MCP tools to read state and resolve dice, combat, lore, and world updates." "External"
        player = person "Player" "Reads scenes, character sheets, maps, and the chronicle in a browser. May also speak directly to the DM." "External"

        claudeCode = softwareSystem "Claude Code" "The LLM CLI hosting the DM. Speaks MCP over stdio to the campaign server." "External"

        dungml   = softwareSystem "dungml" "Local HTTP service that renders .dmap DSL into tactical-map SVGs. Reached at http://127.0.0.1:8000/api/dsl/render." "External"
        replicate = softwareSystem "Replicate" "Hosted image-generation API. Used for portraits and scene art." "External, SaaS"
        openai    = softwareSystem "OpenAI" "Hosted text-to-speech API. Used for narration audio." "External, SaaS"

        ttrpg = softwareSystem "TTRPG Campaign System" "AD&D 2e campaign toolkit: MCP-backed rules, party/combat/world state, dashboard UI, and per-campaign storage." {

            mcpServer = container "MCP Server" "Exposes ~180 mcp__ttrpg__* tools to the LLM DM (FastMCP over stdio). Owns all campaign state writes." "Python · FastMCP" "MCP" {
                toolRegistry      = component "Tool Registry" "server.py wires each tools/*.py module's register(mcp) into FastMCP." "Python"
                campaignIO        = component "Campaign State I/O" "_campaign.py — read/write campaign.json, state.json, events.jsonl, secrets.json." "Python"
                diceTool          = component "Dice"          "roll(notation, times)" "tools/dice.py"
                partyTool         = component "Party & Characters" "add_character, party_status, get_character, refresh_sheet" "tools/party.py"
                combatTool        = component "Combat"        "start_combat, attack, apply_combat_damage, next_turn, end_combat" "tools/combat.py"
                combatMapTool     = component "Tactical Maps" "create_map (delegates to dungml), place_combatant, get_map_state" "tools/combat_map.py"
                worldTool         = component "World & Time"  "advance_time, advance_calendar, tick_world, current_weather" "tools/world.py"
                worldMapTool      = component "World Maps"    "create_world_map, add_world_map_feature, compile_world_map_view, world_map_distance_*" "tools/world_map.py"
                weatherTool       = component "Weather"       "weather_check, set_season" "tools/weather.py"
                travelTool        = component "Travel"        "travel, surprise_check, morale_check" "tools/travel.py"
                factionsTool      = component "Factions & Clocks" "add_faction, add_faction_clock, set_disposition, change_reputation" "tools/factions.py"
                questsTool        = component "Quests"        "add_quest, active_quests, complete_quest, quest_status" "tools/quests.py"
                loreTool          = component "Lore"          "search_lore, update_location, create_area, create_location" "tools/lore.py"
                logTool           = component "Chronicle Log" "log_note, party_timeline, audit_session" "tools/log.py"
                secretsTool       = component "DM Secrets"    "dm_note, dm_secrets, dm_secret_update" "tools/secrets.py"
                lookupTool        = component "Reference Lookups" "monster_lookup, spell_lookup, item_lookup, class_lookup, ability_lookup, proficiency_lookup, turning_undead, rules_lookup, rules_section" "tools/lookup.py · tools/rules.py"
                greyhawkTool      = component "Greyhawk Setting" "greyhawk_lookup, greyhawk_search, greyhawk_category, greyhawk_metadata" "tools/greyhawk.py"
                imagesTool        = component "Images"        "generate_portrait, generate_scene, regenerate_*" "tools/images.py"
                ttsTool           = component "Text-to-Speech" "Narration audio for scenes." "tools/tts.py"
                survivalTool      = component "Survival"      "add_inventory, consume, light_torch, light_state, encumbrance" "tools/survival.py"
                holdingsTool      = component "Holdings"      "add_house, add_mount, update_house, update_mount" "tools/holdings.py"
                hirelingsTool     = component "Hirelings"     "hire, loyalty_check, adjust_loyalty" "tools/hirelings.py"
                rumorsTool        = component "Rumors"        "add_rumor, list_rumors, gossip" "tools/rumors.py"
                wordlistTool      = component "Wordlist"      "random_words — session-tonal inspiration" "tools/wordlist.py"
                campaignMgmtTool  = component "Campaign Mgmt" "create_campaign, switch_campaign, list_campaigns, close_campaign, campaign_info" "tools/campaign_mgmt.py"

                toolRegistry -> campaignIO     "loads active campaign"
                toolRegistry -> diceTool       "registers"
                toolRegistry -> partyTool      "registers"
                toolRegistry -> combatTool     "registers"
                toolRegistry -> combatMapTool  "registers"
                toolRegistry -> worldTool      "registers"
                toolRegistry -> worldMapTool   "registers"
                toolRegistry -> weatherTool    "registers"
                toolRegistry -> travelTool     "registers"
                toolRegistry -> factionsTool   "registers"
                toolRegistry -> questsTool     "registers"
                toolRegistry -> loreTool       "registers"
                toolRegistry -> logTool        "registers"
                toolRegistry -> secretsTool    "registers"
                toolRegistry -> lookupTool     "registers"
                toolRegistry -> greyhawkTool   "registers"
                toolRegistry -> imagesTool     "registers"
                toolRegistry -> ttsTool        "registers"
                toolRegistry -> survivalTool   "registers"
                toolRegistry -> holdingsTool   "registers"
                toolRegistry -> hirelingsTool  "registers"
                toolRegistry -> rumorsTool     "registers"
                toolRegistry -> wordlistTool   "registers"
                toolRegistry -> campaignMgmtTool "registers"
            }

            dashboard = container "Dashboard" "Flask web app: scene gallery, party status, chronicle, character sheets, world maps, /combat tactical view, /reference rules browser." "Python · Flask · Jinja · vanilla JS" "Web"

            campaignStorage = container "Campaign Storage" "Per-campaign filesystem tree under campaigns/<slug>/: campaign.json, state.json, events.jsonl, secrets.json, characters/*.md, locations/*.md, maps/*.map, images/, audio/, combat_state.json." "Filesystem · JSON · Markdown · GeoJSON" "Storage"

            referenceDBs = container "Reference DBs" "Read-only SQLite snapshots of AD&D 2e content: monsters.db, 2e.db (classes/spells/items/proficiencies), rules.db (FTS5 PHB/DMG/MM text)." "SQLite (read-only)" "Storage"

            greyhawkDB = container "Greyhawk Setting DB" "settings/greyhawk/greyhawk.db — 3,222 wiki pages plus typed tables (deities, realms, characters, settlements, …) with FTS5 search." "SQLite (read-only)" "Storage"

            staticAssets = container "Static Assets" "static/: combat-map.js, world-map.js, textures, map-icons, play-hero.png. Served directly by the dashboard." "JS · PNG · SVG" "Web"
        }

        # -- relationships ----------------------------------------------------

        dm -> claudeCode "Issues tool calls during the session"
        claudeCode -> ttrpg.mcpServer "Invokes mcp__ttrpg__* tools" "MCP / stdio"

        player -> ttrpg.dashboard "Reads scenes, sheets, maps, chronicle" "HTTPS · browser"
        player -> dm "Speaks/types player actions"

        ttrpg.mcpServer -> ttrpg.campaignStorage "Reads/writes campaign + session state" "fs"
        ttrpg.mcpServer -> ttrpg.referenceDBs   "Looks up rules, monsters, spells, items, classes" "SQLite (RO)"
        ttrpg.mcpServer -> ttrpg.greyhawkDB     "Looks up setting lore" "SQLite (RO)"
        ttrpg.mcpServer -> dungml                "Renders .dmap tactical SVGs" "HTTP · JSON"
        ttrpg.mcpServer -> replicate             "Generates portraits & scenes" "HTTPS · REST"
        ttrpg.mcpServer -> openai                "Synthesises narration audio" "HTTPS · REST"

        ttrpg.dashboard -> ttrpg.campaignStorage "Reads campaign + session state (mtime-gated reload)" "fs"
        ttrpg.dashboard -> ttrpg.referenceDBs   "Renders /reference, /abilities, /proficiencies, /turning" "SQLite (RO)"
        ttrpg.dashboard -> ttrpg.greyhawkDB     "Renders /reference → Greyhawk Setting" "SQLite (RO)"
        ttrpg.dashboard -> ttrpg.staticAssets   "Serves JS, textures, icons" "HTTP"

        # MCP-server component-level wiring to peers --------------------------

        ttrpg.mcpServer.campaignIO       -> ttrpg.campaignStorage "Reads/writes JSON, Markdown, GeoJSON"
        ttrpg.mcpServer.combatMapTool    -> dungml                "POST /api/dsl/render"
        ttrpg.mcpServer.imagesTool       -> replicate             "Image generation"
        ttrpg.mcpServer.ttsTool          -> openai                "TTS"
        ttrpg.mcpServer.lookupTool       -> ttrpg.referenceDBs    "FTS5 + structured lookups"
        ttrpg.mcpServer.greyhawkTool     -> ttrpg.greyhawkDB      "Wiki + typed-table lookups"
        ttrpg.mcpServer.worldMapTool     -> ttrpg.campaignStorage "Reads .map DSL, writes compiled GeoJSON"
        ttrpg.mcpServer.combatTool       -> ttrpg.campaignStorage "Persists combat_state.json"
        ttrpg.mcpServer.logTool          -> ttrpg.campaignStorage "Appends events.jsonl + adventure_log.md"
        ttrpg.mcpServer.secretsTool      -> ttrpg.campaignStorage "Reads/writes secrets.json"

        # -- deployment -------------------------------------------------------

        deploymentEnvironment "Local" {
            deploymentNode "Developer Workstation" "Linux / WSL2" {
                deploymentNode "Claude Code CLI" {
                    claudeCodeInstance = softwareSystemInstance claudeCode
                }
                deploymentNode "Python 3 runtime" {
                    mcpInstance       = containerInstance ttrpg.mcpServer
                    dashboardInstance = containerInstance ttrpg.dashboard
                }
                deploymentNode "Filesystem" {
                    storageInstance   = containerInstance ttrpg.campaignStorage
                    refDbInstance     = containerInstance ttrpg.referenceDBs
                    ghDbInstance      = containerInstance ttrpg.greyhawkDB
                    staticInstance    = containerInstance ttrpg.staticAssets
                }
                deploymentNode "dungml service" "127.0.0.1:8000" {
                    dungmlInstance    = softwareSystemInstance dungml
                }
                deploymentNode "Web Browser" {
                    # player hits the dashboard from here
                }
            }
            deploymentNode "Internet" {
                deploymentNode "Replicate" {
                    replicateInstance = softwareSystemInstance replicate
                }
                deploymentNode "OpenAI" {
                    openaiInstance    = softwareSystemInstance openai
                }
            }
        }
    }

    views {

        systemContext ttrpg "Context" {
            include *
            autoLayout lr
            description "Who uses the TTRPG system, and which external services it depends on."
        }

        container ttrpg "Containers" {
            include *
            autoLayout lr
            description "Containers inside the TTRPG system and how the DM (via Claude Code) and the player reach them."
        }

        component ttrpg.mcpServer "MCPComponents" {
            include *
            autoLayout lr
            description "Tool modules inside the MCP server (server.py + tools/*.py)."
        }

        deployment ttrpg "Local" "LocalDeployment" {
            include *
            autoLayout lr
            description "Everything runs on the developer workstation; only Replicate and OpenAI are off-box."
        }

        styles {
            element "Person" {
                shape Person
                background #08427B
                color #ffffff
            }
            element "External" {
                background #999999
                color #ffffff
            }
            element "Software System" {
                background #1168BD
                color #ffffff
            }
            element "Container" {
                background #438DD5
                color #ffffff
            }
            element "Component" {
                background #85BBF0
                color #000000
            }
            element "MCP"     { background #6B3FA0 color #ffffff }
            element "Web"     { background #2E8B57 color #ffffff }
            element "Storage" { shape Cylinder background #B8860B color #ffffff }
            element "SaaS"    { background #999999 color #ffffff }
        }

        theme default
    }
}
