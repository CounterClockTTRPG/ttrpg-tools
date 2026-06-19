/**
 * CombatMap — canvas tactical map renderer with fixed viewport, pan, and zoom.
 *
 *   const map = new CombatMap(canvasEl, { cellPx: 36 });
 *   map.onHover      = (x, y, label) => { ... };  // world cell under cursor + terrain label
 *   map.onClick      = (x, y)        => { ... };  // fired on empty-cell click
 *   map.onZoom       = (z)           => { ... };  // fired after zoom change
 *   map.onTokenClick = (c)           => { ... };  // c = combatant or null on deselect
 *   map.render(state);
 *   map.renderInitList(containerEl, combatants);
 */
class CombatMap {
    constructor(canvas, options) {
        options = options || {};
        this.canvas   = canvas;
        this.ctx      = canvas.getContext('2d');
        this.cellPx   = options.cellPx || 36;
        this.zoom     = 1.0;
        this._imgs    = {};
        this._last    = null;
        this._selectedToken = null;
        this._offsetX = 0;
        this._offsetY = 0;
        this._dragging   = false;
        this._dragMoved  = false;
        this._dragStartX = 0;
        this._dragStartY = 0;
        this._dragOffX0  = 0;
        this._dragOffY0  = 0;
        this._pendingRAF = 0;     // rAF handle; coalesces multiple render requests per frame
        this._wrapW      = 0;     // cached wrapper size, updated on ResizeObserver
        this._wrapH      = 0;

        var self = this;

        // ── Drag to pan ───────────────────────────────────────────────────────
        canvas.addEventListener('mousedown', function(e) {
            if (e.button !== 0) return;
            self._dragging   = true;
            self._dragMoved  = false;
            self._dragStartX = e.clientX;
            self._dragStartY = e.clientY;
            self._dragOffX0  = self._offsetX;
            self._dragOffY0  = self._offsetY;
            canvas.style.cursor = 'grabbing';
            e.preventDefault();
        });

        canvas.addEventListener('mousemove', function(e) {
            if (self._dragging) {
                var dx = e.clientX - self._dragStartX;
                var dy = e.clientY - self._dragStartY;
                if (Math.abs(dx) > 3 || Math.abs(dy) > 3) self._dragMoved = true;
                self._offsetX = self._dragOffX0 - dx;
                self._offsetY = self._dragOffY0 - dy;
                if (self._last && self._last.grid) self._clampOffset(self._last.grid, self._C());
                self._scheduleRender();
                return;
            }
            // Hover: terrain label lookup in world coords
            var rect  = canvas.getBoundingClientRect();
            var C     = self._C();
            var mx    = Math.floor((e.clientX - rect.left + self._offsetX) / C);
            var my    = Math.floor((e.clientY - rect.top  + self._offsetY) / C);
            var label = '';
            if (self._last && self._last.grid) {
                var key  = mx + ',' + my;
                var type = (self._last.grid.cells || {})[key];
                if (type) {
                    var st = (self._last.grid.styles || {})[type];
                    label = (st && st.label) ? st.label
                          : type.charAt(0) + type.slice(1).toLowerCase();
                }
            }
            if (self.onHover) self.onHover(mx, my, label);
        });

        canvas.addEventListener('mouseup',    function() { self._dragging = false; canvas.style.cursor = 'grab'; });
        canvas.addEventListener('mouseleave', function() { self._dragging = false; canvas.style.cursor = 'grab'; });

        // ── Click: token hit-test or cell ─────────────────────────────────────
        canvas.addEventListener('click', function(e) {
            if (self._dragMoved) { self._dragMoved = false; return; }
            var rect = canvas.getBoundingClientRect();
            var C    = self._C();
            var ex   = e.clientX - rect.left;   // screen coords
            var ey   = e.clientY - rect.top;

            var hit = null;
            if (self._last && self._last.combatants) {
                for (var i = 0; i < self._last.combatants.length; i++) {
                    var c  = self._last.combatants[i];
                    if (c.x == null || c.y == null) continue;
                    var tx = c.x * C + C / 2 - self._offsetX;
                    var ty = c.y * C + C / 2 - self._offsetY;
                    var tr = C * 0.38 + 3;
                    var dx = ex - tx, dy = ey - ty;
                    if (dx * dx + dy * dy <= tr * tr) { hit = c; break; }
                }
            }

            if (hit) {
                var same = self._selectedToken && self._selectedToken.name === hit.name;
                self._selectedToken = same ? null : hit;
                if (self._last) self.render(self._last);
                if (self.onTokenClick) self.onTokenClick(self._selectedToken);
            } else {
                self._selectedToken = null;
                var mx = Math.floor((ex + self._offsetX) / C);
                var my = Math.floor((ey + self._offsetY) / C);
                if (self.onClick) self.onClick(mx, my);
            }
        });

        // ── Zoom toward cursor ────────────────────────────────────────────────
        canvas.addEventListener('wheel', function(e) {
            e.preventDefault();
            var rect   = canvas.getBoundingClientRect();
            var cx     = e.clientX - rect.left;
            var cy     = e.clientY - rect.top;
            var oldC   = self._C();
            // World point under cursor
            var wx     = (cx + self._offsetX) / oldC;
            var wy     = (cy + self._offsetY) / oldC;
            self.zoom  = Math.max(0.25, Math.min(5.0, self.zoom * (e.deltaY < 0 ? 1.15 : 0.87)));
            var newC   = self._C();
            self._offsetX = wx * newC - cx;
            self._offsetY = wy * newC - cy;
            if (self._last && self._last.grid) self._clampOffset(self._last.grid, newC);
            if (self._last) self.render(self._last);
            if (self.onZoom) self.onZoom(self.zoom);
        }, { passive: false });

        // ── Arrow keys to scroll ──────────────────────────────────────────────
        document.addEventListener('keydown', function(e) {
            if (!self._last || !self._last.grid) return;
            var tag = document.activeElement && document.activeElement.tagName;
            if (tag === 'INPUT' || tag === 'TEXTAREA') return;
            var step  = self._C();
            var moved = true;
            if      (e.key === 'ArrowLeft')  self._offsetX -= step;
            else if (e.key === 'ArrowRight') self._offsetX += step;
            else if (e.key === 'ArrowUp')    self._offsetY -= step;
            else if (e.key === 'ArrowDown')  self._offsetY += step;
            else moved = false;
            if (moved) {
                self._clampOffset(self._last.grid, self._C());
                self.render(self._last);
                e.preventDefault();
            }
        });
    }

