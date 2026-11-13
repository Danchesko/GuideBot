# Data collection

Run commands from the project root after building the Docker images. Supported
cities: `bishkek`, `almaty`. Collection needs 2GIS access and `REVIEWS_API_KEY`;
embedding needs a cached `cointegrated/rubert-tiny2` model or download access.
Back up the data first.

```bash
./manage.sh backup
./manage.sh update almaty
./manage.sh update bishkek --step reviews
./manage.sh update bishkek --from trust
./manage.sh update almaty --test
```

| Step | Result |
|---|---|
| `restaurants` | Collect `еда` and `кофейня` into SQLite using Chromium/Xvfb |
| `reviews` | Fetch new reviews through the API |
| `trust` | Recompute review trust and restaurant statistics |
| `embeddings` | Add missing Chroma vectors in bounded batches; rebuild FTS5 |

After a reviews-only update, run `--from trust`. Existing vectors are not
recomputed. An explicit indexer `--rebuild` deletes the collection; it is not part
of normal updates. After a failure, inspect `logs/` and resume with `--from STEP`.

`manage.sh` handles the Compose bot lifecycle and serializes management commands.
`scripts/collect-city.sh` runs inside the collector, holds `data/.update.lock`, and does not
manage systemd or Docker. Do not delete lock files during a run. Direct module
commands do not acquire the update lock: stop the bot and other writers yourself.

`--test` uses separate `{city}_test.db` and `chroma_{city}_test` paths, two pages per
term, and ten places for reviews. Test files are deleted only after success.
It makes real network requests but does not modify the main city databases.

## Another category

Use a separate database, never the production restaurant database:

```bash
./manage.sh stop
# Do not run alongside another collector, backup, or restore.
docker compose run --rm --no-deps --entrypoint xvfb-run collector -a \
  python -m bishkek_food_finder.scraper.restaurants \
  --city bishkek --search-term "аптека" --pages 2 --db data/pharmacies.db
# Start again only if it was running before this operation.
./manage.sh up
```

This collects another 2GIS category with the same browser image. The table is still
named `restaurants`; restaurant-specific fields can be empty. Other websites need
a parser for that website. Serving another domain through the bot additionally
requires appropriate schema, search tools, prompts, and tests.

## Verification and limitations

Check row counts, trust scores, vector counts, and an actual bot search. Counts
alone do not prove complete coverage or recommendation quality. The current 2GIS
collector intercepts pages starting at page 2; page 1 is a known coverage gap.
A source layout/API change can require scraper changes regardless of Docker.
Restaurants without reviews may lack `restaurant_stats`: use a `LEFT JOIN` when
listing all collected places.
