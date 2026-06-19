"""Item browser routes for the dashboard.

Extracted from dashboard.py — registers /items, /items/<id>, and the
/api/items/cards JSON-fragment endpoint that powers live filtering.
Depends on dashboard's `app`, `cfg`, `render`, `_db`, `_2E_DB`, and the
shared `_markdown_to_html`, `INPUT_STYLE`, `SELECT_STYLE` helpers.
"""
import html
from flask import Response, abort, request


# Mundane equipment keeps functional slugs; magic items use the consolidated
# human-readable category values written by tools/consolidate_magic_item_types.py.
# An option's VALUE must equal the stored item_type exactly — the query does an
# exact `item_type = ?` match.
_ITEM_TYPE_LABELS = {
    # Mundane equipment (functional slugs → readable labels)
    "armor":             "Armor",
    "weapon_melee":      "Melee Weapon",
    "weapon_ranged":     "Ranged Weapon",
    "weapon_ammo":       "Ammunition",
    "misc_equipment":    "Equipment",
    "provisions":        "Provisions",
    "clothing":          "Clothing",
    "item_food_lodging": "Food & Lodging",
    "services":          "Services",
    "animals":           "Animals",
    "tack_harness":      "Tack & Harness",
    # Magic categories (already human-readable; identity labels)
    "Magic Weapon":       "Magic Weapon",
    "Magic Armor":        "Magic Armor",
    "Magic Ammunition":   "Magic Ammunition",
    "Ring":               "Ring",
    "Wand":               "Wand",
    "Rod":                "Rod",
    "Staff":              "Staff",
    "Potion & Oil":       "Potion & Oil",
    "Scroll":             "Scroll",
    "Book & Tome":        "Book & Tome",
    "Musical Instrument": "Musical Instrument",
    "Wondrous Item":      "Wondrous Item",
}

_ITEM_TYPE_GROUPS = {
    "Mundane": ["armor", "weapon_melee", "weapon_ranged", "weapon_ammo",
                "misc_equipment", "provisions", "clothing", "item_food_lodging",
                "services", "animals", "tack_harness"],
    "Magic":   ["Magic Weapon", "Magic Armor", "Magic Ammunition",
                "Ring", "Wand", "Rod", "Staff",
                "Potion & Oil", "Scroll", "Book & Tome",
                "Musical Instrument", "Wondrous Item"],
}


def _rarity_label(r: int) -> str:
    if r <= 5:   return "Common"
    if r <= 20:  return "Uncommon"
    if r <= 40:  return "Rare"
    if r <= 55:  return "Very Rare"
    if r <= 70:  return "Legendary"
    return "Artifact"


_ITEMS_PER_PAGE = 20


def _item_card(r) -> str:
    type_label = _ITEM_TYPE_LABELS.get(r["item_type"], r["item_type"] or "—")
    rarity_str = _rarity_label(r["rarity"])
    return (
        f'<div class="card" style="padding:8px 14px">'
        f'<h2 style="margin:0 0 2px">'
        f'<a href="/items/{r["id"]}">{html.escape(r["name"])}</a></h2>'
        f'<p class="muted" style="margin:0;font-size:.9em">'
        f'{html.escape(type_label)} &nbsp;·&nbsp; '
        + (f'Cost:&nbsp;{html.escape(r["cost"])} &nbsp;·&nbsp; ' if r["cost"] else "")
        + f'{html.escape(rarity_str)}'
        f'</p></div>'
    )


