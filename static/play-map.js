// Explore-map viewer — a vanilla-JS port of dungml's SvgPreview "play view".
//
// The dungml-rendered SVG is injected inline (crisp vector + hover tooltips)
// into a "stage" div that we CSS-transform for pan/zoom. A sibling overlay
// <svg>, sharing the map's viewBox, paints fog-of-war over unrevealed rooms
// and the party-position marker — so both pan/zoom in lockstep with the map.
//
// dungml renders viewBox="0 0 W total_h" in CELL units, and ttrpg2 stores
// room polygons + the party position in those same cell coords, so the
// overlay needs no coordinate transform: cell (x, y) is SVG (x, y).
//
// No combat: /area is a pure exploration view now.
(function () {
  "use strict";
  var SVG_NS = "http://www.w3.org/2000/svg";
  var MIN_RELATIVE = 0.25; // floor: 25% of fit-scale
  var MAX_ABSOLUTE = 32; // ceiling: 3200% pixel scale
  var WHEEL_FACTOR = 1.15;
  var KEY_FACTOR = 1.25;
  var FOG_FILL = "#0a0806"; // opaque dark over rooms the party hasn't seen

  // Decode a data: URL holding an SVG into its markup. Handles base64 and
  // plain (utf8 / url-encoded) forms; base64 is decoded UTF-8-safe.
  function decodeSvgDataUrl(url) {
    if (!url) return null;
    var b64 = "data:image/svg+xml;base64,";
    if (url.indexOf(b64) === 0) {
      var raw = url.slice(b64.length);
      try {
        return decodeURIComponent(escape(atob(raw)));
      } catch (e) {
        try {
          return atob(raw);
        } catch (_) {
          return null;
        }
      }
    }
    var u8 = "data:image/svg+xml;utf8,";
    if (url.indexOf(u8) === 0) return decodeURIComponent(url.slice(u8.length));
    var plain = "data:image/svg+xml,";
    if (url.indexOf(plain) === 0) return decodeURIComponent(url.slice(plain.length));
    return null;
  }

  function parseViewBox(svgEl) {
    var vb = svgEl.getAttribute("viewBox");
    if (!vb) return null;
    var p = vb.trim().split(/[\s,]+/).map(parseFloat);
    if (p.length !== 4 || p.some(isNaN)) return null;
    return { x: p[0], y: p[1], w: p[2], h: p[3] };
  }

  function PlayMap(host) {
    this.host = host; // overflow-hidden container
    this.stage = document.createElement("div");
    this.stage.className = "pm-stage";
    this.mapLayer = document.createElement("div");
    this.mapLayer.className = "pm-map";
    this.overlay = document.createElementNS(SVG_NS, "svg");
    this.overlay.setAttribute("class", "pm-overlay");
    this.stage.appendChild(this.mapLayer);
    this.stage.appendChild(this.overlay);
    this.host.appendChild(this.stage);

    this.tip = document.createElement("div");
    this.tip.className = "pm-tip";
    this.tip.style.display = "none";
    document.body.appendChild(this.tip);

    this.scale = 1;
    this.fitScale = 1;
    this.tx = 0;
    this.ty = 0;
    this.natural = null; // {w, h} in px
    this.viewBox = null; // {x, y, w, h}
    this._svgUrl = null; // last injected svg_url (skip re-inject → keeps zoom)
    this._drag = null;
    this.onZoom = null; // callback(scaleFraction)

    this._bind();
    var self = this;
    this._ro = new ResizeObserver(function () {
      self.fit();
    });
    this._ro.observe(this.host);
  }

  PlayMap.prototype._apply = function () {
    this.stage.style.transform =
      "translate3d(" + this.tx + "px," + this.ty + "px,0) scale(" + this.scale + ")";
    if (this.onZoom && this.fitScale > 0) this.onZoom(this.scale / this.fitScale);
  };

  PlayMap.prototype._minScale = function () {
    return Math.max(0.01, this.fitScale * MIN_RELATIVE);
  };

  PlayMap.prototype._clamp = function (s) {
    return Math.max(this._minScale(), Math.min(MAX_ABSOLUTE, s));
  };

  // Zoom keeping the point (cx, cy) — host-local px — fixed under the cursor.
  PlayMap.prototype.zoomAt = function (cx, cy, factor) {
    var next = this._clamp(this.scale * factor);
    var ratio = next / this.scale;
    this.tx = cx - (cx - this.tx) * ratio;
    this.ty = cy - (cy - this.ty) * ratio;
    this.scale = next;
    this._apply();
  };

  PlayMap.prototype.zoomBy = function (factor) {
    var r = this.host.getBoundingClientRect();
    this.zoomAt(r.width / 2, r.height / 2, factor);
  };

  PlayMap.prototype.fit = function () {
    if (!this.natural) return;
    var cw = this.host.clientWidth;
    var ch = this.host.clientHeight;
    if (cw <= 0 || ch <= 0) return;
    var s = Math.min(cw / this.natural.w, ch / this.natural.h) * 0.94;
    this.fitScale = s;
    this.scale = s;
    this.tx = (cw - this.natural.w * s) / 2;
    this.ty = (ch - this.natural.h * s) / 2;
    this._apply();
  };

  // World/cell coords → stage-local px (pre-transform).
  PlayMap.prototype._worldToPx = function (wx, wy) {
    if (!this.viewBox || !this.natural) return null;
    return {
      x: ((wx - this.viewBox.x) / this.viewBox.w) * this.natural.w,
      y: ((wy - this.viewBox.y) / this.viewBox.h) * this.natural.h,
    };
  };

  // Center the view on a world/cell point (used for set_map_focus).
  PlayMap.prototype.centerOn = function (wx, wy) {
    var p = this._worldToPx(wx + 0.5, wy + 0.5);
    if (!p) return;
    var cw = this.host.clientWidth;
    var ch = this.host.clientHeight;
    this.tx = cw / 2 - p.x * this.scale;
    this.ty = ch / 2 - p.y * this.scale;
    this._apply();
  };

  PlayMap.prototype.clear = function () {
    this.mapLayer.innerHTML = "";
    while (this.overlay.firstChild) this.overlay.removeChild(this.overlay.firstChild);
    this._svgUrl = null;
    this.natural = null;
    this.viewBox = null;
  };

  // Inject the map SVG (only when it changed) and (re)build the fog + party
  // overlay (cheap, every poll — so revealing a room or moving the party
  // updates without disturbing the current pan/zoom).
  PlayMap.prototype.setState = function (data) {
    var grid = data && data.grid;
    var url = grid && grid.svg_url;
    var svg = decodeSvgDataUrl(url);
    if (!svg) {
      this.clear();
      return;
    }
    if (url !== this._svgUrl) {
      this.mapLayer.innerHTML = svg;
      this._svgUrl = url;
      var el = this.mapLayer.querySelector("svg");
      if (!el) {
        this.clear();
        return;
      }
      this.viewBox = parseViewBox(el);
      var w =
        parseFloat(el.getAttribute("width")) ||
        (this.viewBox && this.viewBox.w) ||
        el.getBoundingClientRect().width;
      var h =
        parseFloat(el.getAttribute("height")) ||
        (this.viewBox && this.viewBox.h) ||
        el.getBoundingClientRect().height;
      this.natural = { w: w, h: h };
      el.setAttribute("width", w);
      el.setAttribute("height", h);
      this.overlay.setAttribute("width", w);
      this.overlay.setAttribute("height", h);
      if (this.viewBox)
        this.overlay.setAttribute(
          "viewBox",
          this.viewBox.x + " " + this.viewBox.y + " " + this.viewBox.w + " " + this.viewBox.h
        );
      this.fit();
    }
    this._drawOverlay(data);
  };

  PlayMap.prototype._drawOverlay = function (data) {
    var ov = this.overlay;
    while (ov.firstChild) ov.removeChild(ov.firstChild);

    // Fog: darken every room the party has not revealed.
    var revealed = {};
    (data.revealed_rooms || []).forEach(function (n) {
      revealed[n] = true;
    });
    (data.rooms || []).forEach(function (room) {
      if (revealed[room.name]) return;
      var poly = room.polygon || [];
      if (poly.length < 3) return;
      var pts = poly
        .map(function (p) {
          return p[0] + "," + p[1];
        })
        .join(" ");
      var el = document.createElementNS(SVG_NS, "polygon");
      el.setAttribute("points", pts);
      el.setAttribute("fill", FOG_FILL);
      ov.appendChild(el);
    });

    // Party marker: a green disc + label at the stored cell (centered).
    var party = data.party;
    if (party && party.x != null && party.y != null) {
      var cx = party.x + 0.5;
      var cy = party.y + 0.5;
      var g = document.createElementNS(SVG_NS, "g");
      g.setAttribute("class", "pm-party");

      var halo = document.createElementNS(SVG_NS, "circle");
      halo.setAttribute("cx", cx);
      halo.setAttribute("cy", cy);
      halo.setAttribute("r", 0.78);
      halo.setAttribute("fill", "rgba(63,145,66,0.25)");
      g.appendChild(halo);

      var disc = document.createElementNS(SVG_NS, "circle");
      disc.setAttribute("cx", cx);
      disc.setAttribute("cy", cy);
      disc.setAttribute("r", 0.5);
      disc.setAttribute("fill", "#3f9142");
      disc.setAttribute("stroke", "#eafbe8");
      disc.setAttribute("stroke-width", 0.13);
      g.appendChild(disc);

      var label = party.label || "Party";
      var txt = document.createElementNS(SVG_NS, "text");
      txt.setAttribute("x", cx);
      txt.setAttribute("y", cy + 1.55);
      txt.setAttribute("text-anchor", "middle");
      txt.setAttribute("font-size", 0.85);
      txt.setAttribute("font-family", "system-ui, sans-serif");
      txt.setAttribute("fill", "#eafbe8");
      txt.setAttribute("stroke", "#0a0806");
      txt.setAttribute("stroke-width", 0.16);
      txt.setAttribute("paint-order", "stroke");
      txt.textContent = label;
      g.appendChild(txt);

      ov.appendChild(g);
    }
  };

  // --- pan + zoom + tooltip wiring ---
  PlayMap.prototype._bind = function () {
    var self = this;

    this.host.addEventListener(
      "wheel",
      function (e) {
        e.preventDefault();
        var r = self.host.getBoundingClientRect();
        var factor = e.deltaY < 0 ? WHEEL_FACTOR : 1 / WHEEL_FACTOR;
        self.zoomAt(e.clientX - r.left, e.clientY - r.top, factor);
      },
      { passive: false }
    );

    this.host.addEventListener("mousedown", function (e) {
      if (e.button !== 0 && e.button !== 1) return;
      self._drag = { x: e.clientX, y: e.clientY, tx: self.tx, ty: self.ty };
      self.host.classList.add("grabbing");
      self._hideTip();
    });

    window.addEventListener("mousemove", function (e) {
      if (!self._drag) return;
      self.tx = self._drag.tx + (e.clientX - self._drag.x);
      self.ty = self._drag.ty + (e.clientY - self._drag.y);
      self._apply();
    });

    window.addEventListener("mouseup", function () {
      if (!self._drag) return;
      self._drag = null;
      self.host.classList.remove("grabbing");
    });

    // Hover tooltip from the dungml SVG's data-label / data-description.
    this.host.addEventListener("mousemove", function (e) {
      if (self._drag) return;
      var t = e.target;
      var el = t && t.closest
        ? t.closest("[data-label],[data-description],[data-room],[data-corridor]")
        : null;
      if (!el) return self._hideTip();
      var title =
        el.getAttribute("data-label") ||
        el.getAttribute("data-room") ||
        el.getAttribute("data-corridor") ||
        "";
      var body = el.getAttribute("data-description") || "";
      if (!title && !body) return self._hideTip();
      self.tip.innerHTML = "";
      if (title) {
        var h = document.createElement("div");
        h.className = "pm-tip-title";
        h.textContent = title;
        self.tip.appendChild(h);
      }
      if (body) {
        var b = document.createElement("div");
        b.textContent = body;
        self.tip.appendChild(b);
      }
      self.tip.style.display = "block";
      self.tip.style.left = e.clientX + 14 + "px";
      self.tip.style.top = e.clientY + 14 + "px";
    });

    this.host.addEventListener("mouseleave", function () {
      self._drag = null;
      self.host.classList.remove("grabbing");
      self._hideTip();
    });

    this.host.addEventListener("dblclick", function () {
      self.fit();
    });

    this.host.addEventListener("keydown", function (e) {
      if (e.key === "+" || e.key === "=") {
        e.preventDefault();
        self.zoomBy(KEY_FACTOR);
      } else if (e.key === "-" || e.key === "_") {
        e.preventDefault();
        self.zoomBy(1 / KEY_FACTOR);
      } else if (e.key === "0") {
        e.preventDefault();
        self.fit();
      }
    });
  };

  PlayMap.prototype._hideTip = function () {
    if (this.tip) this.tip.style.display = "none";
  };

  window.PlayMap = PlayMap;
})();