    // ── Public API ────────────────────────────────────────────────────────────

    _C() { return Math.max(8, Math.round(this.cellPx * this.zoom)); }

    _clampOffset(g, C) {
        var mapW = g.width  * C;
        var mapH = g.height * C;
        this._offsetX = Math.max(0, Math.min(this._offsetX, Math.max(0, mapW - this.canvas.width)));
        this._offsetY = Math.max(0, Math.min(this._offsetY, Math.max(0, mapH - this.canvas.height)));
    }

    setZoom(z) {
        // Zoom toward center of viewport
        var cx    = this.canvas.width  / 2;
        var cy    = this.canvas.height / 2;
        var oldC  = this._C();
        var wx    = (cx + this._offsetX) / oldC;
        var wy    = (cy + this._offsetY) / oldC;
        this.zoom = Math.max(0.25, Math.min(5.0, z));
        var newC  = this._C();
        this._offsetX = wx * newC - cx;
        this._offsetY = wy * newC - cy;
        if (this._last && this._last.grid) this._clampOffset(this._last.grid, newC);
        if (this._last) this.render(this._last);
        if (this.onZoom) this.onZoom(this.zoom);
    }

    render(state) {
        if (!state || !state.grid) return;
        this._last = state;
        var g  = state.grid;
        var C  = this._C();
        var ctx = this.ctx;

        // Fit canvas to its wrapper (fixed viewport — does not grow with zoom).
        // Wrapper size is cached and refreshed by ResizeObserver below; the
        // first render does a one-shot measure.
        if (!this._wrapW || !this._wrapH) this._measureWrap();
        var vw = this._wrapW, vh = this._wrapH;
        if (this.canvas.width !== vw || this.canvas.height !== vh) {
            this.canvas.width  = vw;
            this.canvas.height = vh;
        }

        this._clampOffset(g, C);

        // Viewport background (covers area outside map bounds)
        ctx.fillStyle = '#1a1510';
        ctx.fillRect(0, 0, vw, vh);

        // Translate so world origin aligns with current scroll offset
        ctx.save();
        ctx.translate(-this._offsetX, -this._offsetY);
        this._drawGrid(g, C);
        this._drawSvgBackground(g, C);
        this._drawTerrain(g, C);
        this._drawTokens(state.combatants || [], C);
        if (this._selectedToken) this._drawMovementRadius(this._selectedToken, C);
        ctx.restore();
    }

