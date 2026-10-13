import re
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from app.importers.base import BaseImporter, NormalizedItem
from app.importers.registry import register
from app.importers.utils import (
    iter_files, load_json, guess_media_type, resolve_media_uri,
    extract_share_info, collect_message_attachments, detect_reaction, pick_message_content,
)
from app.importers.meta_common import parse_generic_category_file

# Instagram's own posts export is "posts_<N>.json" (paginated) at the export
# root -- NOT to be confused with unrelated same-prefix files buried in other
# categories (e.g. ads_information/.../posts_viewed.json,
# posts_you're_not_interested_in.json), hence the strict numeric suffix.
_OWN_POSTS_FILENAME_RE = re.compile(r"^posts_\d+\.json$")

# Files with their own dedicated parsing below -- excluded from the generic
# category scan so they aren't parsed twice.
_DEDICATED_FILENAME_PATTERNS = (
    lambda name: bool(_OWN_POSTS_FILENAME_RE.match(name)),
    lambda name: name.startswith("message_") and name.endswith(".json"),
)


@register
class InstagramImporter(BaseImporter):
    key = "instagram"
    label = "Instagram"

    def detect(self, root: Path) -> bool:
        if any(root.rglob("posts_1.json")):
            return True
        if any(root.rglob("your_instagram_activity")):
            return True
        if any("instagram" in str(p).lower() for p in root.glob("*")):
            return True
        return False

    def parse(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_posts(root)
        yield from self._parse_messages(root)
        yield from self._parse_generic_categories(root)

    # -- posts --------------------------------------------------------

    def _parse_posts(self, root: Path) -> Iterator[NormalizedItem]:
        for path in iter_files(root, "posts_*.json"):
            if not _OWN_POSTS_FILENAME_RE.match(path.name):
                continue
            try:
                data = load_json(path, fix_encoding=True)
            except Exception:
                continue
            posts = data if isinstance(data, list) else data.get("posts", data.get("media", []))
            for post in posts:
                if not isinstance(post, dict):
                    continue
                try:
                    yield self._normalize_post(root, post)
                except Exception:
                    continue

    def _normalize_post(self, root: Path, post: dict) -> NormalizedItem:
        media_entries = post.get("media") or []
        if isinstance(media_entries, dict):
            media_entries = [media_entries]

        ts = post.get("creation_timestamp")
        if not ts and media_entries:
            ts = media_entries[0].get("creation_timestamp")
        timestamp = datetime.fromtimestamp(ts) if ts else None

        texts = [m.get("title") for m in media_entries if isinstance(m, dict) and m.get("title")]
        if post.get("title") and post["title"] not in texts:
            texts.insert(0, post["title"])

        media = []
        for m in media_entries:
            uri = m.get("uri") if isinstance(m, dict) else None
            if uri:
                rel = resolve_media_uri(root, uri)
                if rel:
                    media.append((rel, guess_media_type(rel)))

        return NormalizedItem(
            item_type="post",
            category="posts",
            timestamp=timestamp,
            title=None,
            content="\n".join(texts) or None,
            author=None,
            context=None,
            media=media,
            raw=post,
        )

    # -- messages (same schema Meta uses for Facebook) -------------------

    def _parse_messages(self, root: Path) -> Iterator[NormalizedItem]:
        for path in iter_files(root, "message_*.json"):
            try:
                data = load_json(path, fix_encoding=True)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            thread_title = data.get("title")
            for message in data.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                try:
                    normalized = self._normalize_message(root, message, thread_title)
                except Exception:
                    continue
                if normalized is not None:
                    yield normalized

    def _normalize_message(self, root: Path, message: dict, thread_title) -> Optional[NormalizedItem]:
        media, external_links = collect_message_attachments(root, message)

        share_fields, share_content = extract_share_info(message.get("share"))
        content = pick_message_content(message.get("content"), share_content)
        # A "reaction" (emoji reacted to a message, or to a story shared in
        # the chat) shows up as its own entry with nothing else to it -- no
        # text, no media, no shared content. Left in, it renders as an empty
        # chat bubble; it isn't a message someone sent, so it's dropped.
        if message.get("reactions") and not content and not media:
            return None

        fields = {**share_fields, **external_links} or None
        # Meta sometimes represents a reaction as its own synthetic message
        # ("X reacted 😍 to your message") instead of attaching it to the
        # target message's "reactions" array -- the emoji is pulled out so
        # the chat view can show it as a small reaction pill, not a bubble.
        reaction = detect_reaction(content)
        if reaction:
            reactor, emoji = reaction
            fields = {**(fields or {}), "Réaction": emoji, "Réaction de": reactor or message.get("sender_name")}

        ts_ms = message.get("timestamp_ms")
        timestamp = datetime.fromtimestamp(ts_ms / 1000) if ts_ms else None

        return NormalizedItem(
            item_type="message",
            category="messages",
            timestamp=timestamp,
            title=None,
            content=content,
            author=message.get("sender_name"),
            context=thread_title,
            fields=fields,
            media=media,
            raw=message,
        )

    # -- everything else: Meta's generic label/value export shapes ------

    def _parse_generic_categories(self, root: Path) -> Iterator[NormalizedItem]:
        for path in iter_files(root, "*.json"):
            if any(pattern(path.name) for pattern in _DEDICATED_FILENAME_PATTERNS):
                continue
            yield from parse_generic_category_file(root, path, activity_root="your_instagram_activity")
