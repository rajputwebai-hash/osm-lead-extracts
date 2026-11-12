# osm-lead-extracts

Small, per-city JSON extracts of OpenStreetMap business data, built daily from the free
[Geofabrik](https://download.geofabrik.de/) state extract with [osmium-tool](https://osmcode.org/osmium-tool/).
The format is Overpass-compatible (`{ "elements": [ ... ] }`, as returned by `out tags center`).

## Data files (pilot: Austin, Texas)

| File | Contents |
|---|---|
| `data/austin-tx-restaurant.json` | named `amenity=restaurant` / `amenity=fast_food` inside the Austin city boundary |
| `data/austin-tx-real_estate.json` | named `office=estate_agent`, `shop=estate_agent`, `office=property_management`, `office=real_estate`, `office=real_estate_agent` |
| `data/austin-tx-stats.json` | element counts, byte sizes, stage timings, source timestamp of the latest build |

Each element keeps its OSM `type` (`node`, `way`, `relation`), `id`, all `tags`, and `lat`/`lon` (nodes) or
`center` (ways and relations, centre of the bounding box like Overpass). The OSM URL is
`https://www.openstreetmap.org/<type>/<id>`.

## Attribution and license

The data files contain data (c) [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, made available
under the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
If you use or redistribute them, credit "(c) OpenStreetMap contributors" and keep the data under ODbL
(share-alike applies to derived databases). See `DATA-LICENSE.md`. The build scripts are MIT licensed (`LICENSE`).
The underlying extract is produced by Geofabrik GmbH from OpenStreetMap data.

## How it works

`scripts/build_extract.py` downloads the Texas PBF (MD5-verified), builds the city polygon from the OSM boundary
relation, clips with `osmium extract`, filters tags with `osmium tags-filter`, exports with `osmium export`,
and writes Overpass-style JSON. The GitHub Actions workflow runs on manual dispatch, daily, and when the build
scripts change, and commits only when the output changed.

Limitations: data is up to about a day old; listings are only as complete as OSM; a name is required.
Scheduled workflows in public repositories are disabled by GitHub after 60 days without repository activity.
