/**
 * World map renderer — Leaflet w/ Simple CRS, consumes the GeoJSON `_style`
 * attribute set by tools/world_map.py:_resolve_styles.
 *
 * Wiring (set before this script loads):
 *   window.WORLD_MAP_SLUG = "sheldomar";
 *   window.WORLD_MAP_VIEW = "world";
 *   window.WORLD_MAP_DATA = "/api/atlas/sheldomar/world.geojson";
 *   window.WORLD_MAP_PARTY = "/api/atlas/sheldomar/party.geojson";  // 404 OK
 */
(function () {
    var dataUrl   = window.WORLD_MAP_DATA;
    var partyUrl  = window.WORLD_MAP_PARTY;
    var container = document.getElementById('worldmap');
    var sidebar   = document.getElementById('worldmap-sidebar');

    if (!container || !dataUrl) return;

    var map = L.map(container, {
        crs: L.CRS.Simple,
        minZoom: -6,
        maxZoom: 6,
        zoomSnap: 0.25,
        attributionControl: false,
    });

    // Author writes coords as [x, y] with y increasing UP. Leaflet's Simple CRS
    // is [y, x] with y increasing DOWN. Negate y so the visual matches author intent.
    function coordsToLatLng(c) { return L.latLng(-c[1], c[0]); }

    function escapeHTML(s) {
        return String(s).replace(/[<>&"]/g, function (c) {
            return ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' })[c];
        });
    }

    function styleForFeature(feature) {
        var s = (feature.properties && feature.properties._style) || {};
        return {
            color:       s.stroke || s.color || '#888',
            weight:      s['stroke-width'] || 1,
            fillColor:   s.color || '#888',
            fillOpacity: s['fill-opacity'] || 0.5,
            opacity:     1,
        };
    }

    // ── Per-kind icon library (inline SVG defaults) ─────────────────────────
    // Drop a file in static/map-icons/<kind>.{svg,png,webp} to override
    // any of these — see window.MAP_ICON_OVERRIDES injection from the
    // /atlas/<slug>/<view> server route.
    var INLINE_ICONS = {
        city:     '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M3 19 L3 12 L7 12 L7 9 L9 9 L9 6 L11 6 L11 9 L13 9 L13 6 L15 6 L15 9 L17 9 L17 12 L21 12 L21 19 Z" fill="#d4af37" stroke="#1a1510" stroke-width="1.4" stroke-linejoin="round"/></svg>',
        town:     '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M4 19 L4 12 L8 8 L12 12 L12 19 Z M12 19 L12 13 L16 10 L20 13 L20 19 Z" fill="#c8a96e" stroke="#1a1510" stroke-width="1.2" stroke-linejoin="round"/></svg>',
        village:  '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M4 19 L4 13 L12 7 L20 13 L20 19 Z" fill="#a48a5c" stroke="#1a1510" stroke-width="1.2" stroke-linejoin="round"/></svg>',
        capital:  '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M3 19 L3 11 L6 11 L6 8 L8 8 L8 11 L11 11 L11 6 L13 6 L13 11 L16 11 L16 8 L18 8 L18 11 L21 11 L21 19 Z" fill="#e8c75a" stroke="#1a1510" stroke-width="1.5" stroke-linejoin="round"/><circle cx="12" cy="3.5" r="1.6" fill="#e8c75a" stroke="#1a1510" stroke-width="1"/></svg>',
        fortress: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M5 19 L5 9 L7 9 L7 7 L9 7 L9 9 L11 9 L11 7 L13 7 L13 9 L15 9 L15 7 L17 7 L17 9 L19 9 L19 19 Z" fill="#8a7a4f" stroke="#1a1510" stroke-width="1.5" stroke-linejoin="round"/></svg>',
        keep:     '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M5 19 L5 9 L7 9 L7 7 L9 7 L9 9 L11 9 L11 7 L13 7 L13 9 L15 9 L15 7 L17 7 L17 9 L19 9 L19 19 Z" fill="#8a7a4f" stroke="#1a1510" stroke-width="1.5" stroke-linejoin="round"/></svg>',
        tower:    '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M9 19 L9 6 L11 6 L11 4 L13 4 L13 6 L15 6 L15 19 Z" fill="#9a8560" stroke="#1a1510" stroke-width="1.4" stroke-linejoin="round"/></svg>',
        ruin:     '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M3 19 L4 13 L6 16 L7 10 L10 14 L11 9 L14 13 L15 8 L18 13 L20 10 L21 19 Z" fill="#6a5a3f" stroke="#1a1510" stroke-width="1.3" stroke-linejoin="round"/></svg>',
        dungeon:  '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M4 19 L4 15 Q4 7 12 7 Q20 7 20 15 L20 19 L15 19 L15 15 Q15 12 12 12 Q9 12 9 15 L9 19 Z" fill="#2a1f12" stroke="#1a1510" stroke-width="1.3" stroke-linejoin="round"/></svg>',
        cave:     '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M4 19 L4 15 Q4 7 12 7 Q20 7 20 15 L20 19 L15 19 L15 15 Q15 12 12 12 Q9 12 9 15 L9 19 Z" fill="#2a1f12" stroke="#1a1510" stroke-width="1.3" stroke-linejoin="round"/></svg>',
        shrine:   '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M3 12 L12 4 L21 12 L19 12 L19 19 L5 19 L5 12 Z M9 19 L9 14 L11 14 L11 19 M13 19 L13 14 L15 14 L15 19" fill="#d4c5a9" stroke="#1a1510" stroke-width="1.3" stroke-linejoin="round"/></svg>',
        temple:   '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M3 12 L12 4 L21 12 L19 12 L19 19 L5 19 L5 12 Z M7 19 L7 13 L9 13 L9 19 M11 19 L11 13 L13 13 L13 19 M15 19 L15 13 L17 13 L17 19" fill="#e8d4a9" stroke="#1a1510" stroke-width="1.3" stroke-linejoin="round"/></svg>',
        monument: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M10 19 L10 8 L12 3 L14 8 L14 19 Z" fill="#b8a07a" stroke="#1a1510" stroke-width="1.3" stroke-linejoin="round"/></svg>',
        bridge:   '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M3 13 Q12 5 21 13" fill="none" stroke="#a48a5c" stroke-width="2.4" stroke-linecap="round"/><path d="M3 18 L21 18" stroke="#4a82a0" stroke-width="2" stroke-linecap="round"/></svg>',
        port:     '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="6" r="2" fill="none" stroke="#1a1510" stroke-width="1.6"/><path d="M12 8 L12 19 M8 14 L16 14 M5 15 Q5 19 12 19 Q19 19 19 15" fill="none" stroke="#1a1510" stroke-width="1.6" stroke-linecap="round"/></svg>',
        ford:     '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M3 11 Q7 9 12 11 T21 11 M3 15 Q7 13 12 15 T21 15" fill="none" stroke="#4a82a0" stroke-width="2" stroke-linecap="round"/><circle cx="6" cy="13" r="1" fill="#a48a5c"/><circle cx="12" cy="13" r="1" fill="#a48a5c"/><circle cx="18" cy="13" r="1" fill="#a48a5c"/></svg>',
        camp:     '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M5 19 L12 6 L19 19 Z" fill="#9a8560" stroke="#1a1510" stroke-width="1.4" stroke-linejoin="round"/><path d="M9 19 L12 13 L15 19" fill="#3a2818" stroke="#1a1510" stroke-width="1"/></svg>',
        farm:     '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M5 19 L5 13 L9 9 L13 13 L13 19 Z" fill="#c8a96e" stroke="#1a1510" stroke-width="1.2" stroke-linejoin="round"/><circle cx="17" cy="16" r="3" fill="#8aa66a" stroke="#1a1510" stroke-width="1.2"/></svg>',
        inn:      '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M5 19 L5 12 L12 7 L19 12 L19 19 Z" fill="#c8a96e" stroke="#1a1510" stroke-width="1.2" stroke-linejoin="round"/><circle cx="12" cy="14" r="1.6" fill="#1a1510"/></svg>',
        // poi is the default fallback — gold star.
        poi:      '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M12 3 L14.4 9.5 L21 9.7 L15.7 13.7 L17.6 20 L12 16.3 L6.4 20 L8.3 13.7 L3 9.7 L9.6 9.5 Z" fill="#d4af37" stroke="#1a1510" stroke-width="1.3" stroke-linejoin="round"/></svg>',
    };

    // Recognised icon kinds — used both as a fallback and as a tag whitelist
    // so the DSL author can force a specific icon by tagging the feature.
    var ICON_KINDS = [
        'capital', 'city', 'town', 'village', 'fortress', 'keep', 'tower',
        'ruin', 'dungeon', 'cave', 'shrine', 'temple', 'monument',
        'bridge', 'port', 'ford', 'camp', 'farm', 'inn', 'poi',
    ];

    // Map the authored kind + properties to the icon kind we should render.
    //   1. If any tag is the literal name of an icon kind, that wins.
    //      Lets a DSL author tag a feature ``[village]`` and have it
    //      rendered as a village even if its declared kind is ``city``.
    //   2. Settlement kinds (city/town/village) get population-based
    //      promotion or demotion: <1000 → village, <8000 → town, else city.
    //      Picks reasonable AD&D breakpoints; bypassed entirely when no
    //      population is set.
    //   3. Otherwise the authored kind is used as-is.
    function effectiveKind(p) {
        var tags = p.tags || [];
        for (var i = 0; i < tags.length; i++) {
            if (ICON_KINDS.indexOf(tags[i]) >= 0) return tags[i];
        }
        var kind = p.kind || 'poi';
        if (kind === 'city' || kind === 'town' || kind === 'village') {
            var pop = parseInt(p.pop || 0, 10);
            if (pop >= 8000) return 'city';
            if (pop >= 1000) return 'town';
            if (pop > 0)     return 'village';
        }
        return kind;
    }

    function iconFor(kind, style) {
        var rawSize = (style && style['marker-size']) || 12;
        var px = Math.max(18, Math.round(rawSize * 2.0));
        var overrides = window.MAP_ICON_OVERRIDES || {};
        var url = overrides[kind];
        if (url) {
            return L.icon({
                iconUrl:    url,
                iconSize:   [px, px],
                iconAnchor: [px / 2, px / 2],
                className:  'map-icon-img map-icon-' + kind,
            });
        }
        var svg = INLINE_ICONS[kind] || INLINE_ICONS.poi;
        return L.divIcon({
            className:  'map-icon map-icon-' + kind,
            html:       svg,
            iconSize:   [px, px],
            iconAnchor: [px / 2, px / 2],
        });
    }

    function pointToLayer(feature, latlng) {
        var p = feature.properties || {};
        return L.marker(latlng, {
            icon: iconFor(effectiveKind(p), p._style || {}),
            riseOnHover: true,
        });
    }

    function describe(p) {
        var lines = [];
        if (p.id)          lines.push('<b>' + escapeHTML(p.id) + '</b>');
        if (p.kind)        lines.push('<i>' + escapeHTML(p.kind) + '</i>');
        if (p.description) lines.push(escapeHTML(p.description));
        if (p.tags && p.tags.length) {
            lines.push('Tags: ' + p.tags.map(escapeHTML).join(', '));
        }
        // Show numeric/string properties not already covered.
        var skip = { id: 1, kind: 1, description: 1, tags: 1, _style: 1, doc: 1, in: 1 };
        Object.keys(p).forEach(function (k) {
            if (skip[k]) return;
            var v = p[k];
            if (v == null || typeof v === 'object') return;
            lines.push(escapeHTML(k) + ': ' + escapeHTML(String(v)));
        });
        if (p.in)  lines.push('In: ' + escapeHTML(p.in));
        if (p.doc) {
            // Strip extension and use last path component as a slug into /locations.
            var slug = String(p.doc).split('/').pop().replace(/\.md$/, '');
            lines.push('<a href="/locations/_/' + escapeHTML(slug) + '">→ doc</a>');
        }
        return lines.join('<br>');
    }

    function showSidebar(p) {
        if (!sidebar) return;
        sidebar.innerHTML = describe(p);
    }

    function onEachFeature(feature, layer) {
        var p = feature.properties || {};
        var html = describe(p);
        layer.bindPopup(html);
        layer.on('click', function () { showSidebar(p); });
    }

    // ── Layer management: group features by `kind` so users can toggle ─────
    var layersByKind = {};
    function addToKindLayer(kind, layer) {
        if (!layersByKind[kind]) {
            layersByKind[kind] = L.layerGroup().addTo(map);
        }
        layersByKind[kind].addLayer(layer);
    }

    // The layers control is created once, after the main features load.
    // The party overlay gets added to it later (if/when its fetch resolves)
    // so users can uncheck it like any other layer.
    var layersControl = null;
    var partyLayer    = null;

    fetch(dataUrl)
        .then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        })
        .then(function (geo) {
            if (geo.error) {
                container.innerHTML = '<p style="color:#c66;padding:16px">'
                    + 'Compile error: ' + escapeHTML(geo.error) + '</p>';
                return;
            }
            // Build features per-feature so we can place each into a kind layer.
            var allBounds = null;
            (geo.features || []).forEach(function (feat) {
                var sub = L.geoJSON({
                    type: 'FeatureCollection',
                    features: [feat],
                }, {
                    style:           styleForFeature,
                    pointToLayer:    pointToLayer,
                    onEachFeature:   onEachFeature,
                    coordsToLatLng:  coordsToLatLng,
                });
                addToKindLayer(feat.properties.kind || 'other', sub);
                try {
                    var b = sub.getBounds();
                    if (b.isValid()) {
                        allBounds = allBounds ? allBounds.extend(b) : b;
                    }
                } catch (e) { /* point-only or empty */ }
            });

            // Layer toggle control (one entry per kind). Saved so the party
            // overlay can register itself once it lands.
            var overlays = {};
            Object.keys(layersByKind).forEach(function (k) {
                overlays[k] = layersByKind[k];
            });
            layersControl = L.control.layers(null, overlays, { collapsed: false, position: 'topright' }).addTo(map);
            // If the party overlay arrived before us, register it now.
            if (partyLayer) layersControl.addOverlay(partyLayer, 'party');

            if (allBounds && allBounds.isValid()) {
                map.fitBounds(allBounds.pad(0.1));
            } else {
                map.setView([0, 0], 0);
            }

            // Display view metadata (units, scope) in the sidebar header if present.
            if (sidebar && geo.metadata) {
                var meta = geo.metadata;
                sidebar.innerHTML =
                    '<div style="color:#8a7a60;font-size:.85em;margin-bottom:8px">'
                    + 'Scope: <b>' + escapeHTML(meta.scope || '') + '</b>'
                    + (meta.units ? ' · Units: <b>' + escapeHTML(meta.units) + '</b>' : '')
                    + '</div>'
                    + '<div style="color:#8a7a60;font-style:italic">Click a feature for details.</div>';
            }
        })
        .catch(function (err) {
            container.innerHTML = '<p style="color:#c66;padding:16px">'
                + 'Failed to load map: ' + escapeHTML(String(err.message || err)) + '</p>';
        });

    // ── Party overlay — toggleable via the layers control ───────────────────
    if (partyUrl) {
        fetch(partyUrl).then(function (r) {
            if (!r.ok) return null;
            return r.json();
        }).then(function (geo) {
            if (!geo) return;
            partyLayer = L.geoJSON(geo, {
                pointToLayer: function (feature, latlng) {
                    var p = feature.properties || {};
                    var label = escapeHTML(p.label || 'Party');
                    // Custom HTML icon so we can pulse around it via CSS.
                    var icon = L.divIcon({
                        className: 'party-marker',
                        html: '<span class="party-pulse"></span>'
                            + '<span class="party-dot"></span>'
                            + '<span class="party-label">' + label + '</span>',
                        iconSize:  [16, 16],
                        iconAnchor:[8, 8],
                    });
                    return L.marker(latlng, { icon: icon, interactive: false });
                },
                coordsToLatLng: coordsToLatLng,
            });
            partyLayer.addTo(map);
            // Register on the layers control so users can uncheck it.
            // If the control hasn't been created yet (main fetch slower),
            // the .then() above will register us instead.
            if (layersControl) layersControl.addOverlay(partyLayer, 'party');
        }).catch(function () { /* overlay missing is fine */ });
    }
})();
