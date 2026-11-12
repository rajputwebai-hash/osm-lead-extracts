#!/usr/bin/env python3
"""Build per-city OpenStreetMap lead-discovery extracts in Overpass-compatible JSON.

Usage: build_extract.py <city-slug>

Pipeline: Geofabrik state PBF -> city boundary polygon (OSM relation) -> osmium extract
-> osmium tags-filter (per industry) -> osmium export -> Overpass-style {"elements": [...]}
Output elements mirror Overpass `out tags center`:
  node:            {type, id, lat, lon, tags}
  way / relation:  {type, id, center: {lat, lon}, tags}
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

UA = "osm-lead-extracts/1.0 (https://github.com/{repo})".format(
    repo=os.environ.get("GITHUB_REPOSITORY", "osm-lead-extracts"))
WORK = os.environ.get("WORK_DIR", "work")
DATA = "data"

INDUSTRIES = {
    "restaurant": {
        "filters": ["nwr/amenity=restaurant,fast_food"],
        "match": [("amenity", {"restaurant", "fast_food"})],
    },
    "real_estate": {
        "filters": [
            "nwr/office=estate_agent,property_management,real_estate,real_estate_agent",
            "nwr/shop=estate_agent",
        ],
        "match": [
            ("office", {"estate_agent", "property_management", "real_estate", "real_estate_agent"}),
            ("shop", {"estate_agent"}),
        ],
    },
}

COPYRIGHT = ("Contains data (c) OpenStreetMap contributors, available under the Open "
             "Database License (ODbL) 1.0: https://www.openstreetmap.org/copyright")


def run(cmd, timings, label):
    t = time.time()
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)
    timings[label] = round(time.time() - t, 1)


def download(url, dest, timings):
    t = time.time()
    run(["curl", "-fsSL", "--retry", "3", "--retry-delay", "10", "-A", UA, "-D", dest + ".headers", "-o", dest, url],
        timings, "download_pbf_s")
    return {k.lower(): v.strip() for k, v in (l.split(":", 1) for l in open(dest + ".headers", errors="replace") if ":" in l)}


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def coords_bbox(geom):
    xs, ys = [], []

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1])
        else:
            for x in c:
                walk(x)
    walk(geom["coordinates"])
    return min(xs), min(ys), max(xs), max(ys)


def matches(tags, rules):
    if not (tags.get("name") or "").strip():
        return False
    return any(tags.get(k) in vals for k, vals in rules)


def convert(geojsonseq, rules):
    seen, out = set(), []
    with open(geojsonseq, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feat = json.loads(line)
            fid = str(feat.get("id", ""))
            if len(fid) < 2 or fid[0] not in "nwr" or not fid[1:].isdigit():
                continue
            tags = feat.get("properties") or {}
            tags = {k: v for k, v in tags.items() if not k.startswith("@")}
            if not matches(tags, rules) or not feat.get("geometry"):
                continue
            typ = {"n": "node", "w": "way", "r": "relation"}[fid[0]]
            oid = int(fid[1:])
            if (typ, oid) in seen:
                continue
            seen.add((typ, oid))
            g = feat["geometry"]
            if typ == "node" and g["type"] == "Point":
                el = {"type": typ, "id": oid, "lat": g["coordinates"][1], "lon": g["coordinates"][0], "tags": tags}
            else:
                x0, y0, x1, y1 = coords_bbox(g)
                el = {"type": typ, "id": oid, "center": {"lat": round((y0 + y1) / 2, 7), "lon": round((x0 + x1) / 2, 7)}, "tags": tags}
            out.append(el)
    order = {"node": 0, "way": 1, "relation": 2}
    out.sort(key=lambda e: (order[e["type"]], e["id"]))
    return out


def main():
    slug = sys.argv[1]
    city = json.load(open("cities.json"))[slug]
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)
    timings, t_all = {}, time.time()

    pbf = os.path.join(WORK, "state.osm.pbf")
    headers = download(city["geofabrik_pbf"], pbf, timings)
    md5_txt = urllib.request.urlopen(urllib.request.Request(city["geofabrik_md5"], headers={"User-Agent": UA})).read().decode().split()[0]
    actual = md5_of(pbf)
    if actual != md5_txt:
        raise SystemExit("MD5 mismatch: expected %s got %s" % (md5_txt, actual))
    pbf_size = os.path.getsize(pbf)
    ts = subprocess.run(["osmium", "fileinfo", "-g", "header.option.osmosis_replication_timestamp", pbf],
                        capture_output=True, text=True, check=True).stdout.strip()

    rel = "r%d" % city["osm_relation_id"]
    bsrc = os.path.join(WORK, "boundary_src.osm.pbf")
    run(["osmium", "getid", "-r", "-O", "-o", bsrc, pbf, rel], timings, "boundary_getid_s")
    braw = os.path.join(WORK, "boundary_all.geojson")
    run(["osmium", "export", "-O", "-f", "geojson", "--geometry-types=polygon", "--add-unique-id=type_id", "-o", braw, bsrc],
        timings, "boundary_export_s")
    feats = [f for f in json.load(open(braw))["features"] if str(f.get("id")) == rel and f.get("geometry")]
    if not feats:
        raise SystemExit("No polygon geometry built for %s" % rel)
    bpoly = os.path.join(WORK, "boundary.geojson")
    json.dump({"type": "Feature", "properties": {}, "geometry": feats[0]["geometry"]}, open(bpoly, "w"))

    city_pbf = os.path.join(WORK, "city.osm.pbf")
    run(["osmium", "extract", "-p", bpoly, "-s", "smart", "-O", "-o", city_pbf, pbf], timings, "city_extract_s")

    stats = {
        "city": slug,
        "osm_relation_id": city["osm_relation_id"],
        "source_pbf": city["geofabrik_pbf"],
        "source_pbf_bytes": pbf_size,
        "source_pbf_md5_verified": True,
        "source_osm_data_timestamp": ts,
        "city_pbf_bytes": os.path.getsize(city_pbf),
        "industries": {},
    }

    for ind, spec in INDUSTRIES.items():
        parts = []
        for i, flt in enumerate(spec["filters"]):
            p = os.path.join(WORK, "%s_%d.osm.pbf" % (ind, i))
            run(["osmium", "tags-filter", "-O", "-o", p, city_pbf, flt], timings, "%s_filter_%d_s" % (ind, i))
            parts.append(p)
        merged = os.path.join(WORK, "%s.osm.pbf" % ind)
        if len(parts) > 1:
            run(["osmium", "merge", "-O", "-o", merged] + parts, timings, "%s_merge_s" % ind)
        else:
            merged = parts[0]
        seq = os.path.join(WORK, "%s.geojsonseq" % ind)
        run(["osmium", "export", "-O", "-f", "geojsonseq", "-x", "print_record_separator=false", "--add-unique-id=type_id", "-o", seq, merged],
            timings, "%s_export_s" % ind)
        elements = convert(seq, spec["match"])
        doc = {
            "version": 0.6,
            "generator": "osm-lead-extracts (Geofabrik + osmium)",
            "osm3s": {"timestamp_osm_base": ts, "copyright": COPYRIGHT},
            "extract": {"city": slug, "name": city["name"], "industry": ind,
                        "osm_relation_id": city["osm_relation_id"], "overpass_area_id": city["overpass_area_id"],
                        "element_count": len(elements), "source": city["geofabrik_pbf"]},
            "elements": elements,
        }
        path = os.path.join(DATA, "%s-%s.json" % (slug, ind))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        counts = {}
        for e in elements:
            counts[e["type"]] = counts.get(e["type"], 0) + 1
        stats["industries"][ind] = {"file": path, "elements": len(elements), "by_type": counts,
                                    "bytes": os.path.getsize(path)}

    timings["total_s"] = round(time.time() - t_all, 1)
    stats["timings_seconds"] = timings
    stats["github_run_id"] = os.environ.get("GITHUB_RUN_ID")
    with open(os.path.join(DATA, "%s-stats.json" % slug), "w") as f:
        json.dump(stats, f, indent=2)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
