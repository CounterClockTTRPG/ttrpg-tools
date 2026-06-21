"""DungML server HTTP client.

Lightweight session-with-token wrapper around the DungML backend API
(see /home/raf/roleplaying/dungml/packages/backend). Used by
`map_state.py` to fetch project maps for fog-of-war loading. The
existing `combat_map._dungml_render` uses the stateless /api/dsl/render
endpoint and does not need auth; this module covers the authenticated
project/map endpoints.

Configuration order (first non-empty wins):

  DUNGML_API_BASE  default http://192.168.86.29:8000
  DUNGML_EMAIL     no default
  DUNGML_PASSWORD  no default
  DUNGML_PROJECT   project name (case-insensitive prefix) or full UUID;
                   default: active campaign name

If credentials are absent the module raises ConnectionError on first
use — callers should surface that to the DM verbatim (per CLAUDE.md
combat-map policy: don't invent fallbacks).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent

try:
    from dotenv import load_dotenv  # type: ignore[import-not-found]
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass


_DEFAULT_BASE = "http://192.168.86.29:8000"
_TIMEOUT_S = 10.0

# Cached login state — re-auth lazily when missing or stale.
_token: str | None = None
_token_at: float = 0.0
_TOKEN_TTL_S = 30 * 60  # 30 min; backend default is longer but be conservative


def api_base() -> str:
    return os.environ.get("DUNGML_API_BASE", _DEFAULT_BASE).rstrip("/")


def _http(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    token: str | None = None,
) -> dict:
    """Send an HTTP request to the DungML server and return parsed JSON.

    Raises:
      ConnectionError    backend unreachable
      PermissionError    401 (auth)
      LookupError        404 (not found)
      ValueError         400 (bad request) — message carries the detail
      RuntimeError       any other non-2xx
    """
    url = f"{api_base()}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"accept": "application/json"}
    if body is not None:
        headers["content-type"] = "application/json"
    if token:
        headers["authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {}
        detail = payload.get("detail", payload)
        if e.code == 401:
            raise PermissionError(f"auth failed: {detail}") from e
        if e.code == 404:
            raise LookupError(f"not found: {detail}") from e
        if e.code == 400:
            raise ValueError(f"bad request: {detail}") from e
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise ConnectionError(
            f"dungml server unreachable at {url} ({e.reason})"
        ) from e


def _ensure_token() -> str:
    global _token, _token_at
    if _token and (time.time() - _token_at) < _TOKEN_TTL_S:
        return _token

    email = os.environ.get("DUNGML_EMAIL")
    password = os.environ.get("DUNGML_PASSWORD")
    if not email or not password:
        raise ConnectionError(
            "DungML credentials missing. Set DUNGML_EMAIL and DUNGML_PASSWORD "
            "in the environment (or the project .env file)."
        )

    payload = _http("POST", "/api/auth/login",
                    body={"email": email, "password": password})
    tok = payload.get("token")
    if not tok:
        raise PermissionError(f"login returned no token: {payload}")
    _token = tok
    _token_at = time.time()
    return tok


def list_projects() -> list[dict]:
    """Return all projects visible to the logged-in user.

    Each item: {id, name, ...}. The server pads with timestamps and
    other fields which callers can ignore.
    """
    return _http("GET", "/api/projects", token=_ensure_token())


def list_maps(project_id: str) -> list[dict]:
    """Return all maps in a project.

    Each item: {id, name, kind, ...}. `kind` is 'map' for dungeon maps
    and 'library' for include files (e.g. core.dmap).
    """
    return _http("GET", f"/api/projects/{project_id}/maps",
                 token=_ensure_token())


def get_map(map_id: str) -> dict:
    """Fetch a single map: {id, name, source, ...}. `source` is the DSL."""
    return _http("GET", f"/api/maps/{map_id}", token=_ensure_token())


def find_project(project_hint: str | None = None) -> dict:
    """Resolve a project name/UUID hint to a project dict.

    Matching order: exact UUID, then exact name (case-insensitive),
    then case-insensitive prefix. Raises LookupError if no match or
    multiple prefix matches.
    """
    projects = list_projects()
    if not project_hint:
        if len(projects) == 1:
            return projects[0]
        raise LookupError(
            "DUNGML_PROJECT not set and multiple projects visible: "
            + ", ".join(p.get("name", "?") for p in projects)
        )

    # UUID exact match
    for p in projects:
        if p.get("id") == project_hint:
            return p

    low = project_hint.lower().strip()
    exact = [p for p in projects if p.get("name", "").lower() == low]
    if exact:
        return exact[0]
    prefix = [p for p in projects if p.get("name", "").lower().startswith(low)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise LookupError(
            f"project hint '{project_hint}' is ambiguous: "
            + ", ".join(p.get("name", "?") for p in prefix)
        )
    raise LookupError(
        f"no project matches '{project_hint}'. visible: "
        + ", ".join(p.get("name", "?") for p in projects)
    )


def find_map(project_id: str, map_hint: str) -> dict:
    """Resolve a map name/UUID to a map dict within a project."""
    maps = [m for m in list_maps(project_id) if m.get("kind") != "library"]

    for m in maps:
        if m.get("id") == map_hint:
            return m

    low = map_hint.lower().strip()
    exact = [m for m in maps if m.get("name", "").lower() == low]
    if exact:
        return exact[0]
    # Loose match: contains all tokens (so "gatehouse" matches "Surface Level Gatehouse")
    tokens = [t for t in low.replace("_", " ").replace("-", " ").split() if t]
    contains = [
        m for m in maps
        if all(t in m.get("name", "").lower() for t in tokens)
    ]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        raise LookupError(
            f"map hint '{map_hint}' is ambiguous: "
            + ", ".join(m.get("name", "?") for m in contains)
        )
    raise LookupError(
        f"no map matches '{map_hint}'. available: "
        + ", ".join(m.get("name", "?") for m in maps)
    )


def parse_dsl(source: str) -> dict:
    """POST /api/dsl/parse — returns the typed model as JSON.

    Used to recover room geometry (rect / polygon / boundary / circle)
    for fog-of-war masking. Stateless: no auth required, no project
    context, no includes resolution — caller must inline includes
    upstream if the DSL uses `!include`.
    """
    return _http("POST", "/api/dsl/parse", body={"source": source})


# ----- play sessions (fog-of-war on the embedded /area widget) -----
#
# The embedded DungML play widget reads a *session* (server-side fog state),
# not map_state.json. These helpers let both the dashboard and the MCP tools
# bind to one canonical per-campaign session and push reveals/party moves to
# it. The campaign↔session binding lives in `<campaign>/dungml_sessions.json`
# (keyed by dungml map id).


def resolve_node_ids(source: str) -> dict[str, str]:
    """Map each node's bare name (room/corridor name in the DSL) to its
    graph node id (``<kind>.<name>``) via the connectivity endpoint."""
    conn = _http("POST", "/api/dsl/connectivity", body={"source": source})
    return {n["name"]: n["id"] for n in conn.get("nodes", [])}


def reveal_node(session_id: str, node_id: str) -> None:
    _http("POST", f"/api/sessions/{session_id}/reveal",
          body={"node": node_id}, token=_ensure_token())


def move_party(session_id: str, node_id: str) -> None:
    _http("POST", f"/api/sessions/{session_id}/move",
          body={"to": node_id}, token=_ensure_token())


def _sessions_store(campaign_dir) -> tuple["Path", dict]:
    p = Path(campaign_dir) / "dungml_sessions.json"
    store: dict = {}
    if p.exists():
        try:
            store = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            store = {}
    return p, store


def ensure_play_session(
    campaign_dir,
    map_id: str,
    source: str,
    *,
    seed_names: "list[str] | tuple[str, ...]" = (),
    session_name: str = "Campaign — party",
) -> str:
    """Return the play-session id bound to this campaign + map, creating and
    seeding it on first use (and recreating if it was deleted server-side).

    On creation the session's discovered nodes are seeded from ``seed_names``
    (resolved to node ids), and the party is placed on the last of them.
    Idempotent thereafter: an existing, still-present session is reused.
    """
    store_path, store = _sessions_store(campaign_dir)
    tok = _ensure_token()

    sid = store.get(map_id)
    if sid:
        try:
            _http("GET", f"/api/sessions/{sid}", token=tok)
            return sid  # still exists — reuse
        except LookupError:
            pass  # stale — recreate below

    try:
        name_to_id = resolve_node_ids(source)
    except Exception:
        name_to_id = {}  # no graph → empty session, seeded later via tools
    seed = [name_to_id[n] for n in seed_names if n in name_to_id]
    start = seed[-1] if seed else None

    sess = _http("POST", f"/api/maps/{map_id}/sessions",
                 body={"name": session_name, "start_location": start},
                 token=tok)
    sid = sess["id"]
    for nid in seed:
        if nid == start:
            continue  # start_location already revealed it
        try:
            _http("POST", f"/api/sessions/{sid}/reveal",
                  body={"node": nid}, token=tok)
        except Exception:
            pass  # best-effort seed

    store[map_id] = sid
    tmp = store_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
    tmp.replace(store_path)
    return sid