def _item_pagination(page: int, total_pages: int) -> str:
    _btn = (
        'style="background:#3a2818;border:1px solid #5a4030;color:#c8a96e;'
        'padding:5px 12px;border-radius:4px;cursor:pointer;font-size:.9em"'
    )
    _btn_off = (
        'style="background:#2a2018;border:1px solid #3a2818;color:#5a4040;'
        'padding:5px 12px;border-radius:4px;font-size:.9em;cursor:default" disabled'
    )
    parts = []
    parts.append(
        f'<button onclick="iGoTo({page-1})" {_btn}>← Prev</button>'
        if page > 1 else f'<button {_btn_off}>← Prev</button>'
    )
    lo = max(1, page - 3)
    hi = min(total_pages, lo + 6)
    lo = max(1, hi - 6)
    if lo > 1:
        parts.append(f'<button onclick="iGoTo(1)" {_btn}>1</button>')
        if lo > 2:
            parts.append('<span style="color:#8a7a60;padding:0 4px">…</span>')
    for n in range(lo, hi + 1):
        if n == page:
            parts.append(
                f'<button {_btn_off} style="background:#3a2818;border:1px solid #5a4030;'
                f'color:#c8a96e;padding:5px 12px;border-radius:4px;font-size:.9em;'
                f'font-weight:bold;cursor:default">{n}</button>'
            )
        else:
            parts.append(f'<button onclick="iGoTo({n})" {_btn}>{n}</button>')
    if hi < total_pages:
        if hi < total_pages - 1:
            parts.append('<span style="color:#8a7a60;padding:0 4px">…</span>')
        parts.append(f'<button onclick="iGoTo({total_pages})" {_btn}>{total_pages}</button>')
    parts.append(
        f'<button onclick="iGoTo({page+1})" {_btn}>Next →</button>'
        if page < total_pages else f'<button {_btn_off}>Next →</button>'
    )
    return (
        f'<div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap;margin-top:12px">'
        + "".join(parts) + "</div>"
    )


def _item_results_html(rows, total: int, page: int) -> str:
    if not rows:
        return "<p class='muted'>No results.</p>"
    total_pages = (total + _ITEMS_PER_PAGE - 1) // _ITEMS_PER_PAGE
    count = f"<p class='muted' style='margin-bottom:12px'>{total} item{'s' if total != 1 else ''}</p>"
    cards = "".join(_item_card(r) for r in rows)
    pager = _item_pagination(page, total_pages) if total_pages > 1 else ""
    return count + cards + pager


_ITEMS_JS = r"""
<script>
let _itimer = null;
let _iPage  = 1;
function iGoTo(n) { _iPage = n; iDoFetch(); }
function iSchedule() {
  _iPage = 1;
  clearTimeout(_itimer);
  _itimer = setTimeout(iDoFetch, 280);
}
async function iDoFetch() {
  const q     = document.getElementById("item-q").value.trim();
  const itype = document.getElementById("item-type").value;
  const min_r = document.getElementById("item-min-rarity").value;
  if (q.length > 0 && q.length < 3) return;
  const p = new URLSearchParams();
  if (q)          p.set("q",          q);
  if (itype)      p.set("type",       itype);
  if (min_r)      p.set("min_rarity", min_r);
  if (_iPage > 1) p.set("page",       _iPage);
  const r = await fetch("/api/items/cards?" + p.toString());
  document.getElementById("item-results").innerHTML = await r.text();
}
document.getElementById("item-q").addEventListener("input", iSchedule);
document.getElementById("item-type").addEventListener("change", iSchedule);
document.getElementById("item-min-rarity").addEventListener("change", iSchedule);
</script>
"""


