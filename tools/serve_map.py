#!/usr/bin/env python3
"""Standalone Leaflet server for a single world-map view.

Compiles a `.map` DSL file's view to GeoJSON (via tools/world_map.py) and serves
it as a self-contained Leaflet page — independent of the dashboard and of any
"active campaign". Recompiles on every request, so editing the .map and
refreshing the browser shows the change.

Usage:
    python3 tools/serve_map.py [--map PATH] [--view NAME] [--port N] [--host H]

Defaults to the WC1 Cantona city view.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))          # so world_map's `import _campaign` resolves
from tools import world_map as wm  # noqa: E402

from flask import Flask, Response, abort  # noqa: E402

app = Flask(__name__)
CFG = {"map": None, "view": None}  # filled in by main()


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
 integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
 integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<style>
  html,body{{margin:0;height:100%;background:#15110c;font-family:Georgia,serif;color:#e8dcc0}}
  #wrap{{display:flex;flex-direction:column;height:100%}}
  header{{padding:8px 16px;background:#241a10;border-bottom:1px solid #4a3828}}
  header h1{{margin:0;font-size:1.1em;color:#e0c060;letter-spacing:.04em}}
  header span{{color:#9a8a68;font-size:.8em}}
  #map{{flex:1;background:#efe6cf}}
  .leaflet-tooltip.lbl{{background:none;border:none;box-shadow:none;color:#1a120a;
     font-weight:bold;font-size:13px;text-shadow:0 0 3px #efe6cf,0 0 2px #efe6cf;padding:0}}
  .leaflet-tooltip.lbl-sm{{font-weight:normal;font-size:11px}}
  .leaflet-popup-content{{color:#1a1510;font-family:Georgia,serif}}
  .legend{{background:rgba(36,26,16,.92);color:#e8dcc0;padding:8px 10px;border:1px solid #4a3828;
     border-radius:5px;font-size:12px;line-height:1.5}}
  .legend i{{display:inline-block;width:12px;height:12px;margin-right:6px;border:1px solid #3a2a1a;vertical-align:-1px}}
</style></head>
<body><div id="wrap">
<header><h1>{title}</h1> <span>{subtitle}</span></header>
<div id="map"></div></div>
<script>
const GEO = {geojson};
const map = L.map('map', {{ crs: L.CRS.Simple, zoomSnap: 0.25, minZoom: -5 }});
const coordsToLatLng = c => L.latLng(-c[1], c[0]);   // author y is UP

function style(f){{
  const s = (f.properties && f.properties._style) || {{}};
  return {{ color: s.stroke || s.color || '#5a4632',
           weight: s['stroke-width'] || 1,
           fillColor: s.color || '#888',
           fillOpacity: (s['fill-opacity'] != null ? s['fill-opacity'] : 0.5) }};
}}
function pointToLayer(f, latlng){{
  const s = (f.properties && f.properties._style) || {{}};
  return L.circleMarker(latlng, {{
    radius: (s['marker-size'] || 6),
    color: s.stroke || '#222', weight: s['stroke-width'] || 1,
    fillColor: s.color || '#bbb', fillOpacity: 1 }});
}}
const LABEL = {{ terrain:1, city:1 }};        // big labels
const LABEL_SM = {{ building:1, poi:1 }};     // small labels
function onEach(f, layer){{
  const p = f.properties || {{}};
  const name = (p.id||'').replace(/-/g,' ').replace(/\\b\\w/g, c=>c.toUpperCase());
  if (p.description) layer.bindPopup('<b>'+name+'</b><br>'+p.description);
  if (LABEL[p.kind])    layer.bindTooltip(name, {{permanent:true, direction:'center', className:'lbl'}});
  else if (LABEL_SM[p.kind]) layer.bindTooltip(name, {{permanent:true, direction:'top', className:'lbl lbl-sm'}});
}}
const gj = L.geoJSON(GEO, {{ style, pointToLayer, onEachFeature: onEach, coordsToLatLng }}).addTo(map);
map.fitBounds(gj.getBounds().pad(0.08));

// Legend from district faction colours actually present.
const FACTION = {{
  '#3a7a4a':'Reclamation (safe)', '#2a7a8a':'Brine Cabal', '#2f6f9a':'Drowned',
  '#5d6b55':'Restless Dead', '#6a4a7a':'Monstrous', '#a82a44':'Scarlet Sign',
  '#c8a040':'Refugee holdout', '#a83a2a':'Pomarj', '#2d5f8f':'Water'
}};
const seen = {{}};
GEO.features.forEach(f=>{{const c=(f.properties._style||{{}}).color; if(FACTION[c]) seen[c]=1;}});
const legend = L.control({{position:'bottomright'}});
legend.onAdd = function(){{
  const d = L.DomUtil.create('div','legend');
  d.innerHTML = '<b>Holding faction</b><br>' +
    Object.keys(FACTION).filter(c=>seen[c])
      .map(c=>'<i style="background:'+c+'"></i>'+FACTION[c]).join('<br>');
  return d;
}};
legend.addTo(map);
</script></body></html>"""


@app.route("/")
def index():
    ws = wm.parse_file(CFG["map"])
    fc = wm.compile_view(ws, CFG["view"])
    title = f"{ws.name} — {CFG['view']}"
    sub = f"{len(fc['features'])} features · units: {fc['metadata'].get('units')}"
    html = _PAGE.format(title=title, subtitle=sub, geojson=json.dumps(fc))
    return Response(html, mimetype="text/html")


@app.route("/<view>.geojson")
def geojson(view):
    try:
        ws = wm.parse_file(CFG["map"])
        return Response(json.dumps(wm.compile_view(ws, view)), mimetype="application/json")
    except (SyntaxError, ValueError) as e:
        abort(400, str(e))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--map", default=str(
        _REPO / "campaigns/wc1-salt-wrack/maps/cantona.map"))
    ap.add_argument("--view", default="city")
    ap.add_argument("--port", type=int, default=5057)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)

    CFG["map"] = Path(args.map)
    CFG["view"] = args.view
    if not CFG["map"].exists():
        print(f"error: map not found: {CFG['map']}", file=sys.stderr)
        return 2
    # Validate up front so failures are obvious at launch, not first request.
    ws = wm.parse_file(CFG["map"])
    wm.compile_view(ws, args.view)
    print(f"Serving {CFG['map'].name} view '{args.view}' at "
          f"http://{args.host}:{args.port}/  (Ctrl-C to stop)")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
