# World Map DSL — Implementation Plan

*Drafted: 2026-05-08. Scope-locked from analysis conversation.*

## Goal

A Structurizr-style DSL that describes a campaign world (kingdoms, cities, roads, terrain, rivers, POIs, dungeon overviews) once, then compiles into per-scope GeoJSON views for rendering in the dashboard. Distance tools (direct + via-roads) and MCP tools to create, query, and surgically edit maps round it out.

## Locked decisions

| Decision | Choice |
|---|---|
| Source of truth | DSL `.map` files. GeoJSON is derived per `view`. |
| Includes | `!include path/to/other.map` directives, inlined at preprocess. |
| Doc cross-refs | `doc locations/orlane.md` — soft link, no validation. |
| Generation | Skeleton scaffolding only (`create_map(slug)` stamps empty workspace). No LLM/procedural in v1. |
| Scales in v1 | World/regional, city layout, dungeon overview. No building interior (combat_map handles that). |
| Party POI | Explicit `place_party_on_map(slug, x, y)` tool. Not auto-derived from state. |
| Authoring path | Claude generates DSL; humans view it. No round-trip serializer needed; surgical edits are text-level append/delete. |
| Coordinate system | Leaflet `L.CRS.Simple`. Units per-view (`miles` for world, `feet` for dungeon). |
| Storage | `campaigns/<name>/maps/<slug>.map` + derived `<slug>.<view>.geojson` cache. |
| Building/room dimensions | Either `size w,h` (axis-aligned rectangle, anchored at `at`) OR `polygon …` for non-rectangular shapes. |
| Include scope | `!include path` is a true preprocessor directive — valid anywhere a statement is valid (top-level, inside `model`, inside `contains`, inside `views`). |
| Generic feature properties | Any feature can carry `description "..."` (free text) and `tags [a, b, c]` (array of identifiers). Both surface as GeoJSON feature properties. |
| Look-and-feel | `styles { … }` block at workspace level sets defaults; the same block inside a `view { … }` overrides for that view only. Selectors: by kind, kind+property match, or tag. |
| Terrain extensibility | `terrain` carries a free-form `biome` identifier. A built-in style table covers common biomes (marsh, desert, forest, hills, plains, mountain, tundra, jungle); workspaces and views add or override entries via `styles`. |

## DSL grammar (target)

Block-based syntax, same family as `tools/combat_map.py`. Whitespace-insensitive; `#` comments; identifiers are `[a-z][a-z0-9-]*`.

```
!include shared/sheldomar-base.map        # preprocessor: inlines file in place

workspace sheldomar {

    # --- Style defaults for the whole workspace ------------------------------
    styles {
        terrain.biome=marsh    { color #4a6b3a; fill-opacity 0.4 }
        terrain.biome=forest   { color #2d5a2d; fill-opacity 0.5 }
        terrain.biome=hills    { color #8a7350; fill-opacity 0.3 }
        terrain.biome=desert   { color #d4a25a; fill-opacity 0.4 }
        terrain.biome=mountain { color #6e6e6e; fill-opacity 0.5 }
        city                   { marker circle; color #222; marker-size 6 }
        [walled]               { stroke #444;   stroke-width 2 }
        road.surface=paved     { stroke #555;   stroke-width 3 }
        road.surface=dirt      { stroke #8a6b3a; stroke-width 2 }
        river                  { stroke #3a6da3; stroke-width 2 }
    }

    model {
        kingdom geoff   { color #6b7c4a; capital hochoch
                          description "Frontier duchy on the Sheldomar's western edge." }
        kingdom keoland { color #b08040 }

        city hochoch {
            at  412,308
            pop 2400
            in  geoff
            tags [walled, trade, militia]
            description "Frontier town, staging post for caravans south."
            doc locations/hochoch.md
        }

        city orlane {
            at  398,224
            pop 100
            in  geoff
            tags [farming, troubled]
            description "Farming village in the southern Sheldomar."
            doc locations/orlane.md
            contains {
                !include orlane-buildings.map         # nested include — buildings live elsewhere
                street main-road   { points 0,10 30,10 }
                street temple-lane { points 18,10 18,4 }
            }
        }

        road south-trade-track {
            from hochoch
            to   orlane
            via  410,260 405,240
            surface dirt
            days 4
        }

        river javan { points 380,400 385,380 390,360 }

        terrain rushmoors {
            polygon 350,200 380,180 390,260 360,280
            biome marsh
            description "Trackless marshland west of Orlane."
        }

        terrain dim-forest {
            polygon 380,320 420,340 415,380 375,360
            biome forest
        }

        poi crocodile-attack {
            at 372,250
            kind event
            tags [hazard, recent]
            description "Caravan lost wagons here, week 3."
        }

        dungeon temple-cellars {
            in orlane.temple-merikka
            room antechamber { at 0,0 size 4,4 }
            room pit-chamber { at 4,0 size 6,6; tags [hazard]; description "Open pit, 20ft deep." }
            passage { from antechamber to pit-chamber }
        }
    }

    views {
        view world {
            scope sheldomar
            include kingdoms, cities, roads, rivers, terrain, poi
            units  miles
            # Per-view override: this view paints deserts brighter than the workspace default.
            styles {
                terrain.biome=desert { color #f0c070 }
                city.pop>1000        { marker circle; marker-size 10; color #b00 }
            }
        }
        view orlane-city    { scope orlane;         include buildings, streets, poi; units feet }
        view temple-cellars { scope temple-cellars; include rooms, passages;         units feet }
    }
}
```