def init(app, *, render, db_get, _2E_DB, markdown_to_html, INPUT_STYLE, SELECT_STYLE):
    """Register item routes on `app`. `db_get` is dashboard's _db helper."""

    def _item_query(q, itype, min_r, max_r, page=1):
        if not _2E_DB.exists():
            return [], 0
        conn = db_get(_2E_DB)
        where, params = ["rarity BETWEEN ? AND ?"], [min_r, max_r]
        if q:
            where.append("name LIKE ? COLLATE NOCASE")
            params.append(f"%{q}%")
        if itype:
            where.append("item_type = ?")
            params.append(itype)
        clause = f"WHERE {' AND '.join(where)}"
        total = conn.execute(f"SELECT COUNT(*) FROM items {clause}", params).fetchone()[0]
        offset = (page - 1) * _ITEMS_PER_PAGE
        rows = conn.execute(
            f"SELECT id, name, item_type, cost, rarity FROM items {clause} "
            f"ORDER BY rarity, item_type, name LIMIT {_ITEMS_PER_PAGE} OFFSET {offset}",
            params,
        ).fetchall()
        return rows, total

    @app.route("/api/items/cards")
    def api_item_cards():
        q     = request.args.get("q", "").strip()
        itype = request.args.get("type", "").strip()
        try:
            min_r = int(request.args.get("min_rarity", "0"))
            max_r = int(request.args.get("max_rarity", "100"))
            page  = max(1, int(request.args.get("page", "1")))
        except ValueError:
            min_r, max_r, page = 0, 100, 1
        rows, total = _item_query(q, itype, min_r, max_r, page)
        return Response(_item_results_html(rows, total, page), mimetype="text/html")

    @app.route("/items")
    def items_list():
        q       = request.args.get("q", "").strip()
        itype   = request.args.get("type", "").strip()
        min_rar = request.args.get("min_rarity", "0").strip()

        if not _2E_DB.exists():
            body = "<h1>Items</h1><p class='muted'>2e.db not found. Run tools/build_2e_db.py.</p>"
            return render("Items", body)

        try:
            min_r = int(min_rar)
        except ValueError:
            min_r = 0
        rows, total = _item_query(q, itype, min_r, 100, page=1)

        type_opts = '<option value="">All types</option>'
        for group, types in _ITEM_TYPE_GROUPS.items():
            type_opts += f'<optgroup label="{html.escape(group)}">'
            for t in types:
                label = _ITEM_TYPE_LABELS.get(t, t)
                sel = "selected" if itype == t else ""
                type_opts += f'<option value="{html.escape(t, quote=True)}" {sel}>{html.escape(label)}</option>'
            type_opts += '</optgroup>'

        rarity_opts = (
            '<option value="0">Any rarity</option>'
            + f'<option value="30" {"selected" if min_rar == "30" else ""}>Rare+</option>'
            + f'<option value="45" {"selected" if min_rar == "45" else ""}>Very Rare+</option>'
            + f'<option value="65" {"selected" if min_rar == "65" else ""}>Legendary+</option>'
        )

        controls = (
            f'<div style="margin-bottom:20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
            f'<input id="item-q" value="{html.escape(q)}" placeholder="Search items…" {INPUT_STYLE} autocomplete="off">'
            f'<select id="item-type" {SELECT_STYLE}>{type_opts}</select>'
            f'<select id="item-min-rarity" {SELECT_STYLE}>{rarity_opts}</select>'
            f'</div>'
        )

        body = (
            f"<h1>Items</h1>{controls}"
            f'<div id="item-results">{_item_results_html(rows, total, 1)}</div>'
            + _ITEMS_JS
        )
        return render("Items", body)

    @app.route("/items/<int:item_id>")
    def item_detail(item_id):
        if not _2E_DB.exists():
            abort(404)

        conn = db_get(_2E_DB)
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

        if row is None:
            abort(404)

        def stat(label, val):
            if val is None or val == "":
                return ""
            return (
                f'<tr><th style="width:160px;white-space:nowrap">{html.escape(label)}</th>'
                f'<td>{html.escape(str(val))}</td></tr>'
            )

        type_label = _ITEM_TYPE_LABELS.get(row["item_type"], row["item_type"] or "—")
        table = (
            "<table>"
            + stat("Type",    type_label)
            + stat("Cost",    row["cost"])
            + stat("Weight",  row["weight"])
            + stat("Rarity",  _rarity_label(row["rarity"]))
            + stat("Source",  row["source"])
        )
        if row["ac"] is not None:
            table += stat("Armour Class", row["ac"])
        if row["speed"] is not None:
            table += (
                stat("Size",       row["size"])
                + stat("Type",     row["weapon_type"])
                + stat("Speed",    row["speed"])
                + stat("Dmg S/M",  row["damage_sm"])
                + stat("Dmg L",    row["damage_l"])
            )
            if row["rof"]:
                table += (
                    stat("Rate of Fire", row["rof"])
                    + stat("Range S",   row["range_s"])
                    + stat("Range M",   row["range_m"])
                    + stat("Range L",   row["range_l"])
                )
        if row["xp_value"] is not None:
            table += stat("XP Value", row["xp_value"])
        table += "</table>"

        desc_html = markdown_to_html(row["description"]) if row["description"] else ""

        body = (
            f'<p><a href="/items">← Items</a></p>'
            f'<h1>{html.escape(row["name"])}</h1>'
            f'<div class="card">{table}</div>'
            + (f'<div class="card" style="margin-top:12px">{desc_html}</div>' if desc_html else "")
        )
        return render(f'{html.escape(row["name"])} — Items', body)
