#!/usr/bin/env python3
"""
yoto_podcast_sync.py — Sync an Apple Podcast feed to a Yoto "Make Your Own" playlist.

Workflow:
    1. Give it an Apple Podcasts URL. It resolves the podcast ID via the
       iTunes Search API, fetches the RSS feed, and enumerates every episode.
    2. It creates (or reuses) a Yoto playlist and uploads only the episodes
       that are not already there (matched by episode GUID, then by title).
    3. New episodes are appended; episodes that fall outside the
       --max-episodes window are dropped, so the playlist mirrors the feed.

Usage:
    pip install requests feedparser
    python yoto_podcast_sync.py login            # one-time: browser OAuth
    python yoto_podcast_sync.py sync <apple-podcasts-url>

First run needs a Yoto client ID (free, one minute):
    1. Go to https://dashboard.yoto.dev and sign in with your Yoto account.
    2. Create a new client, type "public" (native/SPA/no client secret).
    3. Register the redirect URI exactly:  http://127.0.0.1:8787/callback
    4. Copy the client ID and either export YOTO_CLIENT_ID=<id> or pass
       --client-id on the login command.

The Yoto API is the same one Yoto's own app uses (api.yotoplay.com, OAuth2 +
PKCE). This script reimplements the flows documented by the community
yoto-cli project (lizozom/yoto-cli) in dependency-light Python.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

try:
    import feedparser
except ImportError:
    feedparser = None

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

AUTH_BASE = "https://login.yotoplay.com"
API_BASE = "https://api.yotoplay.com"
AUDIENCE = "https://api.yotoplay.com"
SCOPE = "offline_access family:library:view user:content:manage"
REDIRECT_URI = "http://127.0.0.1:8787/callback"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"

CONFIG_DIR = Path.home() / ".yoto-podcast-sync"
TOKENS_FILE = CONFIG_DIR / "tokens.json"
STATE_FILE = CONFIG_DIR / "state.json"

UA = {"User-Agent": "yoto-podcast-sync/1.0 (+https://github.com)"}

# Yoto Make-Your-Own cards hold up to 100 tracks / 500 MB.
DEFAULT_MAX_EPISODES = 100
TRANSCODE_POLL_INTERVAL = 5
TRANSCODE_MAX_ATTEMPTS = 60  # ~5 minutes, same as yoto-cli


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def die(msg: str, code: int = 1) -> "NoReturn":
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip()).casefold()


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fmt_dur(seconds) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


# --------------------------------------------------------------------------
# Yoto API client
# --------------------------------------------------------------------------

class YotoAuthError(Exception):
    pass


class YotoClient:
    """Minimal Yoto API client: OAuth2 PKCE login + content/media endpoints."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.expires_at: float = 0.0
        self._load_tokens()

    # -- token persistence -------------------------------------------------
    def _load_tokens(self) -> None:
        try:
            data = json.loads(TOKENS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if data.get("client_id") != self.client_id:
            return  # tokens belong to a different client
        self.access_token = data.get("access_token")
        self.refresh_token = data.get("refresh_token")
        self.expires_at = data.get("expires_at", 0.0)

    def _save_tokens(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TOKENS_FILE.write_text(json.dumps({
            "client_id": self.client_id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }, indent=2))

    @property
    def logged_in(self) -> bool:
        return bool(self.access_token and self.refresh_token)

    # -- OAuth PKCE login ---------------------------------------------------
    def login(self) -> None:
        verifier = b64url(secrets.token_bytes(32))
        challenge = b64url(hashlib.sha256(verifier.encode()).digest())
        state = b64url(secrets.token_bytes(16))

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "audience": AUDIENCE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        url = f"{AUTH_BASE}/authorize?{urlencode(params)}"
        print("Opening your browser to sign in to Yoto...")
        print(f"If it doesn't open, visit:\n  {url}\n")
        webbrowser.open(url)

        code = self._catch_redirect_code(expected_state=state)
        tokens = self._token_request({
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT_URI,
        })
        self._store_token_response(tokens)
        print("Logged in. Tokens saved to", TOKENS_FILE)

    def _catch_redirect_code(self, expected_state: str) -> str:
        """Run a loopback server briefly to catch the OAuth redirect."""
        result: dict = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                qs = parse_qs(urlparse(self.path).query)
                result["code"] = qs.get("code", [None])[0]
                result["state"] = qs.get("state", [None])[0]
                result["error"] = qs.get("error", [None])[0]
                body = (b"<html><body><h2>Logged in to Yoto.</h2>"
                        b"<p>You can close this tab and return to the terminal.</p>"
                        b"</body></html>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # noqa: N802
                pass

        server = HTTPServer(("127.0.0.1", 8787), Handler)
        server.timeout = 300
        print(f"Waiting for the browser callback on {REDIRECT_URI} ...")
        server.handle_request()  # one shot

        if result.get("error"):
            die(f"Yoto refused the login: {result['error']}")
        if not result.get("code"):
            die("No authorization code received. Try `login` again.")
        if result.get("state") != expected_state:
            die("State mismatch — possible CSRF. Aborting.")
        return result["code"]

    def _token_request(self, data: dict) -> dict:
        r = requests.post(f"{AUTH_BASE}/oauth/token", data=data, timeout=30)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if not r.ok:
            raise YotoAuthError(body.get("error_description") or body.get("error")
                                or f"token request failed: HTTP {r.status_code}")
        return body

    def _store_token_response(self, tokens: dict) -> None:
        self.access_token = tokens["access_token"]
        self.refresh_token = tokens.get("refresh_token", self.refresh_token)
        self.expires_at = time.time() + tokens.get("expires_in", 3600) - 60
        self._save_tokens()

    def refresh(self) -> None:
        if not self.refresh_token:
            raise YotoAuthError("No refresh token. Run `login` first.")
        tokens = self._token_request({
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": self.refresh_token,
        })
        self._store_token_response(tokens)

    # -- API calls -----------------------------------------------------------
    def _api(self, method: str, path: str, **kw) -> dict:
        if not self.access_token:
            raise YotoAuthError("Not logged in. Run `login` first.")
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.access_token}"
        if "json" in kw:
            headers["Content-Type"] = "application/json"
        r = requests.request(method, f"{API_BASE}{path}",
                             headers=headers, timeout=60, **kw)
        if r.status_code == 401 and self.refresh_token:
            self.refresh()
            headers["Authorization"] = f"Bearer {self.access_token}"
            r = requests.request(method, f"{API_BASE}{path}",
                                 headers=headers, timeout=60, **kw)
        if not r.ok:
            try:
                msg = r.json().get("error", {}).get("message")
            except ValueError:
                msg = None
            raise RuntimeError(msg or f"Yoto API {method} {path}: HTTP {r.status_code}")
        return r.json()

    # content (playlists)
    def list_content(self) -> list:
        return self._api("GET", "/content/mine").get("cards", [])

    def get_content(self, card_id: str) -> dict:
        return self._api("GET", f"/content/{card_id}")["card"]

    def create_content(self, title: str, metadata: dict | None = None) -> dict:
        body = {
            "title": title,
            "content": {
                "chapters": [],
                "playbackType": "linear",
                "activity": "yoto_Player",
                "version": "1",
                "restricted": True,
            },
            "metadata": metadata or {},
        }
        return self._api("POST", "/content", json=body)["card"]

    def update_content(self, card_id: str, title: str,
                       content: dict, metadata: dict | None = None) -> dict:
        body = {"cardId": card_id, "title": title,
                "content": content, "metadata": metadata or {}}
        return self._api("POST", "/content", json=body)["card"]

    # audio upload + transcode
    def get_audio_upload_url(self, sha256: str, filename: str) -> dict:
        q = urlencode({"sha256": sha256, "filename": filename})
        return self._api("GET", f"/media/transcode/audio/uploadUrl?{q}")["upload"]

    def get_transcode_status(self, upload_id: str) -> dict:
        return self._api(
            "GET", f"/media/upload/{upload_id}/transcoded?loudnorm=false")["transcode"]

    def upload_icon(self, data: bytes, filename: str) -> str:
        """Upload a chapter icon; returns the mediaId."""
        q = urlencode({"autoConvert": "true", "filename": filename})
        r = requests.post(
            f"{API_BASE}/media/displayIcons/user/me/upload?{q}",
            headers={"Authorization": f"Bearer {self.access_token}",
                     "Content-Type": "image/jpeg"},
            data=data, timeout=60)
        if not r.ok:
            raise RuntimeError(f"icon upload failed: HTTP {r.status_code}")
        return r.json()["displayIcon"]["mediaId"]

# --------------------------------------------------------------------------
# Podcast side: Apple URL -> iTunes lookup -> RSS -> episodes
# --------------------------------------------------------------------------

class Episode:
    def __init__(self, guid: str, title: str, url: str,
                 published_ts: float, published_str: str):
        self.guid = guid
        self.title = title
        self.url = url
        self.published_ts = published_ts
        self.published_str = published_str


def apple_id_from_url(url: str) -> str:
    m = re.search(r"/id(\d+)", url)
    if not m:
        die(f"Could not find a podcast id (…/id123456) in: {url}")
    return m.group(1)


def resolve_podcast(apple_url: str):
    """Return (podcast_title, author, feed_url, artwork_url)."""
    pid = apple_id_from_url(apple_url)
    r = requests.get(ITUNES_LOOKUP, params={"id": pid, "entity": "podcast"},
                     headers=UA, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("resultCount", 0) == 0:
        die(f"iTunes lookup found no podcast for id {pid}")
    info = data["results"][0]
    feed_url = info.get("feedUrl")
    if not feed_url:
        die(f"iTunes has no RSS feed URL for '{info.get('collectionName')}'")
    return (info.get("collectionName", f"Podcast {pid}"),
            info.get("artistName", ""),
            feed_url,
            info.get("artworkUrl600"))


def fetch_episodes(feed_url: str) -> list[Episode]:
    if feedparser is None:
        die("The 'feedparser' package is required: pip install feedparser")
    r = requests.get(feed_url, headers=UA, timeout=60)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    episodes = []
    for i, e in enumerate(feed.entries):
        enc = None
        for x in e.get("enclosures", []):
            href = x.get("href")
            if not href:
                continue
            if (x.get("type") or "").startswith("audio/"):
                enc = href
                break
            enc = enc or href
        if not enc:
            print(f"  warning: skipping '{e.get('title')}' (no audio enclosure)")
            continue
        guid = e.get("id") or enc
        pp = e.get("published_parsed")
        ts = time.mktime(pp) if pp else 0.0
        episodes.append(Episode(
            guid=guid,
            title=(e.get("title") or f"Episode {i + 1}").strip(),
            url=enc,
            published_ts=ts,
            published_str=e.get("published", ""),
        ))
    return episodes


def download_episode(url: str, dest: Path) -> str:
    """Stream-download to dest, return hex sha256."""
    h = hashlib.sha256()
    with requests.get(url, headers=UA, timeout=60, stream=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                if chunk:
                    h.update(chunk)
                    f.write(chunk)
    if dest.stat().st_size == 0:
        raise RuntimeError("downloaded file is empty")
    return h.hexdigest()


def canon_epnum(s: str) -> str:
    """Canonicalize an episode number: EP08 -> EP8, ep136 -> EP136."""
    m = re.search(r"EP0*(\d+)", s, re.I)
    return f"EP{m.group(1)}" if m else s.upper()


def match_episodes(ordered: list[Episode], wanted: list[str]) -> list[Episode]:
    """Match a hand-picked list (EP numbers, titles, or YouTube titles)
    against feed episodes, preserving the list's order (most popular first).

    Returns the matched episodes; prints a warning for lines that match
    nothing.
    """
    by_epnum: dict[str, Episode] = {}
    by_title: dict[str, Episode] = {}
    for e in ordered:
        m = re.match(r"(EP\d+)", e.title, re.I)
        if m:
            by_epnum.setdefault(canon_epnum(m.group(1)), e)
        by_title[norm_title(e.title)] = e

    result: list[Episode] = []
    for w in wanted:
        ep = None
        m = re.search(r"EP\d+", w, re.I)
        if m:
            ep = by_epnum.get(canon_epnum(m.group(0)))
        if ep is None:
            ep = by_title.get(norm_title(w))
        if ep is None:
            # Fallback: match on the story name (strip EP prefix and any
            # parenthetical theme, then substring-match).
            name = re.sub(r"^EP\d+\s*", "", w, flags=re.I).strip()
            name = re.split(r"[（(]", name)[0].strip()
            if name:
                for cand in ordered:
                    if norm_title(name) in norm_title(cand.title):
                        ep = cand
                        break
        if ep is None:
            print(f"  warning: no feed episode matched '{w}'")
        elif ep not in result:
            result.append(ep)
    return result


# --------------------------------------------------------------------------
# Sync state
# --------------------------------------------------------------------------

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"cards": {}}


def save_state(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------
# Yoto playlist building blocks
# --------------------------------------------------------------------------

def make_chapter(index: int, title: str, track_url: str,
                 duration, icon_ref: str | None) -> dict:
    chapter = {
        "key": f"{index:02d}",          # API requires keys <= 20 chars
        "title": title,
        "tracks": [{
            "key": "01",
            "title": title,
            "trackUrl": track_url,
            "type": "audio",
            "duration": duration,
        }],
        "overlayLabel": str(index + 1),
        "availableFrom": None,
        "ambient": None,
        "defaultTrackDisplay": None,
        "defaultTrackAmbient": None,
    }
    if icon_ref:
        chapter["display"] = {"icon16x16": icon_ref}
    return chapter


def upload_and_transcode(client: YotoClient, path: Path,
                         filename: str, dry_run: bool = False):
    """Upload an audio file to Yoto and wait for transcoding.

    Returns (track_url like 'yoto:#<sha>', duration_seconds).
    With dry_run, returns placeholders without touching the API.
    """
    if dry_run:
        return "yoto:#dryrun", None
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    up = client.get_audio_upload_url(sha256, filename)
    upload_id = up["uploadId"]
    if up.get("uploadUrl"):
        print(f"    uploading {path.stat().st_size / 1e6:.1f} MB ...")
        r = requests.put(up["uploadUrl"], data=path.read_bytes(), timeout=300)
        r.raise_for_status()
    else:
        print("    already on Yoto's server (sha256 match), skipping upload")
    for _ in range(TRANSCODE_MAX_ATTEMPTS):
        tc = client.get_transcode_status(upload_id)
        phase = (tc.get("progress") or {}).get("phase", "")
        tsha = tc.get("transcodedSha256")
        if phase == "complete" or tsha:
            dur = (tc.get("transcodedInfo") or {}).get("duration")
            return f"yoto:#{tsha}", dur
        if phase and phase not in ("queued", "analyzing", "processing", "transcoding"):
            raise RuntimeError(f"transcoding failed, phase={phase!r}")
        time.sleep(TRANSCODE_POLL_INTERVAL)
    raise RuntimeError("transcoding timed out (~5 min)")


def find_or_create_playlist(client: YotoClient, podcast_title: str,
                            author: str, feed_url: str,
                            card_id: str | None, dry_run: bool):
    if card_id:
        card = client.get_content(card_id)
        print(f"Using playlist '{card['title']}' ({card_id})")
        return card
    for c in client.list_content():
        if norm_title(c.get("title", "")) == norm_title(podcast_title):
            card = client.get_content(c["cardId"])
            print(f"Found existing playlist '{card['title']}' ({c['cardId']})")
            return card
    print(f"Creating playlist '{podcast_title}' ...")
    if dry_run:
        return {"cardId": "dryrun", "title": podcast_title,
                "content": {"chapters": []}, "metadata": {}}
    card = client.create_content(
        podcast_title,
        metadata={"author": author,
                  "description": f"Synced from {feed_url}"})
    print(f"Created playlist, card id: {card['cardId']}")
    return card


# --------------------------------------------------------------------------
# The sync
# --------------------------------------------------------------------------

def cmd_sync(args) -> None:
    client = YotoClient(resolve_client_id(args))
    if not client.logged_in:
        die("Not logged in. Run `python yoto_podcast_sync.py login` first.")

    print("Resolving podcast ...")
    podcast_title, author, feed_url, artwork_url = resolve_podcast(args.url)
    print(f"  {podcast_title}")
    print(f"  RSS: {feed_url}")

    print("Fetching episodes ...")
    episodes = fetch_episodes(feed_url)
    if not episodes:
        die("No episodes with audio found in the feed.")
    # Chronological order (feed order breaks timestamp ties), oldest first.
    indexed = list(enumerate(episodes))
    indexed.sort(key=lambda t: (t[1].published_ts, t[0]))
    ordered = [e for _, e in indexed]
    print(f"  {len(ordered)} episodes in feed")

    # Episode selection: either a hand-picked list (popularity order) or
    # the newest-N window (chronological, sliding as new episodes arrive).
    if args.episode_list:
        wanted = [l.strip() for l in Path(args.episode_list).read_text(
            encoding="utf-8").splitlines() if l.strip()]
        window = match_episodes(ordered, wanted)
        dropped = []
        print(f"  syncing {len(window)} episodes from list "
              f"({len(wanted) - len(window)} unmatched)")
    else:
        window = ordered[-args.max_episodes:] if args.max_episodes < len(ordered) else ordered
        dropped = ordered[:len(ordered) - len(window)]
        if args.newest_first:
            window = window[::-1]
        print(f"  syncing {len(window)} episodes"
              + (f" (dropping {len(dropped)} older ones)" if dropped else ""))

    card = find_or_create_playlist(client, args.title or podcast_title,
                                   author, feed_url, args.card_id, args.dry_run)
    card_id = card["cardId"]

    # Index what's already on the card: normalized title -> (chapter, track).
    existing: dict[str, tuple[dict, dict]] = {}
    for ch in card.get("content", {}).get("chapters", []):
        for tr in ch.get("tracks", []):
            existing.setdefault(norm_title(tr.get("title", "")), (ch, tr))

    state = load_state()
    card_state = state["cards"].setdefault(card_id, {
        "feed_url": feed_url, "title": card["title"], "episodes": {}})
    known: dict = card_state["episodes"]  # guid -> {title, track_url, duration}

    # Optional: upload the podcast artwork once, reuse for every chapter.
    icon_ref = None
    if args.artwork and artwork_url and not args.dry_run:
        icon_ref = card_state.get("artwork_icon_ref")
        if not icon_ref:
            print("Uploading podcast artwork as chapter icon ...")
            img = requests.get(artwork_url, headers=UA, timeout=60).content
            media_id = client.upload_icon(img, "podcast-artwork.jpg")
            icon_ref = f"yoto:#{media_id}"
            card_state["artwork_icon_ref"] = icon_ref

    new_chapters: list[dict] = []
    n_reused = n_adopted = n_uploaded = 0
    claimed_tracks = set()

    def flush_playlist(chapters: list[dict]) -> None:
        """Write the current chapter list to Yoto (skipped in dry-run)."""
        if args.dry_run:
            return
        content = dict(card.get("content") or {})
        content["chapters"] = list(chapters)
        client.update_content(card_id, card["title"], content,
                              card.get("metadata"))

    with tempfile.TemporaryDirectory(prefix="yoto-sync-") as tmp:
        for i, ep in enumerate(window):
            rec = known.get(ep.guid)
            if rec and rec.get("track_url"):
                track_url, duration = rec["track_url"], rec.get("duration")
                n_reused += 1
                status = "kept"
            else:
                hit = existing.get(norm_title(ep.title))
                if hit and id(hit[1]) not in claimed_tracks:
                    _ch, tr = hit
                    track_url = tr.get("trackUrl")
                    duration = tr.get("duration")
                    claimed_tracks.add(id(tr))
                    known[ep.guid] = {"title": ep.title, "track_url": track_url,
                                      "duration": duration,
                                      "added": datetime.now(timezone.utc).isoformat()}
                    n_adopted += 1
                    status = "adopted from playlist"
                else:
                    print(f"[{i + 1}/{len(window)}] NEW  {ep.title}")
                    if not args.dry_run:
                        dest = Path(tmp) / f"ep{i}.mp3"
                        print(f"    downloading ...")
                        download_episode(ep.url, dest)
                        track_url, duration = upload_and_transcode(
                            client, dest, f"{ep.title[:80]}.mp3")
                        known[ep.guid] = {
                            "title": ep.title, "track_url": track_url,
                            "duration": duration,
                            "added": datetime.now(timezone.utc).isoformat()}
                    else:
                        track_url, duration = "yoto:#dryrun", None
                    n_uploaded += 1
                    status = "uploaded"
            new_chapters.append(make_chapter(i, ep.title, track_url,
                                             duration, icon_ref))
            if status != "kept":
                print(f"         -> {status} ({fmt_dur(duration)})")
            # Incremental save: playlist + state hit disk after every new
            # episode, so progress is visible live and Ctrl+C loses nothing.
            if not args.dry_run and status != "kept":
                save_state(state)
                flush_playlist(new_chapters)
                print(f"    playlist saved "
                      f"({len(new_chapters)}/{len(window)} chapters)")

    # Report episodes falling out of the window.
    removed_titles = []
    if dropped and not args.dry_run:
        old_titles = {norm_title(ch.get("title", ""))
                      for ch in card.get("content", {}).get("chapters", [])}
        for ep in dropped:
            if norm_title(ep.title) in old_titles:
                removed_titles.append(ep.title)

    if args.dry_run:
        print(f"\nDRY RUN: {n_reused} kept, {n_adopted} adopted, "
              f"{n_uploaded} would upload, {len(removed_titles)} would drop. "
              "No changes written.")
        return

    print(f"\nWriting final playlist ({len(new_chapters)} chapters) ...")
    flush_playlist(new_chapters)
    save_state(state)

    print(f"\nDone: {n_reused} kept, {n_adopted} adopted, {n_uploaded} uploaded"
          + (f", {len(removed_titles)} dropped (outside --max-episodes window)"
             if removed_titles else ""))
    print(f"Playlist '{card['title']}' ({card_id}) now mirrors the feed.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def resolve_client_id(args) -> str:
    if args.client_id:
        return args.client_id
    env = os.environ.get("YOTO_CLIENT_ID")
    if env:
        return env
    try:
        saved = json.loads(TOKENS_FILE.read_text()).get("client_id")
    except (OSError, json.JSONDecodeError):
        saved = None
    if saved:
        return saved
    die("No client ID. Pass --client-id, set YOTO_CLIENT_ID, or run `login` first.")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Sync an Apple Podcast feed to a Yoto playlist.")
    p.add_argument("--client-id", default=None,
                   help="Yoto OAuth client ID (or set YOTO_CLIENT_ID)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("login", help="One-time browser login to Yoto")
    pl.set_defaults(func=lambda a: YotoClient(resolve_client_id(a)).login())

    ps = sub.add_parser("sync", help="Sync a podcast feed to a Yoto playlist")
    ps.add_argument("url", help="Apple Podcasts URL, e.g. "
                    "https://podcasts.apple.com/.../podcast/.../id1525687063")
    ps.add_argument("--card-id", default=None,
                    help="Sync into this existing Yoto playlist card id "
                         "(default: find by title, else create)")
    ps.add_argument("--title", default=None,
                    help="Playlist title (default: podcast name from iTunes)")
    ps.add_argument("--max-episodes", type=int, default=DEFAULT_MAX_EPISODES,
                    help="Keep at most N newest episodes "
                         f"(default {DEFAULT_MAX_EPISODES}; Yoto cards cap ~100 tracks)")
    ps.add_argument("--episode-list", default=None, metavar="FILE",
                    help="Sync exactly the episodes listed in FILE (one EP number "
                         "or title per line, in playlist order) instead of the "
                         "newest-N window. Useful for a 'most popular' playlist.")
    ps.add_argument("--newest-first", action="store_true",
                    help="Order newest episode first (default: oldest first)")
    ps.add_argument("--artwork", action="store_true",
                    help="Use the podcast cover art as the chapter icon")
    ps.add_argument("--dry-run", action="store_true",
                    help="Show what would change without uploading or writing")
    ps.set_defaults(func=cmd_sync)

    args = p.parse_args()
    if args.cmd == "sync" and args.max_episodes < 1:
        die("--max-episodes must be >= 1")
    args.func(args)


if __name__ == "__main__":
    main()
