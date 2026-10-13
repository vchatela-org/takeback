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

# Facebook's own posts export is "your_posts_<N>.json" (paginated) at the
# export root -- kept strict (numeric suffix) so an unrelated same-prefix
# file in another category can't be mistaken for it, the way Instagram's
# "posts_*.json" collides with ads_information/.../posts_viewed.json.
_OWN_POSTS_FILENAME_RE = re.compile(r"^your_posts_\d+\.json$")

# Files with their own dedicated parsing below -- excluded from the generic
# category scan so they aren't parsed twice.
_DEDICATED_FILENAME_PATTERNS = (
    lambda name: bool(_OWN_POSTS_FILENAME_RE.match(name)),
    lambda name: name.startswith("message_") and name.endswith(".json"),
)


@register
class FacebookImporter(BaseImporter):
    key = "facebook"
    label = "Facebook"

    def detect(self, root: Path) -> bool:
        if any(root.rglob("your_posts*.json")):
            return True
        if any(p.parent.name == "inbox" and "facebook" in str(p).lower() for p in root.rglob("message_1.json")):
            return True
        if any(root.rglob("profile_information.json")):
            return True
        # An account with no posts and no messages (e.g. used mostly for
        # groups/marketplace/ads) has none of the signals above -- but the
        # export still sits under Meta's activity umbrella folder, or carries
        # a Facebook-specific category folder no other source has. Current
        # exports name that top folder "your_activity_across_facebook"
        # (renamed at some point from the older "your_facebook_activity" --
        # both are checked since exports requested a while ago may still use
        # the old name).
        if any(root.rglob("your_activity_across_facebook")):
            return True
        if any(root.rglob("your_facebook_activity")):
            return True
        if any(root.rglob("apps_and_websites_off_of_facebook")):
            return True
        return False

    def parse(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_posts(root)
        yield from self._parse_messages(root)
        yield from self._parse_generic_categories(root)

    # -- posts --------------------------------------------------------

    def _parse_posts(self, root: Path) -> Iterator[NormalizedItem]:
        for path in iter_files(root, "your_posts*.json"):
            if not _OWN_POSTS_FILENAME_RE.match(path.name):
                continue
            try:
                data = load_json(path, fix_encoding=True)
            except Exception:
                continue
            posts = data if isinstance(data, list) else data.get("posts", [])
            for post in posts:
                if not isinstance(post, dict):
                    continue
                try:
                    yield self._normalize_post(root, post)
                except Exception:
                    continue

    def _normalize_post(self, root: Path, post: dict) -> NormalizedItem:
        ts = post.get("timestamp")
        timestamp = datetime.fromtimestamp(ts) if ts else None

        texts = []
        media = []
        for entry in post.get("data") or []:
            if isinstance(entry, dict) and entry.get("post"):
                texts.append(entry["post"])
        for attachment in post.get("attachments") or []:
            for att_data in attachment.get("data") or []:
                media_info = att_data.get("media") if isinstance(att_data, dict) else None
                if media_info and media_info.get("uri"):
                    rel = resolve_media_uri(root, media_info["uri"])
                    if rel:
                        media.append((rel, guess_media_type(rel)))

        return NormalizedItem(
            item_type="post",
            category="posts",
            timestamp=timestamp,
            title=post.get("title"),
            content="\n".join(texts) or None,
            author=None,
            context=None,
            media=media,
            raw=post,
        )

    # -- messages -------------------------------------------------------

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
            yield from parse_generic_category_file(
                root, path, activity_root=("your_activity_across_facebook", "your_facebook_activity")
            )
