# watchlist-sync

Keeps one watchlist in sync across Trakt and Simkl. It is a single-user web app that runs as one container and stores its state in SQLite.

## How syncing works

- **The watchlist on each service.** On Trakt it is the watchlist. On Simkl it is the **Plan to Watch** status. Simkl has no separate watchlist: every library item sits in exactly one status.
- **Changes come from snapshots.** After each successful sync the app stores what each service held. The next sync compares each service with its own snapshot. That is how "removed on Simkl" is told apart from "never added to Simkl".
- **Merged changes go everywhere.** An add on one service is added to the others. A removal on one service is removed from the others.
- **Conflicts wait for you.** When a title was added on one service and removed on another since the last sync, nothing happens to it until you pick an outcome on the Conflicts page.
- **Leaving Plan to Watch counts as a removal.** If Plex marks a movie watched on Simkl, it moves to Completed, so the app removes it from your Trakt watchlist.
- **Other Simkl statuses are never touched.** If a title is Completed, Watching, On Hold or Dropped on Simkl, the app does not move it back to Plan to Watch.
- **Watch history is protected.** Simkl can only remove an item by deleting its history and rating too. The app does not remove an item that has any history; the plan lists it under "Left alone" instead.
- **Matching never guesses.** Titles match by IMDb, TMDB, TVDB, MAL, Trakt or Simkl ID first. If no ID matches, a title matches only when exactly one candidate has the same type, normalized title and year. Anything else goes to the Unmatched page. Matches you make there are stored and reused.
- **Writes carry only real data.** Adds send IDs and nothing else, so no dates or defaults are ever written.
- **Safe to retry.** Every write is recorded in an outbox before it is sent. After a crash, the next sync checks each unfinished write against the service: writes that already landed are recorded, and the rest are sent.
- **No echo loops.** A successful write updates the snapshot in the same transaction, so the next sync does not read it back as your change.
- **Cheap polling.** Each sync first reads Trakt's `/sync/last_activities` and Simkl's `/sync/activities`. A service's full list is downloaded only when its timestamps moved. Requests are paced to 1 POST per second on both services. A `429` response is retried after `Retry-After`, or with exponential backoff when that header is missing.
- **First sync is a dry run.** After you connect a service, syncs are dry runs until you approve one. Approving applies exactly the changes that dry run showed.

## Register the API apps

Both services redirect your browser back to the app after login. The redirect URL must match character for character, with no trailing slash.

### Trakt

1. Open <https://app.trakt.tv/settings/apps/api/new>. Trakt requires a verified GitHub account for this.
2. Set **Redirect URI** to `https://watchlists.erix-homelab.site/oauth/trakt/callback`.
3. Copy the **Client ID**. The app signs in with PKCE, so the Client Secret is optional. If you set one, it is sent only from the server.

Trakt deletes API apps that are unused for 30 consecutive days. Regular syncing counts as use.

### Simkl

1. Open <https://simkl.com/settings/developer/new/>.
2. Use AUTH V2. Set the redirect URI to `https://watchlists.erix-homelab.site/oauth/simkl/callback`.
3. Copy the **Client ID**. The **Server apps & services** client type also gives a Client Secret, which is optional here.

The app requests the `media:read media:write` scope. If Simkl grants a token without write access, the login fails with an error instead of producing a read-only connection.

## Deploy on k3s with Flux

The layout matches `erix/splitbot`: a `k8s/base` plus a `k8s/overlays/local` overlay with a sealed secret.

1. Push this repository to `github.com/erix/watchlist-sync`. The workflow in `.github/workflows/docker.yml` runs the tests, then pushes `ghcr.io/erix/watchlist-sync` tagged `latest` and `main-<run>-sha-<sha>`. The deployment has no image pull secret, so make the GHCR package public, or add a pull secret.
2. Create the secret and seal it:

   ```sh
   cp k8s/overlays/local/secret.example.yaml /tmp/watchlist-secret.yaml
   # fill in TRAKT_CLIENT_ID and SIMKL_CLIENT_ID (secrets optional)
   kubeseal --format yaml < /tmp/watchlist-secret.yaml > k8s/overlays/local/watchlist-sync-sealed.yaml
   rm /tmp/watchlist-secret.yaml
   ```

3. In `k8s/overlays/local/kustomization.yaml`, uncomment `- watchlist-sync-sealed.yaml`, then commit and push.
4. Copy `k8s/flux/watchlist-sync.yaml` to `erix/homelab` at `clusters/new/apps/watchlist-sync.yaml` and push. It adds the GitRepository, the Kustomization, and image automation, the same as splitbot.
5. Open <https://watchlists.erix-homelab.site>. The ingress uses Traefik and a cert-manager `letsencrypt` certificate. The hostname resolves only on your LAN, which is fine for OAuth because your own browser follows the redirect.

The Deployment runs one replica with the `Recreate` strategy. SQLite has a single writer, and the app owns token refresh: Trakt refresh tokens work once, so two pods would log each other out.

All state lives in `/data/watchlist.db` on the `watchlist-sync-data` PVC. That includes tokens, titles, IDs, snapshots, and the change log.

## First sync

1. Connect Trakt and Simkl on the Status page.
2. Click **Dry run** or wait for the next scheduled sync. Nothing is written yet.
3. Open the dry run's plan. Check the changes, the conflicts, and the "Left alone" list.
4. Click **Approve and apply**. Later syncs apply changes automatically.

To keep every sync a dry run, set `WS_DRY_RUN=true`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `WS_BASE_URL` | `http://localhost:8080` | Public URL of the app; the OAuth redirect URLs are derived from it. |
| `WS_DATA_DIR` | `/data` | Directory for `watchlist.db`. Mount a volume here. |
| `WS_SYNC_INTERVAL_MINUTES` | `15` | Time between automatic syncs. |
| `WS_DRY_RUN` | `false` | Make every sync a dry run. |
| `TRAKT_CLIENT_ID`, `TRAKT_CLIENT_SECRET` | none | Trakt API app credentials. The secret is optional. |
| `SIMKL_CLIENT_ID`, `SIMKL_CLIENT_SECRET` | none | Simkl API app credentials. The secret is optional. |

## Rate limits and quotas

- **Trakt:** 1 POST per second and 500 GETs per 5 minutes per user.
- **Simkl:** 1 POST per second and 10 GETs per second. With AUTH V2 there is also a daily quota per user, shared by every app you have connected to Simkl: 500 requests on Free, 1,000 on PRO, 10,000 on VIP.
- **Cost of a quiet sync:** one activity call per service, about 96 a day per service at the default interval.

## Run locally

```sh
docker compose up --build        # http://localhost:8080
uv sync && uv run pytest         # tests
uv run python -m watchlist_sync  # without Docker; set WS_DATA_DIR=./data
```

## Adding a service

Write an adapter that implements `Adapter` and `OAuthClient` in `watchlist_sync/adapters/base.py`, then register it in `watchlist_sync/runtime.py`. The planner and engine work on title IDs and need no changes.

## Code map

- `watchlist_sync/planner.py` is the pure planning logic: diffs, conflicts, and what to add or remove where.
- `watchlist_sync/engine.py` runs a sync: it reads the services, resumes unfinished writes, links titles across services, applies the plan, and commits snapshots.
- `watchlist_sync/db.py` holds the SQLite schema and the store, including title merging.
- `watchlist_sync/adapters/` holds the Trakt and Simkl adapters and the paced HTTP client.
- `tests/test_engine.py` runs the sync scenarios against fake services: one-sided add and remove, conflict, echo, a crash halfway through, and the Chef's Table: Pizza ID mismatch.