`orlane-buildings.map` (separate file, included into the `contains` block above):

```
building golden-grain-inn { at 12,8;  size 6,4;  type inn;       tags [cult-hq]; description "Whitewashed two-storey inn." }
building temple-merikka   { at 18,6;  size 8,12; type temple;    tags [walled];  description "Granite walls, oak gates." }
building mayors-cottage   { at 22,4;  size 5,4;  type residence }
building blacksmith       { at 8,15;  size 6,5;  type forge;     tags [hostile] }
building general-store    { at 14,15; polygon 0,0 8,0 8,5 4,7 0,5; type shop }
```

**Property syntax:** `key value` inside braces. Values: numbers, identifiers, `#hexcolor`, `"quoted strings"`, comma-separated number pairs (coords), space-separated coord lists, `[a, b, c]` arrays for tags.

**Generic properties allowed on every feature:** `description "..."`, `tags [a, b, c]`, `doc <path>` (soft link). Surface as GeoJSON feature properties unchanged.

**Reserved feature kinds:** `kingdom`, `city`, `road`, `river`, `lake`, `terrain`, `poi`, `building`, `street`, `dungeon`, `room`, `passage`. Each maps to a GeoJSON Feature with a known geometry type:

| DSL kind | GeoJSON geometry | Notes |
|---|---|---|
| city, poi | Point | `at x,y` |
| building, room | Point + size box → Polygon, OR explicit `polygon …` | `size w,h` is sugar for an axis-aligned rectangle anchored at `at` |
| road, river, street, passage | LineString | from `points`, or `from/to/via` for roads |
| kingdom, terrain, lake | Polygon (or MultiPolygon for `kingdom`) | from `polygon` or implied by contained features |
| dungeon | none — container only, defines coordinate frame for child rooms | |

## Style cascade

Every style block is a flat list of `selector { properties }` rules. Selectors:

- **By kind:** `city { … }` — applies to all features of that kind.
- **By kind + property match:** `terrain.biome=forest { … }`, `city.pop>1000 { … }`. Operators: `=`, `!=`, `>`, `<`, `>=`, `<=`. RHS is a literal.
- **By tag:** `[walled] { … }` matches any feature whose `tags` array contains that identifier.

Resolution order at compile time, last wins:

1. Built-in defaults (compiled into `tools/world_map.py`).
2. Workspace-level `styles { … }` block.
3. View-level `styles { … }` block (overrides workspace defaults for that view only).

The compiler resolves styles at view-compile time and writes the resolved style attributes onto each GeoJSON feature's properties (`_style: { color, marker, … }`). The dashboard renderer reads `_style` directly — no client-side cascade.

**Style attributes shipped in v1:** `color`, `stroke`, `stroke-width`, `fill-opacity`, `marker` (`circle | square | diamond | pin`), `marker-size`. Icons deferred.