    /**
     * If the active map was rendered via dungml the grid carries an
     * `svg_url` (a data: URL). Decode it once, cache the resulting
     * Image, and draw at `g.width * C` × `g.height * C` so one cell
     * = one world unit at the current zoom. The image scales naturally
     * because the SVG's viewBox uses world-unit coords.
     */
    _drawSvgBackground(g, C) {
        var url = g && g.svg_url;
        if (!url) return;
        var ctx = this.ctx;
        var img = this._svgImage;
        if (!img || img._url !== url) {
            img = new Image();
            img._url = url;
            var self = this;
            img.onload  = function() { self._scheduleRender(); };
            img.onerror = function() {};
            img.src = url;
            this._svgImage = img;
        }
        if (img.complete && img.naturalWidth > 0) {
            ctx.drawImage(img, 0, 0, g.width * C, g.height * C);
        }
    }

    // ── Canvas layers ─────────────────────────────────────────────────────────

    _drawGrid(g, C) {
        var ctx = this.ctx;
        ctx.fillStyle = '#1a1510';
        ctx.fillRect(0, 0, g.width * C, g.height * C);
        ctx.strokeStyle = '#2e261e';
        ctx.lineWidth   = 0.5;
        for (var x = 0; x <= g.width; x++) {
            ctx.beginPath(); ctx.moveTo(x*C, 0); ctx.lineTo(x*C, g.height*C); ctx.stroke();
        }
        for (var y = 0; y <= g.height; y++) {
            ctx.beginPath(); ctx.moveTo(0, y*C); ctx.lineTo(g.width*C, y*C); ctx.stroke();
        }
    }

    _drawTerrain(g, C) {
        var ctx    = this.ctx;
        var cells  = g.cells  || {};
        var styles = g.styles || {};

        for (var key in cells) {
            var type = cells[key];
            var p    = key.split(',');
            var cx   = parseInt(p[0], 10);
            var cy   = parseInt(p[1], 10);
            var st   = styles[type] || { color: '#555', symbol: '?' };
            var px   = cx * C + 1;
            var py   = cy * C + 1;
            var pw   = C - 2;

            var texImg     = st.texture ? this._getImg(st.texture) : null;
            var hasTexture = texImg && texImg.complete && texImg.naturalWidth > 0;

            if (hasTexture) {
                ctx.drawImage(texImg, px, py, pw, pw);
                ctx.fillStyle = st.color + '28';
                ctx.fillRect(px, py, pw, pw);
            } else {
                ctx.fillStyle = st.color;
                ctx.fillRect(px, py, pw, pw);
            }

            var showSym = !hasTexture || type !== 'WALL';
            if (showSym) {
                ctx.fillStyle    = hasTexture ? 'rgba(255,255,255,0.45)' : 'rgba(255,255,255,0.75)';
                ctx.font         = Math.floor(C * 0.52) + 'px monospace';
                ctx.textAlign    = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(st.symbol, cx*C + C/2, cy*C + C/2);
            }
        }
    }

    _drawTokens(combatants, C) {
        for (var i = 0; i < combatants.length; i++) {
            var c = combatants[i];
            if (c.x == null || c.y == null) continue;
            // Combatants already drawn into the dungml SVG carry
            // in_svg=true. Skip them so we don't double-draw the same
            // token (server-side marker + client-side canvas token).
            if (c.in_svg) continue;
            this._drawToken(c, c.x*C + C/2, c.y*C + C/2, C);
        }
    }

