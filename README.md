# yoto-podcast-sync

Sync an Apple Podcast feed to a Yoto "Make Your Own" playlist. Give it an
Apple Podcasts URL; it finds the RSS feed, enumerates every episode MP3, and
uploads only what's missing to a Yoto playlist. Re-running it picks up new
episodes automatically.

Uses Yoto's official API (`api.yotoplay.com`, OAuth2 + PKCE) — the same one
Yoto's own app uses. Endpoint details were cross-checked against the
community `yoto-cli` project.

## Requirements

- Python 3.9+
- `pip install requests feedparser`

## One-time setup

**1. Get a Yoto client ID** (free, about a minute):

1. Go to https://dashboard.yoto.dev and sign in with your Yoto account.
2. Create a new client. Choose the **public** type (may be labelled "native",
   "SPA", or "no client secret").
3. Register this redirect URI **exactly**:
   `http://127.0.0.1:8787/callback`
4. Copy the client ID.

**2. Log in** (opens your browser once; tokens are stored locally):

```bash
export YOTO_CLIENT_ID=<your-client-id>   # or pass --client-id
python yoto_podcast_sync.py login
```

## Usage

```bash
# Preview what would happen (no uploads, no changes):
python yoto_podcast_sync.py sync "https://podcasts.apple.com/tw/podcast/.../id1525687063" --dry-run

# Real sync — creates the playlist if needed:
python yoto_podcast_sync.py sync "https://podcasts.apple.com/tw/podcast/.../id1525687063"

# Sync into an existing playlist card instead:
python yoto_podcast_sync.py sync "<url>" --card-id 5ukMR

# Use the podcast cover art as the chapter icon:
python yoto_podcast_sync.py sync "<url>" --artwork
```

Each episode becomes one chapter with one track (chapter-per-episode matches
how the Yoto player advances with button presses).

## Options

| Flag | Default | What it does |
|---|---|---|
| `--card-id` | find by title, else create | Sync into this existing playlist |
| `--title` | podcast name from iTunes | Playlist title when creating |
| `--max-episodes N` | 100 | Keep the N newest episodes (Yoto cards cap at ~100 tracks / 500 MB) |
| `--newest-first` | oldest first | Order newest episode first |
| `--artwork` | off | Upload the podcast cover as the chapter icon |
| `--episode-list FILE` | off | Sync exactly the episodes in FILE (one EP number or title per line, in playlist order) instead of the newest-N window — for a "most popular" playlist |
| `--dry-run` | off | Show what would change, touch nothing |

## "Most popular" playlist

To sync a hand-picked set (e.g. ranked by YouTube views) instead of the
newest episodes, put one EP number or title per line in a file, most popular
first:

```
EP136
EP137
EP105
...
```

then:

```bash
python yoto_podcast_sync.py sync "<url>" --episode-list popular.txt --title "水獺媽媽精選"
```

## Notes

- **Idempotent.** Episodes are matched by RSS GUID first, then by title, so
  re-runs only upload genuinely new episodes. Sync state lives in
  `~/.yoto-podcast-sync/state.json`; tokens in `~/.yoto-podcast-sync/tokens.json`.
- **First run takes a while.** A 374-episode feed syncs the newest 100;
  each MP3 is downloaded, uploaded to Yoto, and transcoded before it lands
  in the playlist (~15 MB per episode for the tested feed). The playlist is
  saved after every episode, so progress shows up live and Ctrl+C loses
  nothing — just re-run and it picks up where it left off.
- **Window slides.** When new episodes push old ones past `--max-episodes`,
  the oldest chapters are dropped so the playlist mirrors the feed.
- **Link a card.** After syncing, open the Yoto app, find the playlist, and
  "Link to a card" — then it plays on the Yoto player.