**Adding a new terrain biome** (e.g. `tundra`) is a one-liner — declare it on a `terrain` feature, add a `terrain.biome=tundra { color … }` rule to the workspace `styles` block, done. No code change.

## File layout

```
tools/
  world_map.py          # parser, compiler, MCP tools — single module ~700 LoC
campaigns/<name>/maps/
  <slug>.map            # canonical DSL
  <slug>.<view>.geojson # derived, regenerated on DSL mtime change
  <slug>.party.geojson  # party overlay, written by place_party_on_map
static/
  maps.js               # Leaflet renderer (Simple CRS, per-view units)
  maps.css
templates/              # if Flask templates exist; otherwise inline in dashboard.py
  maps.html
```

Routing graph is held in memory (rebuilt on DSL change), not persisted.

## Module breakdown — `tools/world_map.py`

| Section | Responsibility | Approx LoC |
|---|---|---|
| `_preprocess(text, base_dir)` | Resolve `!include` directives anywhere a statement is valid (top-level or inside any block). Recursive with cycle detection, max depth 8. Returns flat text + line-origin map for error messages. Tracks the manifest of all sourced files for cache invalidation. | 100 |
| `_tokenize(text)` | Stream of tokens: `LBRACE`, `RBRACE`, `LBRACKET`, `RBRACKET`, `IDENT`, `NUMBER`, `STRING`, `HEX`, `COMMA`, `OP` (`= != > < >= <=`), `NEWLINE`. Skips comments. | 110 |
| `_parse(tokens)` | Recursive-descent into AST: `Workspace { styles, model: {features}, views: [{styles, …}] }`. First pass records identifiers; second pass resolves `from/to/in` references. | 220 |
| `_resolve_styles(workspace, view)` | Cascade defaults → workspace.styles → view.styles. For each feature, evaluate selectors in order, last match wins. Emits a resolved `_style` dict per feature. | 80 |
| `_compile_view(workspace, view_name)` | Walks model filtered by view's `scope` + `include`. Builds geometry (Point, LineString, Polygon — including `size`-to-rectangle expansion for buildings/rooms). Attaches resolved `_style`, `description`, `tags`. Emits GeoJSON FeatureCollection. | 180 |
| `_build_routing_graph(workspace)` | NetworkX `Graph`. Nodes = cities + waypoints. Edges = road segments. Edge weight = Euclidean length × surface multiplier. | 60 |
| `_distance_direct(p1, p2)` | Euclidean in view's coordinate frame. | 10 |
| `_distance_via_roads(graph, a, b)` | `nx.shortest_path_length(weight="distance")`. Returns total distance + path of node names. | 15 |
| `_cache_get(slug, view)` | mtime check vs every file in the include manifest. Regen on any change. Atomic write (`.tmp` + rename). | 50 |
| MCP tool definitions | `create_map`, `get_map`, `update_map`, `list_views`, `compile_view`, `add_map_feature`, `remove_map_feature`, `distance_direct`, `distance_via_roads`, `nearest`, `place_party_on_map` | 200 |

Total target: ~1025 LoC for the module. Test coverage: parser fixtures + golden GeoJSON files for each view kind, plus style-cascade unit tests.

## MCP tool surface

| Tool | Signature | Notes |
|---|---|---|
| `create_map` | `(slug)` | Stamps empty workspace skeleton. Errors if file exists. |
| `get_map` | `(slug, format='dsl'\|'geojson', view=None)` | Returns DSL text, or compiled GeoJSON for the named view. |
| `update_map` | `(slug, dsl)` | Overwrites `.map`, invalidates all view caches. |
| `list_views` | `(slug)` | Returns view names + their `scope` + `include`. |
| `compile_view` | `(slug, view)` | Force-recompile (bypasses mtime cache). For debugging. |
| `add_map_feature` | `(slug, kind, body)` | Appends a feature block to the model section. Text-level edit; re-parses to validate before write. |
| `remove_map_feature` | `(slug, ident)` | Removes feature by identifier. Errors if other features reference it (e.g., road `to` an unknown city). |
| `distance_direct` | `(slug, view, a, b)` | a and b are identifiers OR `[x,y]` literals. |
| `distance_via_roads` | `(slug, a, b)` | Identifiers only. Returns distance + path. |
| `nearest` | `(slug, point, kind, n=1)` | Finds nearest features of given kind to a point. |
| `place_party_on_map` | `(slug, x, y, view='world')` | Writes `<slug>.party.geojson` overlay. Single-feature FeatureCollection. |