    _drawToken(c, px, py, C) {
        var ctx    = this.ctx;
        var r      = C * 0.38;
        var downed = c.hp !== undefined && c.hp <= 0;
        var isSelected = this._selectedToken && this._selectedToken.name === c.name;

        // Outer ring: gray=downed, white=selected, gold=current turn, red=enemy, HP-color=party
        ctx.beginPath();
        ctx.arc(px, py, r + 3, 0, Math.PI * 2);
        if (downed) {
            ctx.strokeStyle = '#505050'; ctx.lineWidth = 1.5;
        } else if (isSelected) {
            ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 2.5;
        } else if (c.current) {
            ctx.strokeStyle = '#c8a96e'; ctx.lineWidth = 2.5;
        } else if (c.side !== 'party') {
            ctx.strokeStyle = '#8c2a2a'; ctx.lineWidth = 1.5;
        } else {
            ctx.strokeStyle = this._hpColor(c.hp, c.hp_max); ctx.lineWidth = 1.5;
        }
        ctx.stroke();

        // Portrait / monogram (clipped to circle)
        ctx.save();
        ctx.beginPath();
        ctx.arc(px, py, r, 0, Math.PI * 2);
        ctx.clip();

        var drawn = false;
        if (c.portrait_url) {
            var img = this._getImg(c.portrait_url);
            if (img.complete && img.naturalWidth > 0) {
                var s     = r * 2;
                var scale = Math.max(s / img.naturalWidth, s / img.naturalHeight);
                ctx.drawImage(img,
                    px - img.naturalWidth  * scale / 2,
                    py - img.naturalHeight * scale / 2,
                    img.naturalWidth  * scale,
                    img.naturalHeight * scale);
                drawn = true;
            }
        }
        if (!drawn) {
            ctx.fillStyle    = c.side === 'party' ? '#2a4a2a' : '#4a2a2a';
            ctx.fillRect(px - r, py - r, r*2, r*2);
            ctx.fillStyle    = '#d4c5a9';
            ctx.font         = 'bold ' + Math.floor(r * 0.9) + 'px sans-serif';
            ctx.textAlign    = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText((c.name || '?')[0].toUpperCase(), px, py);
        }

        // Gray overlay for downed/unconscious tokens
        if (downed) {
            ctx.fillStyle = 'rgba(20, 20, 20, 0.65)';
            ctx.fillRect(px - r, py - r, r*2, r*2);
        }
        ctx.restore();

        // Name label
        var nameLabel = c.name.length > 9 ? c.name.slice(0, 8) + '…' : c.name;
        ctx.shadowColor  = '#000';
        ctx.shadowBlur   = 3;
        ctx.fillStyle    = downed   ? '#606060'
                         : c.current ? '#c8a96e'
                         : c.side === 'party' ? '#8dc88d' : '#c88d8d';
        ctx.font         = Math.max(9, Math.floor(C * 0.26)) + 'px sans-serif';
        ctx.textAlign    = 'center';
        ctx.textBaseline = 'top';
        ctx.fillText(nameLabel, px, py + r + 2);
        ctx.shadowBlur   = 0;
    }

    _drawMovementRadius(c, C) {
        if (c.x == null || c.y == null) return;
        var ctx = this.ctx;
        var mov = c.movement || 6;
        var px  = c.x * C + C / 2;
        var py  = c.y * C + C / 2;
        var rad = mov * C;

        ctx.save();
        ctx.setLineDash([5, 4]);
        ctx.beginPath();
        ctx.arc(px, py, rad, 0, Math.PI * 2);
        ctx.strokeStyle = c.side === 'party' ? 'rgba(100,200,100,0.8)' : 'rgba(200,100,100,0.8)';
        ctx.lineWidth   = 1.5;
        ctx.stroke();
        ctx.fillStyle   = c.side === 'party' ? 'rgba(100,200,100,0.06)' : 'rgba(200,100,100,0.06)';
        ctx.fill();
        ctx.restore();

        ctx.save();
        ctx.fillStyle    = c.side === 'party' ? 'rgba(140,210,140,0.95)' : 'rgba(210,140,140,0.95)';
        ctx.font         = '11px sans-serif';
        ctx.textAlign    = 'center';
        ctx.textBaseline = 'bottom';
        ctx.shadowColor  = '#000';
        ctx.shadowBlur   = 3;
        ctx.fillText(mov + ' cells', px, py - rad - 2);
        ctx.restore();
    }

