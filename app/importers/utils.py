import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple


def fix_mojibake(value: Any) -> Any:
    """Meta (Facebook/Instagram) JSON exports mis-encode non-ASCII text: UTF-8
    bytes get decoded as Latin-1 and re-escaped. This reverses that."""
    if isinstance(value, str):
        try:
            return value.encode("latin1").decode("utf8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return value
    if isinstance(value, list):
        return [fix_mojibake(v) for v in value]
    if isinstance(value, dict):
        return {k: fix_mojibake(v) for k, v in value.items()}
    return value


def load_json(path: Path, fix_encoding: bool = False) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return fix_mojibake(data) if fix_encoding else data


def iter_files(root: Path, pattern: str) -> Iterator[Path]:
    yield from sorted(root.rglob(pattern))


MEDIA_EXTENSIONS = {
    ".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".gif": "photo", ".webp": "photo", ".heic": "photo",
    ".mp4": "video", ".mov": "video", ".avi": "video", ".webm": "video",
    ".mp3": "audio", ".m4a": "audio", ".wav": "audio", ".ogg": "audio",
}


def guess_media_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    return MEDIA_EXTENSIONS.get(ext, "other")


def extract_share_info(share: Any) -> Tuple[Dict[str, str], Optional[str]]:
    """A message's "share" object (a Reel/post/link shared into the chat,
    as opposed to a locally-uploaded photo/video) has no fixed, documented
    schema and its field names vary across export versions -- so this stays
    permissive: whatever recognizable field is present is kept verbatim
    under a readable label, nothing is assumed mandatory. Returns
    (fields, content) where content is a best-effort human-readable summary
    so the message isn't left blank when there's no "content" text of its
    own (a plain link share has none)."""
    if not isinstance(share, dict):
        return {}, None
    fields: Dict[str, str] = {}
    for key, label in (
        ("share_text", "Texte du partage"),
        ("text", "Texte du partage"),
        ("link", "Lien partagé"),
        ("original_content_owner", "Auteur d'origine"),
    ):
        value = share.get(key)
        if isinstance(value, str) and value and label not in fields:
            fields[label] = value
    content = fields.get("Texte du partage")
    if not content and "Lien partagé" in fields:
        content = f"[Partage] {fields['Lien partagé']}"
    return fields, content


# Meta auto-generates a plain-language "content" for a reaction instead of
# always keeping it attached to the target message's own "reactions" array
# -- when it does, the reaction shows up as its own message with no other
# content, and would otherwise render as a blank bubble. Wording varies by
# export locale, AND the leading name is sometimes just missing ("Reacted 😂
# to your message", capitalized, no one named -- the reactor is only in the
# message's own "sender_name" then), so the name group is optional in both.
_REACTION_CONTENT_RE = re.compile(r"^(?:(?P<name>.+?)\s+)?reacted\s+(?P<emoji>\S+)\s+to\b", re.IGNORECASE)
_REACTION_CONTENT_FR_RE = re.compile(
    r"^(?:(?P<name>.+?)\s+)?a réagi(?:\(e\))?\s+(?:avec\s+)?(?P<emoji>\S+)\s+(?:à|au|a)\b", re.IGNORECASE
)


def detect_reaction(content: Optional[str]) -> Optional[Tuple[Optional[str], str]]:
    """(reactor_name, emoji) parsed out of a synthetic reaction "content"
    sentence, or None if `content` isn't one."""
    if not content:
        return None
    match = _REACTION_CONTENT_RE.search(content) or _REACTION_CONTENT_FR_RE.search(content)
    if not match:
        return None
    name = match.group("name")
    return (name.strip() or None if name else None), match.group("emoji")


# Meta fills "content" with a generic caption when a message is really just
# a share/attachment carrier ("Vous avez envoyé une pièce jointe." /
# "sent an attachment."). Left as the message's content, that generic
# sentence hides genuinely useful information a share brings (the actual
# text of the shared post, who posted it) behind a caption that says nothing.
_GENERIC_ATTACHMENT_RE = re.compile(
    r"^(?:vous avez |tu as |.+? a )?envoyée?\s+une pièce jointe\.?$|"
    r"^(?:.+? )?sent an attachment\.?$",
    re.IGNORECASE,
)


def is_generic_attachment_notice(content: Optional[str]) -> bool:
    return bool(content) and bool(_GENERIC_ATTACHMENT_RE.match(content.strip()))


def pick_message_content(raw_content: Optional[str], share_content: Optional[str]) -> Optional[str]:
    """What to show as the message's content: real typed text always wins;
    Meta's generic "you sent an attachment" caption is treated as no content
    at all so the share's own text (far more informative) takes its place;
    the generic caption is the last resort, kept only when nothing else is
    available at all (still better than a blank bubble)."""
    if raw_content and not is_generic_attachment_notice(raw_content):
        return raw_content
    return share_content or raw_content


def resolve_media_uri(root: Path, uri: str) -> str | None:
    """Given a `uri` field from a Meta export JSON (relative path, possibly
    with a leading segment that doesn't exist at `root`), return a path
    relative to `root` if a matching file actually exists on disk."""
    candidate = (root / uri).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    if candidate.is_file():
        return Path(uri).as_posix()
    # Some exports nest the export root one level deeper than expected;
    # fall back to matching by filename anywhere under root.
    matches = list(root.rglob(Path(uri).name))
    if matches:
        return matches[0].relative_to(root).as_posix()
    return None


# Every key a message's attachments can show up under. Facebook and
# Instagram share the same message schema, but earlier code only checked a
# different subset per platform (e.g. Instagram never looked at
# "gifs"/"files"/"sticker") -- a message using one of the missed keys had no
# way to show its attachment at all, only Meta's auto-generated caption
# ("X sent an attachment.").
MESSAGE_ATTACHMENT_KEYS = ("photos", "videos", "audio_files", "gifs", "files", "sticker", "share")


def collect_message_attachments(root: Path, message: Dict[str, Any]) -> Tuple[list, Dict[str, str]]:
    """(media, external_links) for every attachment-carrying key on a
    message. `media` is (relative_path, media_type) pairs that resolved to a
    real file in the export. A "uri" that doesn't resolve locally but is
    itself a full URL (a GIF picked from Giphy by reference, say, never
    actually bundled into the export) is kept as a labelled link instead of
    silently vanishing."""
    media = []
    external_links: Dict[str, str] = {}
    for key in MESSAGE_ATTACHMENT_KEYS:
        value = message.get(key)
        entries = value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])
        for i, entry in enumerate(entries):
            uri = entry.get("uri") if isinstance(entry, dict) else None
            if not uri:
                continue
            rel = resolve_media_uri(root, uri)
            if rel:
                media.append((rel, guess_media_type(rel)))
            elif uri.startswith("http://") or uri.startswith("https://"):
                label = key.rstrip("s").capitalize() + (f" {i + 1}" if len(entries) > 1 else "")
                external_links[label] = uri
    return media, external_links
