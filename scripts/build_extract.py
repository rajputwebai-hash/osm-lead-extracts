#!/usr/bin/env python3
"""Build per-city OpenStreetMap lead-discovery extracts in Overpass-compatible JSON.

Usage: build_extract.py <city-slug>

Pipeline: Geofabrik state PBF -> city boundary polygon (OSM relation) -> osmium extract
-> osmium tags-filter (per industry) -> osmium export -> Overpass-style {"elements": [...]}
Output elements mirror Overpass `out tags center`:
  node:            {type, id, lat, lon, tags}
  way / relation:  {type, id, center: {lat, lon}, tags}

osmium export labels polygons built from ways/relations as areas with an "a" prefix:
area id = 2*way_id (ways) or 2*relation_id+1 (relations). These are decoded back to the OSM id.
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
DIAG = "diagnostics/last-failure.txt"

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

STAGE = {"name": "start"}


def write_failure(text):
    os.makedirs("diagnostics", exist_ok=True)
    with open(DIAG, "w") as f:
        f.write(text)


def run(cmd, timings, label):
    t = time.time()
    STAGE["name"] = label
    print("+", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    timings[label] = round(time.time() - t, 1)
    if r.stderr:
        print(r.stderr[-3000:], flush=True)
    if r.returncode != 0:
        write_failure("stage: %s\ncommand: %s\nexit code: %s\nelapsed_s: %s\nstdout (tail):\n%s\nstderr (tail):\n%s\n" % (
            label, " ".join(cmd), r.returncode, timings[label], r.stdout[-2000:], r.stderr[-4000:]))
        raise SystemExit("Command failed at stage %s (exit %s)" % (label, r.returncode))
    return r.stdout


def download(url, dest, timings):
    run(["curl", "-fsSL", "--retry", "3", "--retry-delay", "10", "-A", UA, "-o", dest, url],
        timings, "download_pbf_s")


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


def decode_id(fid):
    """Map an osmium export feature id (n123, w123, r123, a123) to (osm type, osm id)."""
    fid = str(fid or "")
    if len(fid) < 2 or fid[0] not in "nwra" or not fid[1:].isdigit():
        return None
    c, n = fid[0], int(fid[1:])
    if c == "a":
        return ("way", n // 2) if n % 2 == 0 else ("relation", (n - 1) // 2)
    return {"n": "node", "w": "way", "r": "relation"}[c], n


def convert(geojsonseq, rules):
    seen, out = set(), []
    with open(geojsonseq, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feat = json.loads(line)
            key = decode_id(feat.get("id"))
            if key is None:
                continue
            typ, oid = key
            tags = feat.get("properties") or {}
            tags = {k: v for k, v in tags.items() if not k.startswith("@")}
            if not matches(tags, rules) or not feat.get("geometry"):
                continue
            if key in seen:
                continue
            seen.add(key)
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


def pick_boundary(braw, rel_num):
    allf = json.load(open(braw))["features"]
    polys = [f for f in allf if f.get("geometry") and f["geometry"]["type"] in ("Polygon", "MultiPolygon")]
    feats = [f for f in polys if decode_id(f.get("id")) == ("relation", rel_num)]
    if not feats:
        summary = [(f.get("id"), (f.get("geometry") or {}).get("type"), dict(list((f.get("properties") or {}).items())[:4])) for f in allf[:15]]
        write_failure("stage: boundary_polygon\nfeatures: %d, polygons: %d\nfirst features: %s\n" % (len(allf), len(polys), json.dumps(summary)[:3000]))
        raise SystemExit("No polygon geometry built for relation %s" % rel_num)
    return feats[0], len(allf), len(polys)


def main():
    slug = sys.argv[1]
    city = json.load(open("cities.json"))[slug]
    if os.path.exists(DIAG):
        os.remove(DIAG)
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)
    timings, t_all = {}, time.time()

    pbf = os.path.join(WORK, "state.osm.pbf")
    download(city["geofabrik_pbf"], pbf, timings)
    STAGE["name"] = "md5"
    md5_txt = urllib.request.urlopen(urllib.request.Request(city["geofabrik_md5"], headers={"User-Agent": UA})).read().decode().split()[0]
    actual = md5_of(pbf)
    if actual != md5_txt:
        raise SystemExit("MD5 mismatch: expected %s got %s" % (md5_txt, actual))
    pbf_size = os.path.getsize(pbf)
    ts = run(["osmium", "fileinfo", "-g", "header.option.osmosis_replication_timestamp", pbf], timings, "fileinfo_s").strip()

    rel = "r%d" % city["osm_relation_id"]
    bsrc = os.path.join(WORK, "boundary_src.osm.pbf")
    run(["osmium", "getid", "-r", "-O", "-o", bsrc, pbf, rel], timings, "boundary_getid_s")
    braw = os.path.join(WORK, "boundary_all.geojson")
    run(["osmium", "export", "-O", "-f", "geojson", "--geometry-types=polygon", "--add-unique-id=type_id", "-o", braw, bsrc],
        timings, "boundary_export_s")
    STAGE["name"] = "boundary_polygon"
    feat, n_all, n_poly = pick_boundary(braw, city["osm_relation_id"])
    bpoly = os.path.join(WORK, "boundary.geojson")
    json.dump({"type": "Feature", "properties": {}, "geometry": feat["geometry"]}, open(bpoly, "w"))

    city_pbf = os.path.join(WORK, "city.osm.pbf")
    run(["osmium", "extract", "-p", bpoly, "-s", "smart", "-O", "-o", city_pbf, pbf], timings, "city_extract_s")

    stats = {
        "city": slug,
        "osm_relation_id": city["osm_relation_id"],
        "source_pbf": city["geofabrik_pbf"],
        "source_pbf_bytes": pbf_size,
        "source_pbf_md5_verified": True,
        "source_osm_data_timestamp": ts,
        "boundary_geometry": feat["geometry"]["type"],
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
        STAGE["name"] = "%s_convert" % ind
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
    write_failure("none - last build succeeded (run %s)\n" % os.environ.get("GITHUB_RUN_ID"))
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if isinstance(e.code, str) and not os.path.exists(DIAG):
            write_failure("stage: %s\nmessage: %s\n" % (STAGE["name"], e.code))
        raise
    except Exception:
        import traceback
        write_failure("stage: %s\npython exception:\n%s" % (STAGE["name"], traceback.format_exc()[-4000:]))
        raise
