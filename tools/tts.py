"""OpenAI gpt-4o-mini-tts narration with per-character voice mapping.

Splits a DM-turn into narration / character-speech segments, renders each
segment with the appropriate voice (configurable per campaign), and
caches the resulting MP3 on disk so re-clicks don't re-pay.

Speech detection uses markdown blockquotes (lines starting with ``>``),
matching the existing /play visual convention. Speaker attribution looks
for a bold/italic name prefix inside the quote (``**Tomas:**``,
``*Tomas* —``) or an English attribution verb (``Tomas says``,
``Tomas mutters``). Unattributed speech falls back to a generic NPC
voice. Inline quotes within prose are NOT segmented in v1 — the whole
prose paragraph is voiced by the narrator, embedded quotes included.

Voice mapping is per-campaign and persisted to
``campaigns/<slug>/audio_voices.json``::

    {
      "narrator": "alloy",
      "default_npc": "fable",
      "characters": { "tomas": "echo", "yorick": "onyx" }
    }
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

BASE_DIR = Path(__file__).parent.parent

# gpt-4o-mini-tts voice roster as of 2026-01. Includes the original 6
# plus the newer expressive voices added with the model.
VALID_VOICES = (
    "alloy", "ash", "ballad", "coral", "echo", "fable",
    "nova", "onyx", "sage", "shimmer",
)

DEFAULT_NARRATOR = "alloy"
DEFAULT_NPC = "fable"

NARRATION_INSTRUCTION = (
    "Narrate as a tabletop RPG dungeon master. Vary pacing and tone to "
    "match the scene: tense and clipped during combat, calm and "
    "measured during exposition, weighted and reflective for moments "
    "of loss. Read at a deliberate pace; do not rush."
)

SPEECH_INSTRUCTION = (
    "Voice this character's speech as a single in-character line. Let "
    "punctuation and word choice guide tone. Read attribution and "
    "stage directions in a softer, distinct register so the listener "
    "can tell them apart from the spoken words."
)


@dataclass
class Segment:
    kind: str            # "narration" | "speech"
    text: str
    speaker: str | None  # character slug or None when unattributed
    voice: str           # resolved voice name


# --- OpenAI client --------------------------------------------------------

def _load_openai():
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
    import os
    if not os.environ.get("OPENAI_API_KEY"):
        raise EnvironmentError(
            "OPENAI_API_KEY not set — add it to .env next to "
            "REPLICATE_API_TOKEN"
        )
    from openai import OpenAI
    return OpenAI()


# --- Voice-mapping persistence -------------------------------------------

def voice_map_path(campaign_dir: Path) -> Path:
    return campaign_dir / "audio_voices.json"


def load_voice_map(campaign_dir: Path) -> dict:
    p = voice_map_path(campaign_dir)
    default = {
        "narrator": DEFAULT_NARRATOR,
        "default_npc": DEFAULT_NPC,
        "characters": {},
    }
    if not p.exists():
        return default
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    # Defensive merge: keep unknown keys but ensure required ones exist.
    out = dict(default)
    out.update(data)
    if not isinstance(out.get("characters"), dict):
        out["characters"] = {}
    return out


def save_voice_map(campaign_dir: Path, vmap: dict) -> None:
    p = voice_map_path(campaign_dir)
    cleaned = {
        "narrator": vmap.get("narrator", DEFAULT_NARRATOR),
        "default_npc": vmap.get("default_npc", DEFAULT_NPC),
        "characters": {
            str(k): str(v) for k, v in (vmap.get("characters") or {}).items()
            if v in VALID_VOICES
        },
    }
    if cleaned["narrator"] not in VALID_VOICES:
        cleaned["narrator"] = DEFAULT_NARRATOR
    if cleaned["default_npc"] not in VALID_VOICES:
        cleaned["default_npc"] = DEFAULT_NPC
    p.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")


def known_character_slugs(campaign_dir: Path) -> list[str]:
    chars_dir = campaign_dir / "characters"
    if not chars_dir.is_dir():
        return []
    return sorted(p.stem for p in chars_dir.glob("*.md"))


# --- Speaker detection ---------------------------------------------------

# Match a speaker label prefix at line start. Permissive about how the
# markdown emphasis interleaves with the name and the colon — handles
# all of: ``**Tomas:**``, ``**Tomas**:``, ``*Tomas* —``, ``Tomas:``.
_NAME_PREFIX_RE = re.compile(
    r"^[\s\*]*([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)?)[\s\*]*[:—\-][\s\*]*",
    re.MULTILINE,
)

# "Name says/asks/whispers/..." anywhere near the quote.
_ATTRIBUTION_VERBS = (
    "says?", "said", "asks?", "asked",
    "whispers?", "whispered", "barks?", "barked",
    "growls?", "growled", "shouts?", "shouted",
    "mutters?", "muttered", "murmurs?", "murmured",
    "replies", "replied", "answers?", "answered",
    "grunts?", "hisses?", "spits?", "breathes?",
    "continues?", "adds?", "notes?", "laughs?",
    "sneers?", "warns?", "chuckles?",
    "interjects?", "interrupts?", "calls?",
    "intones?", "rasps?", "snarls?", "drawls?",
)
_ATTRIBUTION_RE = re.compile(
    r"\b([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)?)\s+(?:" + "|".join(_ATTRIBUTION_VERBS) + r")\b"
)


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _resolve_speaker(candidate: str, known: Iterable[str]) -> str | None:
    norm = _normalize_name(candidate)
    if not norm:
        return None
    known_list = list(known)
    # Exact slug match wins.
    for slug in known_list:
        if slug == norm:
            return slug
    # Prefix match either direction (slug 'tomas-the-warden' ↔ 'tomas').
    for slug in known_list:
        if slug.startswith(norm + "-") or norm.startswith(slug + "-"):
            return slug
    # First-token match.
    first = norm.split("-")[0]
    for slug in known_list:
        if slug.split("-")[0] == first:
            return slug
    return None


def _attribute_blockquote(quote_text: str, known: list[str]) -> str | None:
    m = _NAME_PREFIX_RE.search(quote_text)
    if m:
        sp = _resolve_speaker(m.group(1), known)
        if sp:
            return sp
    m = _ATTRIBUTION_RE.search(quote_text)
    if m:
        sp = _resolve_speaker(m.group(1), known)
        if sp:
            return sp
    return None


def parse_segments(text: str, known_chars: list[str], voice_map: dict) -> list[Segment]:
    """Split DM text into narration + speech segments."""
    narrator = voice_map.get("narrator", DEFAULT_NARRATOR)
    default_npc = voice_map.get("default_npc", DEFAULT_NPC)
    chars = voice_map.get("characters") or {}

    segments: list[Segment] = []
    buf_narration: list[str] = []

    def flush_narration():
        joined = "\n".join(buf_narration).strip()
        if joined:
            segments.append(Segment("narration", joined, None, narrator))
        buf_narration.clear()

    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            quote_text = "\n".join(quote_lines).strip()
            if quote_text:
                flush_narration()
                speaker = _attribute_blockquote(quote_text, known_chars)
                voice = chars.get(speaker, default_npc) if speaker else default_npc
                segments.append(Segment("speech", quote_text, speaker, voice))
        else:
            buf_narration.append(line)
            i += 1
    flush_narration()
    return segments


# --- Synthesis & cache ----------------------------------------------------

def _audio_dir(campaign_dir: Path) -> Path:
    d = campaign_dir / "audio"
    d.mkdir(exist_ok=True)
    return d


def _hash_key(text: str, segments: list[Segment]) -> str:
    """Cache key incorporates the text AND the resolved voices, so that
    re-mapping a character invalidates only that turn's cache entry."""
    h = hashlib.sha256()
    h.update(text.encode("utf-8"))
    for s in segments:
        h.update(b"|")
        h.update(s.kind.encode())
        h.update(b":")
        h.update(s.voice.encode())
        h.update(b":")
        h.update((s.speaker or "").encode())
    return h.hexdigest()[:24]