## Dashboard

- `/maps` — index lists all `.map` files in current campaign with their views.
- `/maps/<slug>/<view>` — Leaflet view, Simple CRS, per-view units shown in scale control.
- Layers (toggleable): kingdoms, terrain, rivers, roads, cities, POIs, party-overlay (loaded from `<slug>.party.geojson` if present).
- Click a city → sidebar with `pop`, `kind`, `note` properties; if `doc` link present, link to existing dashboard location route.
- No editing in the UI. View-only.

## Order of work

1. **Parser & compiler skeleton.** `_preprocess` (with nested-include support), `_tokenize`, `_parse`, AST types including `tags`, `description`, and `styles` blocks. Test fixtures for grammar coverage. *No MCP yet, no rendering.*
2. **Style cascade.** `_resolve_styles` with built-in defaults + workspace + view layering. Selector evaluation (kind, kind+property, tag). Unit tests for cascade order.
3. **Compiler → GeoJSON.** Implement `_compile_view` for `world` scope first (Point/LineString/Polygon emission, `size`-to-rectangle sugar, resolved `_style` attached). Golden-file tests.
4. **Cache layer + first MCP tools.** `create_map`, `get_map`, `update_map`, `list_views`, `compile_view`. Manifest-based cache invalidation. Integration test from MCP boundary.
5. **City + dungeon scope compilation.** Extend compiler for `contains` blocks (with nested includes) and `dungeon` containers. More golden files.
6. **Surgical edits.** `add_map_feature`, `remove_map_feature` — text append/delete with re-parse validation.
7. **Routing graph + distance.** NetworkX integration. `distance_direct`, `distance_via_roads`, `nearest`.
8. **Dashboard renderer.** Leaflet view, layer toggles, click-to-inspect, `_style` consumption (color/marker/stroke). Static assets.
9. **Party overlay.** `place_party_on_map` + overlay layer in renderer.
10. **Docs.** Update `CLAUDE.md` with map tools section so the DM (Claude) knows when to use them.

Each step has independent end-state; each commit can stand alone.

## Out of scope (deferred)

- Procedural generation (Voronoi kingdoms, river simulation, settlement seeding).
- LLM-prompted generation tools (`generate_kingdom`, etc.).
- Building-interior scale (rooms inside a single building) — `combat_map` already covers this.
- Round-trip pretty-printer (DSL → AST → DSL). Not needed since edits are text-level append/delete.
- Map editing in the dashboard UI.
- DSL → SVG export, image export.
- Multi-campaign or shared-world maps. Each campaign has its own maps dir.
- Validation of `doc` paths (soft links by design).

## Risks

- **Parser scope creep.** Block grammars look easy until quoted strings and nested `contains` blocks meet user-supplied content. Lock the grammar early; reject ambiguity rather than guess.
- **GeoJSON validation.** Pipe everything through `geojson-pydantic` or a hand-rolled validator at write time. Corrupt cache files are miserable to debug.
- **Reference resolution edge cases.** A road `to nonexistent-city` should fail at parse time with a useful line number, not silently emit broken GeoJSON.
- **Coordinate-system surprises in Leaflet.** `CRS.Simple` uses `[y, x]` not `[x, y]`. Document the convention once, enforce in the compiler so the DSL stays `x,y`.
- **Cache staleness with `!include`.** Cache must invalidate when *any* included file changes, not just the root. Use a manifest of all sourced files + their mtimes per view.
- **Drift between DSL and `locations/` markdown.** Soft links by design — but a `verify_map(slug)` lint that flags `doc` paths pointing to missing files is a cheap safety net. Add post-v1 if drift becomes annoying.
- **Style selector ambiguity.** Two rules can match the same feature (`city` and `[walled]`). Resolve by source order — last wins inside a layer; layers stack defaults < workspace < view. Document this clearly so authoring is predictable.
- **Tag explosion.** Tags are free-form identifiers — easy to typo (`walled` vs `walls`). Add a `verify_map` lint that lists all tags in use; let the user spot anomalies. Don't enforce a closed set in v1.