    // ── Initiative sidebar (HTML) ─────────────────────────────────────────────

    renderInitList(containerEl, combatants) {
        if (!containerEl || !combatants) return;
        var sorted = combatants.slice().sort(function(a, b) {
            if (a.init == null && b.init == null) return 0;
            if (a.init == null) return  1;
            if (b.init == null) return -1;
            return a.init - b.init;
        });
        var html = '';
        for (var i = 0; i < sorted.length; i++) {
            var c       = sorted[i];
            var dead    = c.hp !== undefined && c.hp <= -10;
            var downed  = !dead && c.hp !== undefined && c.hp <= 0;
            var side    = c.side === 'party' ? 'party' : 'enemy';
            var curCls  = c.current ? ' init-current' : '';
            var initStr = (c.init != null) ? c.init : '—';
            var rowStyle = dead   ? ' style="opacity:.3"'
                         : downed ? ' style="opacity:.5;filter:grayscale(.85)"'
                         : '';
            var hpColor = this._hpColor(c.hp, c.hp_max);
            var bgColor = c.side === 'party' ? '#2a4a2a' : '#4a2a2a';
            var initial = (c.name || '?')[0].toUpperCase();
            var portrait = c.portrait_url
                ? '<img src="' + c.portrait_url + '" alt="' + _esc(c.name) + '"'
                  + ' onerror="this.parentNode.innerHTML=\'<span>' + initial + '</span>\'">'
                : '<span>' + initial + '</span>';
            var hpStr = (c.hp !== undefined && c.hp_max) ? c.hp + '/' + c.hp_max : '';
            html += '<div class="init-row ' + side + curCls + '"' + rowStyle + '>'
                  + '<div class="init-portrait" style="border-color:' + hpColor + ';background:' + bgColor + '">' + portrait + '</div>'
                  + '<div class="init-info">'
                  + '<span class="init-name">' + _esc(c.name) + '</span>'
                  + '<div style="display:flex;flex-direction:column;align-items:flex-end;gap:1px">'
                  + '<span class="init-val">' + initStr + '</span>'
                  + (hpStr ? '<span style="color:' + hpColor + ';font-size:.72em;font-family:monospace;line-height:1">' + hpStr + '</span>' : '')
                  + '</div>'
                  + '</div></div>';
        }
        containerEl.innerHTML = html;
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    _hpColor(hp, hp_max) {
        if (!hp_max || hp <= 0) return '#555555';
        var r = hp / hp_max;
        if (r > 0.60) return '#4a8c4a';
        if (r > 0.25) return '#8c7a2a';
        return '#8c2a2a';
    }

    _getImg(url) {
        if (!this._imgs[url]) {
            var img  = new Image();
            var self = this;
            // rAF-coalesce: when many tokens/textures load in the same frame
            // (initial paint), schedule exactly one full re-render.
            img.onload = function() { self._scheduleRender(); };
            img.src = url;
            this._imgs[url] = img;
        }
        return this._imgs[url];
    }

    _scheduleRender() {
        if (this._pendingRAF || !this._last) return;
        var self = this;
        this._pendingRAF = requestAnimationFrame(function() {
            self._pendingRAF = 0;
            if (self._last) self.render(self._last);
        });
    }

    _measureWrap() {
        var wrap = this.canvas.parentElement;
        this._wrapW = wrap ? Math.max(100, wrap.clientWidth)  : 800;
        this._wrapH = wrap ? Math.max(100, wrap.clientHeight) : 560;
        if (!this._resizeObs && wrap && typeof ResizeObserver !== 'undefined') {
            var self = this;
            this._resizeObs = new ResizeObserver(function() {
                self._wrapW = Math.max(100, wrap.clientWidth);
                self._wrapH = Math.max(100, wrap.clientHeight);
                self._scheduleRender();
            });
            this._resizeObs.observe(wrap);
        }
    }
}

function _esc(s) {
    return (s || '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