def _synthesize_segment(client, seg: Segment) -> bytes:
    instructions = (
        NARRATION_INSTRUCTION if seg.kind == "narration" else SPEECH_INSTRUCTION
    )
    resp = client.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice=seg.voice,
        input=seg.text,
        instructions=instructions,
        response_format="mp3",
    )
    # The OpenAI SDK exposes the raw bytes via .read() on the streamed
    # response, or .content for buffered. Try both for SDK-version safety.
    if hasattr(resp, "read") and callable(resp.read):
        return resp.read()
    return resp.content  # type: ignore[attr-defined]


def synthesize_turn(campaign_dir: Path, text: str) -> dict:
    """Render the DM turn to a single concatenated MP3, cached on disk.

    Returns a manifest::

        {
          "cache_key": str,            # filename stem under campaigns/<c>/audio/
          "filename": str,             # "<key>.mp3"
          "cached": bool,              # true if the file was reused
          "segments": [                # voice breakdown for the UI
            {"kind", "speaker", "voice", "char_count"}
          ],
          "total_chars": int,          # sum of synthesized text length
        }
    """
    voice_map = load_voice_map(campaign_dir)
    known = known_character_slugs(campaign_dir)
    segments = parse_segments(text, known, voice_map)
    if not segments:
        raise ValueError("no speakable segments in input")

    cache_key = _hash_key(text, segments)
    audio_dir = _audio_dir(campaign_dir)
    out_path = audio_dir / f"{cache_key}.mp3"

    cached = out_path.exists() and out_path.stat().st_size > 0
    if not cached:
        client = _load_openai()
        parts: list[bytes] = []
        for seg in segments:
            parts.append(_synthesize_segment(client, seg))
        # MP3 frame concat: each part is raw MP3 from the same encoder
        # with matching sample rate and channel layout, so binary concat
        # is decoded correctly by every modern audio engine. No ID3
        # rewriting needed.
        out_path.write_bytes(b"".join(parts))

    manifest = [
        {
            "kind": s.kind,
            "speaker": s.speaker,
            "voice": s.voice,
            "char_count": len(s.text),
        }
        for s in segments
    ]
    return {
        "cache_key": cache_key,
        "filename": out_path.name,
        "cached": cached,
        "segments": manifest,
        "total_chars": sum(len(s.text) for s in segments),
    }
