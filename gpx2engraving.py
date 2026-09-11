#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gpx2engraving — transforme une trace GPX en SVG propre, prêt à graver (LightBurn).

Contenu du SVG (unités : millimètres) :
  - titre (texte vectorisé en chemins, aucune police requise côté laser)
  - trace GPX sur fond de courbes de niveau (MNT IGN RGE ALTI 5 m)
  - plans d'eau (IGN BD TOPO, ou OpenStreetMap en secours)
  - profil altimétrique complet
  - ligne de statistiques : distance, D+, date (durée en option)
  - contour de la planche (calque de découpe)

Exécuter avec le Python de QGIS (fournit GDAL, numpy, shapely, pyproj,
scipy, matplotlib, fontTools) :
  "C:\\Program Files\\QGIS 3.34.9\\bin\\python-qgis-ltr.bat" gpx2engraving.py trace.gpx --title "Mon titre"
ou via le lanceur gpx2engraving.bat fourni à côté.
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict

import numpy as np
import requests
from pyproj import Transformer
from scipy.ndimage import distance_transform_edt, gaussian_filter
from shapely.geometry import (GeometryCollection, LineString, MultiLineString,
                              MultiPolygon, Polygon, box, shape)
from shapely.ops import linemerge, polygonize, unary_union

try:
    import contourpy
except ImportError:  # matplotlib >= 3.6 dépend de contourpy, donc normalement présent
    contourpy = None

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

USER_AGENT = "gpx2engraving/1.0 (generateur de cartes a graver)"
IGN_WMS = "https://data.geopf.fr/wms-r/wms"
IGN_WFS = "https://data.geopf.fr/wfs/ows"
OVERPASS_URLS = ["https://overpass-api.de/api/interpreter",
                 "https://overpass.kumi.systems/api/interpreter"]

# Couleurs = palette LightBurn (C00..C15). LightBurn attribue un calque par couleur.
LAYERS = OrderedDict([
    # nom          (couleur,   rempli, description)
    ("frame",      ("#FF0000", False, "C02 rouge  — contour de la planche (découpe)")),
    ("contours",   ("#0000FF", False, "C01 bleu   — courbes de niveau (ligne)")),
    ("contours_index", ("#0000A0", False, "C09 bleu foncé — courbes maîtresses (ligne, plus de puissance)")),
    ("water",      ("#00E0E0", False, "C06 cyan   — contour des plans d'eau (ligne)")),
    ("water_hatch", ("#00A0FF", False, "C14 bleu clair — hachures des plans d'eau (ligne)")),
    ("track",      ("#000000", True,  "C00 noir   — trace GPX (remplissage)")),
    ("track_line", ("#808080", False, "C16 gris   — axe de la trace (ligne, alternative au remplissage)")),
    ("profile",    ("#00E000", False, "C03 vert   — profil altimétrique : courbe, base, graduations (ligne)")),
    ("profile_fill", ("#00A000", True, "C11 vert foncé — aire sous le profil (remplissage, optionnel)")),
    ("text",       ("#FF00FF", True,  "C07 magenta — titre, statistiques, étiquettes (remplissage)")),
])

FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------
# Lecture GPX
# ----------------------------------------------------------------------------
def parse_time(s):
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t


def read_gpx(path):
    """Retourne (nom, segments, date_metadata). Un segment = liste de (lat, lon, ele, time)."""
    root = ET.parse(path).getroot()
    ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
    name = None
    segs = []

    def read_points(container, tag):
        pts = []
        for p in container.iter(ns + tag):
            lat, lon = float(p.get("lat")), float(p.get("lon"))
            e = p.find(ns + "ele")
            t = p.find(ns + "time")
            ele = float(e.text) if (e is not None and e.text) else float("nan")
            pts.append((lat, lon, ele, parse_time(t.text if t is not None else None)))
        return pts

    for trk in root.iter(ns + "trk"):
        n = trk.find(ns + "name")
        if n is not None and n.text and not name:
            name = n.text.strip()
        for seg in trk.iter(ns + "trkseg"):
            pts = read_points(seg, "trkpt")
            if len(pts) >= 2:
                segs.append(pts)
    if not segs:
        for rte in root.iter(ns + "rte"):
            n = rte.find(ns + "name")
            if n is not None and n.text and not name:
                name = n.text.strip()
            pts = read_points(rte, "rtept")
            if len(pts) >= 2:
                segs.append(pts)
    if not segs:
        raise SystemExit("Aucune trace (<trk>) ni itinéraire (<rte>) exploitable dans le GPX.")
    meta_time = None
    md = root.find(ns + "metadata")
    if md is not None:
        t = md.find(ns + "time")
        if t is not None:
            meta_time = parse_time(t.text)
    return name, segs, meta_time


# ----------------------------------------------------------------------------
# Trace : projection, distance, D+
# ----------------------------------------------------------------------------
class Track:
    def __init__(self, segs, epsg=2154):
        tr = Transformer.from_crs(4326, epsg, always_xy=True)
        self.segments = []      # liste d'arrays (n,2) en mètres
        xs, ys, eles, times = [], [], [], []
        for seg in segs:
            lat = np.array([p[0] for p in seg])
            lon = np.array([p[1] for p in seg])
            x, y = tr.transform(lon, lat)
            x, y = np.asarray(x), np.asarray(y)
            self.segments.append(np.column_stack([x, y]))
            xs.append(x)
            ys.append(y)
            eles.append(np.array([p[2] for p in seg], dtype=float))
            times.extend([p[3] for p in seg])
        self.x = np.concatenate(xs)
        self.y = np.concatenate(ys)
        self.ele = np.concatenate(eles)
        self.times = times
        d = np.hypot(np.diff(self.x), np.diff(self.y))
        self.dist = np.concatenate([[0.0], np.cumsum(d)])
        self.length = float(self.dist[-1])

    @property
    def bbox(self):
        return float(self.x.min()), float(self.y.min()), float(self.x.max()), float(self.y.max())

    def start_time(self):
        ts = [t for t in self.times if t is not None]
        return min(ts) if ts else None

    def duration(self):
        ts = [t for t in self.times if t is not None]
        if len(ts) < 2:
            return None
        return max(ts) - min(ts)

    def moving_duration(self, speed_min_kmh=0.5):
        """Durée en mouvement : on ignore les intervalles où la vitesse < seuil."""
        total = dt.timedelta(0)
        prev_t, prev_i = None, None
        for i, t in enumerate(self.times):
            if t is None:
                continue
            if prev_t is not None:
                dtm = (t - prev_t).total_seconds()
                dd = self.dist[i] - self.dist[prev_i]
                if dtm > 0 and (dd / dtm) * 3.6 >= speed_min_kmh and dtm < 600:
                    total += dt.timedelta(seconds=dtm)
            prev_t, prev_i = t, i
        return total


def resample(dist, values, step):
    """Rééchantillonne values(dist) à pas constant. Ignore les NaN."""
    ok = np.isfinite(values)
    if ok.sum() < 2:
        return None, None
    d = np.arange(0.0, dist[-1] + step, step)
    return d, np.interp(d, dist[ok], values[ok])


def smooth(values, window_pts):
    w = max(1, int(window_pts) | 1)
    if w <= 1:
        return values
    k = np.ones(w) / w
    pad = w // 2
    padded = np.pad(values, pad, mode="edge")
    return np.convolve(padded, k, mode="valid")


def elevation_gain(ele, threshold):
    """D+ / D- avec seuil d'hystérésis (ignore les oscillations < threshold)."""
    gain = loss = 0.0
    base = ele[0]
    for v in ele[1:]:
        if v - base >= threshold:
            gain += v - base
            base = v
        elif base - v >= threshold:
            loss += base - v
            base = v
    return gain, loss


# ----------------------------------------------------------------------------
# Mise en page (mm)
# ----------------------------------------------------------------------------
class Layout:
    M = 7.0          # marge extérieure
    TITLE_H = 13.0   # bandeau titre
    GAP = 3.5
    STATS_H = 6.0

    def __init__(self, track_bbox, args):
        self.profile_h = 0.0 if args.no_profile else args.profile_height
        x0, y0, x1, y1 = track_bbox
        margin = max(args.margin, 0.08 * max(x1 - x0, y1 - y0))
        fx0, fy0, fx1, fy1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
        fw, fh = fx1 - fx0, fy1 - fy0
        a = fw / fh
        S = args.max_size
        M = self.M
        overhead = 2 * M + self.TITLE_H + self.GAP + self.GAP + self.STATS_H
        if self.profile_h:
            overhead += self.GAP + self.profile_h
        self.overhead = overhead
        wmin = 0.55 * S

        def from_W(W):
            map_w = W - 2 * M
            return W, map_w / a + overhead

        def from_H(H):
            map_h = H - overhead
            return map_h * a + 2 * M, H

        fmt = args.format
        if fmt == "auto":
            W, H = from_W(S)
            if H > S:
                W, H = from_H(S)
            W = max(W, wmin)
            if 0.85 <= W / H <= 1.18:
                W = H = S
        elif fmt == "square":
            W = H = S
        elif fmt == "landscape":
            W, H = from_W(S)
            H = min(max(H, overhead + 40), 0.8 * S)
        elif fmt == "portrait":
            W, H = from_H(S)
            W = min(max(W, wmin), 0.8 * S)
        else:
            raise SystemExit("format inconnu : " + fmt)

        self.W, self.H = round(W, 1), round(H, 1)
        self.map_x = M
        self.map_y = M + self.TITLE_H + self.GAP
        self.map_w = self.W - 2 * M
        self.map_h = self.H - overhead
        # étendre l'emprise géographique à l'aspect de la fenêtre carte
        target_a = self.map_w / self.map_h
        cx, cy = (fx0 + fx1) / 2, (fy0 + fy1) / 2
        if fw / fh < target_a:
            fw = fh * target_a
        else:
            fh = fw / target_a
        self.frame = (cx - fw / 2, cy - fh / 2, cx + fw / 2, cy + fh / 2)
        self.scale = self.map_w / fw  # mm par mètre
        y = self.map_y + self.map_h + self.GAP
        if self.profile_h:
            self.profile_rect = (M, y, self.map_w, self.profile_h)
            y += self.profile_h + self.GAP
        else:
            self.profile_rect = None
        self.stats_baseline = y + self.STATS_H * 0.78
        self.title_baseline = M + self.TITLE_H * 0.74

    def to_mm(self, x, y):
        fx0, fy0, fx1, fy1 = self.frame
        return (self.map_x + (np.asarray(x) - fx0) * self.scale,
                self.map_y + (fy1 - np.asarray(y)) * self.scale)

    def mm_to_m(self, mm):
        return mm / self.scale


# ----------------------------------------------------------------------------
# Données externes : MNT et plans d'eau
# ----------------------------------------------------------------------------
def http_get(url, params, timeout=180):
    r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    return r


def fetch_dem(bbox, res, cache_dir, layer="ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES", tile=1800):
    """MNT IGN via WMS-Raster (format BIL 32 bits). Retourne (array HxW, bbox exact)."""
    x0, y0, x1, y1 = bbox
    W = int(math.ceil((x1 - x0) / res))
    H = int(math.ceil((y1 - y0) / res))
    x1, y1 = x0 + W * res, y0 + H * res
    key = hashlib.md5(f"{layer}|{x0:.1f}|{y0:.1f}|{W}|{H}|{res}".encode()).hexdigest()[:12]
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, f"dem_{key}.npy")
    if os.path.exists(cache):
        log(f"  MNT : cache ({W}x{H} px à {res} m)")
        return np.load(cache), (x0, y0, x1, y1)
    log(f"  MNT : téléchargement IGN {layer} ({W}x{H} px à {res} m)")
    dem = np.full((H, W), np.nan, dtype=np.float64)
    for r0 in range(0, H, tile):
        for c0 in range(0, W, tile):
            r1, c1 = min(H, r0 + tile), min(W, c0 + tile)
            bx0, bx1 = x0 + c0 * res, x0 + c1 * res
            by1, by0 = y1 - r0 * res, y1 - r1 * res
            params = dict(SERVICE="WMS", VERSION="1.3.0", REQUEST="GetMap", LAYERS=layer,
                          STYLES="", CRS="EPSG:2154", BBOX=f"{bx0},{by0},{bx1},{by1}",
                          WIDTH=c1 - c0, HEIGHT=r1 - r0, FORMAT="image/x-bil;bits=32")
            r = http_get(IGN_WMS, params)
            ct = r.headers.get("content-type", "")
            if r.status_code != 200 or not ct.startswith("image/x-bil"):
                raise RuntimeError(f"WMS IGN a échoué ({r.status_code}, {ct}) : {r.text[:300]}")
            a = np.frombuffer(r.content, dtype="<f4").reshape(r1 - r0, c1 - c0)
            dem[r0:r1, c0:c1] = a
    dem[dem < -1000] = np.nan
    if np.isnan(dem).any():
        if np.isnan(dem).all():
            raise RuntimeError("MNT vide sur cette emprise (hors couverture IGN ?)")
        idx = distance_transform_edt(np.isnan(dem), return_distances=False, return_indices=True)
        dem = dem[tuple(idx)]
    np.save(cache, dem)
    return dem, (x0, y0, x1, y1)


def sample_dem(dem, dem_bbox, res, x, y):
    """Échantillonnage bilinéaire du MNT aux points (x, y)."""
    x0, y0, x1, y1 = dem_bbox
    H, W = dem.shape
    cx = (np.asarray(x) - x0) / res - 0.5
    ry = (y1 - np.asarray(y)) / res - 0.5
    cx = np.clip(cx, 0, W - 1.001)
    ry = np.clip(ry, 0, H - 1.001)
    c0, r0 = np.floor(cx).astype(int), np.floor(ry).astype(int)
    fc, fr = cx - c0, ry - r0
    z = (dem[r0, c0] * (1 - fc) * (1 - fr) + dem[r0, c0 + 1] * fc * (1 - fr)
         + dem[r0 + 1, c0] * (1 - fc) * fr + dem[r0 + 1, c0 + 1] * fc * fr)
    return z


def fetch_water_ign(bbox):
    x0, y0, x1, y1 = bbox
    params = dict(SERVICE="WFS", VERSION="2.0.0", REQUEST="GetFeature",
                  TYPENAMES="BDTOPO_V3:plan_d_eau", SRSNAME="EPSG:2154",
                  BBOX=f"{x0},{y0},{x1},{y1},EPSG:2154", OUTPUTFORMAT="application/json", COUNT=1000)
    r = http_get(IGN_WFS, params)
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        raise RuntimeError(f"WFS IGN a échoué ({r.status_code})")
    polys, names = [], []
    for f in r.json().get("features", []):
        g = shape(f["geometry"])
        if g.is_empty:
            continue
        if g.has_z:
            g = _drop_z(g)
        polys.append(g.buffer(0))
        names.append(f["properties"].get("toponyme") or f["properties"].get("nature") or "?")
    return polys, names


def _drop_z(g):
    from shapely import wkb
    return wkb.loads(wkb.dumps(g, output_dimension=2))


def fetch_water_osm(bbox_wgs):
    south, west, north, east = bbox_wgs
    bb = f"({south},{west},{north},{east})"
    q = (f'[out:json][timeout:90];(way["natural"="water"]{bb};relation["natural"="water"]{bb};'
         f'way["landuse"="reservoir"]{bb};);out body geom;')
    last = None
    for url in OVERPASS_URLS:
        try:
            r = requests.post(url, data={"data": q}, headers={"User-Agent": USER_AGENT}, timeout=120)
            if r.status_code == 200:
                break
            last = f"{r.status_code}"
        except requests.RequestException as e:
            last = str(e)
    else:
        raise RuntimeError(f"Overpass a échoué : {last}")
    tr = Transformer.from_crs(4326, 2154, always_xy=True)

    def ring(coords):
        lon = [c["lon"] for c in coords]
        lat = [c["lat"] for c in coords]
        x, y = tr.transform(lon, lat)
        return list(zip(x, y))

    polys, names = [], []
    for e in r.json().get("elements", []):
        nm = e.get("tags", {}).get("name") or e.get("tags", {}).get("water") or "?"
        if e["type"] == "way" and len(e.get("geometry", [])) >= 4:
            rg = ring(e["geometry"])
            if rg[0] == rg[-1]:
                polys.append(Polygon(rg).buffer(0))
                names.append(nm)
        elif e["type"] == "relation":
            outers, inners = [], []
            for m in e.get("members", []):
                if m.get("type") != "way" or "geometry" not in m:
                    continue
                (inners if m.get("role") == "inner" else outers).append(LineString(ring(m["geometry"])))
            if outers:
                po = unary_union(list(polygonize(linemerge(outers) if len(outers) > 1 else outers[0])))
                if inners:
                    pi = unary_union(list(polygonize(linemerge(inners) if len(inners) > 1 else inners[0])))
                    po = po.difference(pi)
                if not po.is_empty:
                    polys.append(po)
                    names.append(nm)
    return polys, names


# ----------------------------------------------------------------------------
# Géométrie
# ----------------------------------------------------------------------------
def iter_lines(geom):
    if geom is None or geom.is_empty:
        return
    t = geom.geom_type
    if t == "LineString":
        yield geom
    elif t in ("MultiLineString", "GeometryCollection"):
        for g in geom.geoms:
            yield from iter_lines(g)


def iter_polys(geom):
    if geom is None or geom.is_empty:
        return
    t = geom.geom_type
    if t == "Polygon":
        yield geom
    elif t in ("MultiPolygon", "GeometryCollection"):
        for g in geom.geoms:
            yield from iter_polys(g)


def auto_interval(zrange, target_lines):
    candidates = [5, 10, 20, 25, 50, 100, 200, 500]
    return min(candidates, key=lambda c: abs(zrange / c - target_lines))


def make_contours(dem, dem_bbox, res, interval, sigma, frame_poly, erase, tol_m, min_len_m, index_every):
    if contourpy is None:
        raise SystemExit("Le module contourpy est absent (il est fourni avec matplotlib >= 3.6).")
    z = gaussian_filter(dem, sigma) if sigma > 0 else dem
    x0, y0, x1, y1 = dem_bbox
    H, W = z.shape
    xs = x0 + (np.arange(W) + 0.5) * res
    ys = y1 - (np.arange(H) + 0.5) * res
    cg = contourpy.contour_generator(x=xs, y=ys, z=z, line_type=contourpy.LineType.Separate)
    zmin, zmax = float(np.nanmin(z)), float(np.nanmax(z))
    levels = np.arange(math.ceil(zmin / interval) * interval, zmax, interval)
    normal, index = [], []
    n_pts = 0
    for lev in levels:
        lines = [LineString(l) for l in cg.lines(float(lev)) if len(l) >= 2]
        if not lines:
            continue
        g = MultiLineString(lines).intersection(frame_poly)
        if erase is not None and not erase.is_empty:
            g = g.difference(erase)
        g = g.simplify(tol_m, preserve_topology=False)
        out = [l for l in iter_lines(g) if l.length >= min_len_m]
        n_pts += sum(len(l.coords) for l in out)
        is_index = index_every > 0 and (round(lev / interval) % index_every == 0)
        (index if is_index else normal).extend((lev, l) for l in out)
    return normal, index, levels, n_pts


def hatch_polygon(poly, spacing_m, angle_deg=45.0):
    """Hachures parallèles à l'intérieur d'un polygone."""
    minx, miny, maxx, maxy = poly.bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    diag = math.hypot(maxx - minx, maxy - miny)
    a = math.radians(angle_deg)
    ux, uy = math.cos(a), math.sin(a)          # direction des hachures
    nx, ny = -uy, ux                             # normale
    lines = []
    n = int(diag / spacing_m) + 1
    for i in range(-n, n + 1):
        ox, oy = cx + nx * i * spacing_m, cy + ny * i * spacing_m
        lines.append(LineString([(ox - ux * diag, oy - uy * diag), (ox + ux * diag, oy + uy * diag)]))
    g = MultiLineString(lines).intersection(poly)
    return list(iter_lines(g))


# ----------------------------------------------------------------------------
# Texte → chemins SVG (fontTools)
# ----------------------------------------------------------------------------
def find_font(candidates):
    dirs = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
            "/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"),
            "/Library/Fonts", os.path.expanduser("~/Library/Fonts"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")]
    for c in candidates:
        if os.path.isfile(c):
            return c
        for d in dirs:
            if not d or not os.path.isdir(d):
                continue
            hits = glob.glob(os.path.join(d, "**", c), recursive=True)
            hits = [h for h in hits if h.lower().endswith((".ttf", ".otf"))]
            if hits:
                return hits[0]
    return None


TITLE_FONTS = ["FunnelDisplay-Bold.ttf", "FunnelDisplay-SemiBold.ttf", "Funnel Display*Bold*.ttf",
               "Mulish-Bold.ttf", "Mulish-ExtraBold.ttf", "bahnschrift.ttf", "segoeuib.ttf",
               "arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"]
BODY_FONTS = ["Mulish-Regular.ttf", "Mulish-Medium.ttf", "Mulish*Regular*.ttf", "segoeui.ttf",
              "bahnschrift.ttf", "arial.ttf", "DejaVuSans.ttf", "Arial.ttf"]


class TextEngine:
    def __init__(self, font_path):
        self.path = font_path
        self.font = TTFont(font_path, fontNumber=0)
        self.gs = self.font.getGlyphSet()
        self.cmap = self.font.getBestCmap() or {}
        self.upem = self.font["head"].unitsPerEm
        self.hmtx = self.font["hmtx"].metrics
        os2 = self.font["OS/2"] if "OS/2" in self.font else None
        self.cap_height = (getattr(os2, "sCapHeight", 0) or 0.7 * self.upem) / self.upem

    def has(self, ch):
        return ord(ch) in self.cmap

    def _glyphs(self, text):
        out = []
        for ch in text:
            g = self.cmap.get(ord(ch))
            if g is None:
                # repli : sans accent, sinon espace
                import unicodedata
                base = unicodedata.normalize("NFKD", ch)
                base = "".join(c for c in base if not unicodedata.combining(c))
                g = self.cmap.get(ord(base[0])) if base else None
                if g is None:
                    g = self.cmap.get(32)
            out.append(g)
        return out

    def width(self, text, size):
        s = size / self.upem
        return sum(self.hmtx[g][0] for g in self._glyphs(text) if g in self.hmtx) * s

    def path_d(self, text, size, x, y, anchor="start"):
        """Chemin SVG (coordonnées absolues en mm) du texte, ligne de base en y."""
        w = self.width(text, size)
        if anchor == "middle":
            x -= w / 2
        elif anchor == "end":
            x -= w
        s = size / self.upem
        pen = SVGPathPen(self.gs, ntos=lambda v: f"{v:.3f}".rstrip("0").rstrip("."))
        cur = x
        for g in self._glyphs(text):
            if g not in self.gs:
                continue
            tp = TransformPen(pen, (s, 0, 0, -s, cur, y))
            self.gs[g].draw(tp)
            cur += self.hmtx[g][0] * s
        return pen.getCommands()


# ----------------------------------------------------------------------------
# Écriture SVG
# ----------------------------------------------------------------------------
def fmt_num(v):
    return f"{v:.3f}".rstrip("0").rstrip(".")


class SvgDoc:
    def __init__(self, W, H):
        self.W, self.H = W, H
        self.items = OrderedDict((k, []) for k in LAYERS)

    def polyline(self, layer, pts_mm):
        pts = " ".join(f"{fmt_num(x)},{fmt_num(y)}" for x, y in pts_mm)
        self.items[layer].append(f'<polyline points="{pts}"/>')

    def polygon(self, layer, poly_mm_rings):
        d = []
        for ring in poly_mm_rings:
            d.append("M" + " L".join(f"{fmt_num(x)} {fmt_num(y)}" for x, y in ring) + " Z")
        self.items[layer].append(f'<path d="{" ".join(d)}"/>')

    def path(self, layer, d):
        if d.strip():
            self.items[layer].append(f'<path d="{d}"/>')

    def rect(self, layer, x, y, w, h, rx=0.0):
        self.items[layer].append(
            f'<rect x="{fmt_num(x)}" y="{fmt_num(y)}" width="{fmt_num(w)}" height="{fmt_num(h)}"'
            + (f' rx="{fmt_num(rx)}"' if rx else "") + "/>")

    def write(self, path, stroke_width=0.1):
        W, H = fmt_num(self.W), fmt_num(self.H)
        out = ['<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
               f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
               f'width="{W}mm" height="{H}mm" viewBox="0 0 {W} {H}" version="1.1">',
               "<!-- Généré par gpx2engraving. Unités : mm. Un calque LightBurn par couleur. -->"]
        for name, (color, filled, desc) in LAYERS.items():
            els = self.items[name]
            if not els:
                continue
            fill = color if filled else "none"
            sw = 0.02 if filled else stroke_width
            out.append(f'<g id="{name}" inkscape:label="{name}" inkscape:groupmode="layer" '
                       f'fill="{fill}" stroke="{color}" stroke-width="{sw}" stroke-linejoin="round" stroke-linecap="round">')
            out.append(f"<!-- {desc} -->")
            out.extend(els)
            out.append("</g>")
        out.append("</svg>")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out))


# ----------------------------------------------------------------------------
# Aperçu PNG (matplotlib) fidèle au SVG
# ----------------------------------------------------------------------------
_PATH_TOKEN = re.compile(r"[MLHVCQZmlhvcqz]|-?\d*\.?\d+(?:e-?\d+)?")


def svg_d_to_mpl(d):
    from matplotlib.path import Path
    toks = _PATH_TOKEN.findall(d)
    verts, codes = [], []
    i = 0
    cur = (0.0, 0.0)
    cmd = None
    while i < len(toks):
        t = toks[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in "Zz":
                verts.append((0, 0))
                codes.append(Path.CLOSEPOLY)
            continue
        if cmd in "M":
            cur = (float(toks[i]), float(toks[i + 1]))
            verts.append(cur); codes.append(Path.MOVETO); i += 2; cmd = "L"
        elif cmd == "L":
            cur = (float(toks[i]), float(toks[i + 1]))
            verts.append(cur); codes.append(Path.LINETO); i += 2
        elif cmd == "H":
            cur = (float(toks[i]), cur[1]); verts.append(cur); codes.append(Path.LINETO); i += 1
        elif cmd == "V":
            cur = (cur[0], float(toks[i])); verts.append(cur); codes.append(Path.LINETO); i += 1
        elif cmd == "Q":
            p1 = (float(toks[i]), float(toks[i + 1])); p2 = (float(toks[i + 2]), float(toks[i + 3]))
            verts += [p1, p2]; codes += [Path.CURVE3, Path.CURVE3]; cur = p2; i += 4
        elif cmd == "C":
            p1 = (float(toks[i]), float(toks[i + 1])); p2 = (float(toks[i + 2]), float(toks[i + 3]))
            p3 = (float(toks[i + 4]), float(toks[i + 5]))
            verts += [p1, p2, p3]; codes += [Path.CURVE4] * 3; cur = p3; i += 6
        else:
            i += 1
    if not verts:
        return None
    return Path(verts, codes)


def render_preview(svg, path_png, dpi=300):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import PathPatch, Polygon as MplPolygon, Rectangle
    # Aperçu : rendu "bois" — traits sombres, la couleur ne sert qu'à distinguer les calques
    preview_colors = {"frame": "#113B54", "contours": "#5A7D8C", "contours_index": "#113B54",
                      "water": "#024442", "water_hatch": "#024442", "track": "#113B54",
                      "track_line": "#113B54", "profile": "#113B54", "profile_fill": "#BBD1FF",
                      "text": "#113B54"}
    fig = plt.figure(figsize=(svg.W / 25.4, svg.H / 25.4), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, svg.W); ax.set_ylim(svg.H, 0); ax.axis("off")
    fig.patch.set_facecolor("#F3E7D3")
    pt_per_mm = 72 / 25.4
    for name, (color, filled, _) in LAYERS.items():
        col = preview_colors.get(name, color)
        for el in svg.items[name]:
            if el.startswith("<polyline"):
                pts = re.search(r'points="([^"]*)"', el).group(1).split()
                xy = np.array([[float(v) for v in p.split(",")] for p in pts])
                lw = 0.12 if name in ("contours",) else 0.2
                ax.plot(xy[:, 0], xy[:, 1], color=col, lw=lw * pt_per_mm, solid_capstyle="round")
            elif el.startswith("<path"):
                d = re.search(r'd="([^"]*)"', el).group(1)
                p = svg_d_to_mpl(d)
                if p is None:
                    continue
                ax.add_patch(PathPatch(p, facecolor=col if filled else "none", edgecolor=col,
                                       lw=(0.05 if filled else 0.15) * pt_per_mm))
            elif el.startswith("<rect"):
                g = lambda k: float(re.search(k + r'="([^"]*)"', el).group(1))
                ax.add_patch(Rectangle((g("x"), g("y")), g("width"), g("height"), fill=False,
                                       edgecolor=col, lw=0.3 * pt_per_mm))
    fig.savefig(path_png, dpi=dpi, facecolor=fig.get_facecolor())
    plt.close(fig)


# ----------------------------------------------------------------------------
# Formatage
# ----------------------------------------------------------------------------
def fmt_km(m):
    km = m / 1000.0
    s = f"{km:.1f}" if km < 100 else f"{km:.0f}"
    return s.replace(".", ",") + " km"


def fmt_m(v):
    v = int(round(v))
    return f"{v:,}".replace(",", " ") + " m"


def fmt_date_fr(t):
    return f"{t.day} {FR_MONTHS[t.month - 1]} {t.year}"


def fmt_duration(td):
    s = int(td.total_seconds())
    h, m = s // 3600, (s % 3600) // 60
    return f"{h} h {m:02d}" if h else f"{m} min"


# ----------------------------------------------------------------------------
# Programme principal
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="GPX → SVG prêt à graver (LightBurn).",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("gpx", help="fichier GPX")
    ap.add_argument("--title", "-t", help="titre gravé (défaut : nom de la trace dans le GPX)")
    ap.add_argument("--out", "-o", help="SVG de sortie (défaut : à côté du GPX)")
    ap.add_argument("--max-size", type=float, default=200.0, help="côté maximal de la planche en mm")
    ap.add_argument("--format", choices=["auto", "square", "portrait", "landscape"], default="auto",
                    help="forme de la planche")
    ap.add_argument("--margin", type=float, default=250.0, help="marge terrain minimale autour de la trace (m)")
    ap.add_argument("--interval", type=float, default=0, help="équidistance des courbes (m) ; 0 = auto")
    ap.add_argument("--target-lines", type=int, default=40, help="nb de niveaux visé en mode auto")
    ap.add_argument("--index-every", type=int, default=5, help="1 courbe maîtresse toutes les N (0 = aucune)")
    ap.add_argument("--dem-res", type=float, default=5.0, help="résolution minimale du MNT (m)")
    ap.add_argument("--dem-layer", default="ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES",
                    help="couche WMS IGN (HIGHRES = RGE ALTI 1-5 m ; ELEVATION.ELEVATIONGRIDCOVERAGE = BD ALTI 25 m)")
    ap.add_argument("--smooth", type=float, default=1.2, help="lissage gaussien du MNT (pixels)")
    ap.add_argument("--simplify", type=float, default=0.06, help="tolérance de simplification des tracés (mm)")
    ap.add_argument("--min-length", type=float, default=1.5, help="longueur minimale d'un morceau de courbe (mm)")
    ap.add_argument("--track-width", type=float, default=1.1, help="largeur gravée de la trace (mm)")
    ap.add_argument("--track-style", choices=["fill", "line", "both"], default="fill",
                    help="trace en polygone rempli, en ligne d'axe, ou les deux")
    ap.add_argument("--halo", type=float, default=0.45, help="halo vide autour de la trace (mm) ; 0 = aucun")
    ap.add_argument("--water", choices=["ign", "osm", "none"], default="ign", help="source des plans d'eau")
    ap.add_argument("--water-hatch", type=float, default=0.9, help="espacement des hachures des lacs (mm) ; 0 = contour seul")
    ap.add_argument("--water-min-area", type=float, default=4.0, help="surface minimale d'un lac sur la planche (mm²)")
    ap.add_argument("--elev-source", choices=["auto", "gpx", "dem"], default="auto",
                    help="altitude pour le D+ et le profil : GPX, MNT, ou GPX si présent sinon MNT")
    ap.add_argument("--dplus-threshold", type=float, default=5.0, help="seuil d'hystérésis du D+ (m)")
    ap.add_argument("--elev-smooth", type=float, default=60.0, help="fenêtre de lissage de l'altitude (m)")
    ap.add_argument("--no-profile", action="store_true", help="pas de profil altimétrique")
    ap.add_argument("--profile-height", type=float, default=28.0, help="hauteur du bloc profil (mm)")
    ap.add_argument("--profile-fill", action="store_true", help="ajoute l'aire sous le profil (calque remplissage)")
    ap.add_argument("--show-duration", action="store_true", help="ajoute la durée totale aux statistiques")
    ap.add_argument("--show-moving", action="store_true", help="ajoute le temps en mouvement aux statistiques")
    ap.add_argument("--date", help="date à afficher (texte libre) ; défaut : date du GPX en français")
    ap.add_argument("--no-date", action="store_true")
    ap.add_argument("--corner-radius", type=float, default=4.0, help="rayon des angles du contour de découpe (mm)")
    ap.add_argument("--no-frame", action="store_true", help="pas de contour de découpe")
    ap.add_argument("--map-border", action="store_true", help="trace un cadre fin autour de la carte")
    ap.add_argument("--title-size", type=float, default=7.5, help="corps du titre (mm, réduit automatiquement si trop long)")
    ap.add_argument("--stats-size", type=float, default=3.6, help="corps de la ligne de statistiques (mm)")
    ap.add_argument("--label-size", type=float, default=2.4, help="corps des étiquettes du profil (mm)")
    ap.add_argument("--font", help="police du corps de texte (.ttf/.otf)")
    ap.add_argument("--title-font", help="police du titre (.ttf/.otf)")
    ap.add_argument("--no-preview", action="store_true", help="ne génère pas l'aperçu PNG")
    ap.add_argument("--cache-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache"))
    args = ap.parse_args()

    # ---- polices ----
    title_font = find_font([args.title_font] if args.title_font else TITLE_FONTS)
    body_font = find_font([args.font] if args.font else BODY_FONTS)
    if not title_font or not body_font:
        raise SystemExit("Aucune police trouvée. Indiquez --font / --title-font (fichier .ttf).")
    T_title, T_body = TextEngine(title_font), TextEngine(body_font)
    log(f"Polices : titre={os.path.basename(title_font)}  corps={os.path.basename(body_font)}")

    # ---- GPX ----
    log(f"Lecture GPX : {args.gpx}")
    gpx_name, segs, meta_time = read_gpx(args.gpx)
    track = Track(segs)
    title = args.title or gpx_name or os.path.splitext(os.path.basename(args.gpx))[0]
    n_pts = len(track.x)
    log(f"  {n_pts} points, {len(segs)} segment(s), longueur {track.length / 1000:.2f} km")

    # ---- mise en page ----
    lay = Layout(track.bbox, args)
    fx0, fy0, fx1, fy1 = lay.frame
    log(f"Planche : {lay.W} x {lay.H} mm  |  carte {lay.map_w:.1f} x {lay.map_h:.1f} mm  |  "
        f"emprise {fx1 - fx0:.0f} x {fy1 - fy0:.0f} m  |  échelle 1:{1000 / lay.scale:.0f}")

    # ---- MNT ----
    # résolution : au moins dem_res, et pas plus fine que ~0.18 mm sur la planche
    res = max(args.dem_res, round(0.18 / lay.scale / 5) * 5 or args.dem_res)
    pad = 6 * res
    dem, dem_bbox = fetch_dem((fx0 - pad, fy0 - pad, fx1 + pad, fy1 + pad), res, args.cache_dir, args.dem_layer)

    # ---- altitude, D+, profil ----
    use_gpx = np.isfinite(track.ele).sum() > 0.9 * n_pts
    if args.elev_source == "gpx" or (args.elev_source == "auto" and use_gpx):
        ele_src, src_name = track.ele, "GPX"
    else:
        ele_src, src_name = sample_dem(dem, dem_bbox, res, track.x, track.y), "MNT"
    step = 5.0
    d_rs, e_rs = resample(track.dist, ele_src, step)
    e_sm = smooth(e_rs, args.elev_smooth / step)
    gain, loss = elevation_gain(e_sm, args.dplus_threshold)
    log(f"Altitude ({src_name}) : min {e_sm.min():.0f} m, max {e_sm.max():.0f} m  |  D+ {gain:.0f} m  D- {loss:.0f} m")

    # ---- textes ----
    start = track.start_time() or meta_time
    stats = [fmt_km(track.length), fmt_m(gain) + " D+"]
    if args.show_duration and track.duration():
        stats.append(fmt_duration(track.duration()))
    if args.show_moving and track.duration():
        stats.append(fmt_duration(track.moving_duration()) + " en mouvement")
    if not args.no_date:
        if args.date:
            stats.append(args.date)
        elif start:
            stats.append(fmt_date_fr(start.astimezone()))
    sep = "  ·  " if T_body.has("·") else "   -   "
    stats_txt = sep.join(stats)
    log(f"Statistiques : {stats_txt}")

    # ---- géométries en mètres ----
    frame_poly = box(fx0, fy0, fx1, fy1)
    tol_m = lay.mm_to_m(args.simplify)
    track_lines = [LineString(s).simplify(tol_m) for s in track.segments if len(s) >= 2]
    track_union = unary_union(track_lines)
    half_w_m = lay.mm_to_m(args.track_width) / 2
    track_poly = track_union.buffer(half_w_m, quad_segs=6).simplify(tol_m / 2)
    erase = None
    if args.halo > 0:
        erase = track_union.buffer(half_w_m + lay.mm_to_m(args.halo), quad_segs=4)

    # ---- plans d'eau ----
    water_polys, water_names = [], []
    if args.water != "none":
        try:
            if args.water == "ign":
                polys, names = fetch_water_ign((fx0, fy0, fx1, fy1))
            else:
                tr_inv = Transformer.from_crs(2154, 4326, always_xy=True)
                lon0, lat0 = tr_inv.transform(fx0, fy0)
                lon1, lat1 = tr_inv.transform(fx1, fy1)
                polys, names = fetch_water_osm((lat0, lon0, lat1, lon1))
            min_area_m2 = args.water_min_area / (lay.scale ** 2)
            for p, nm in zip(polys, names):
                c = p.intersection(frame_poly)
                parts = [q for q in iter_polys(c) if q.area >= min_area_m2]
                if not parts:
                    continue
                water_polys.append(unary_union(parts).simplify(tol_m))
                water_names.append(nm)
            log(f"Plans d'eau ({args.water}) : {len(water_polys)} retenu(s) " + ", ".join(water_names))
        except Exception as e:
            log(f"  ! plans d'eau ignorés : {e}")
    if water_polys:
        wu = unary_union(water_polys)
        erase = wu if erase is None else unary_union([erase, wu])

    # ---- courbes de niveau ----
    # équidistance déterminée sur l'altitude visible dans le cadre
    zvis = dem[6:-6, 6:-6] if dem.shape[0] > 20 else dem
    zrange = float(np.nanmax(zvis) - np.nanmin(zvis))
    interval = args.interval or auto_interval(zrange, args.target_lines)
    log(f"Courbes : équidistance {interval:g} m (dénivelé visible {zrange:.0f} m), maîtresse toutes les {args.index_every}")
    normal, index, levels, npts = make_contours(dem, dem_bbox, res, interval, args.smooth, frame_poly, erase,
                                                tol_m, lay.mm_to_m(args.min_length), args.index_every)
    log(f"  {len(normal)} courbes + {len(index)} maîtresses, {npts} sommets, {len(levels)} niveaux")

    # ---- construction SVG ----
    svg = SvgDoc(lay.W, lay.H)
    if not args.no_frame:
        svg.rect("frame", 0, 0, lay.W, lay.H, rx=args.corner_radius)
    if args.map_border:
        svg.rect("profile", lay.map_x, lay.map_y, lay.map_w, lay.map_h)

    def line_mm(geom):
        x, y = lay.to_mm(*np.asarray(geom.coords).T)
        return list(zip(x, y))

    def poly_mm(poly):
        rings = [line_mm(poly.exterior)] + [line_mm(r) for r in poly.interiors]
        return rings

    for lev, l in normal:
        svg.polyline("contours", line_mm(l))
    for lev, l in index:
        svg.polyline("contours_index", line_mm(l))
    for p in water_polys:
        for pp in iter_polys(p):
            svg.polygon("water", poly_mm(pp))
            if args.water_hatch > 0:
                inner = pp.buffer(-lay.mm_to_m(0.25))
                if inner.is_empty:
                    continue
                for h in hatch_polygon(inner, lay.mm_to_m(args.water_hatch)):
                    svg.polyline("water_hatch", line_mm(h))
    if args.track_style in ("fill", "both"):
        for pp in iter_polys(track_poly):
            svg.polygon("track", poly_mm(pp))
    if args.track_style in ("line", "both"):
        for l in track_lines:
            svg.polyline("track_line", line_mm(l))

    # titre : réduit jusqu'à tenir dans la largeur
    size = args.title_size
    while T_title.width(title, size) > lay.map_w and size > 3:
        size *= 0.95
    svg.path("text", T_title.path_d(title, size, lay.W / 2, lay.title_baseline, "middle"))

    # statistiques
    size = args.stats_size
    while T_body.width(stats_txt, size) > lay.map_w and size > 2:
        size *= 0.95
    svg.path("text", T_body.path_d(stats_txt, size, lay.W / 2, lay.stats_baseline, "middle"))

    # profil altimétrique
    if lay.profile_rect:
        px, py, pw, ph = lay.profile_rect
        lab = args.label_size
        emin, emax = float(e_sm.min()), float(e_sm.max())
        zlo = math.floor((emin - 10) / 50) * 50
        zhi = math.ceil((emax + 10) / 50) * 50
        lab_w = max(T_body.width(fmt_m(zhi), lab), T_body.width(fmt_m(zlo), lab)) + 1.5
        gx0, gx1 = px + lab_w, px + pw
        gy1 = py + ph - lab - 1.2          # ligne de base (au-dessus des étiquettes km)
        gy0 = py + 0.6                     # sommet de la zone
        gw, gh = gx1 - gx0, gy1 - gy0

        def pxy(dm, z):
            return gx0 + dm / track.length * gw, gy1 - (z - zlo) / (zhi - zlo) * gh

        # courbe : rééchantillonnée pour ~0.15 mm entre points
        n = max(50, int(gw / 0.15))
        dd = np.linspace(0, track.length, n)
        zz = np.interp(dd, d_rs, e_sm)
        curve = [pxy(a, b) for a, b in zip(dd, zz)]
        curve_ls = LineString(curve).simplify(0.03)
        svg.polyline("profile", list(curve_ls.coords))
        if args.profile_fill:
            svg.polygon("profile_fill", [list(curve_ls.coords) + [pxy(track.length, zlo), pxy(0, zlo)]])
        # base + montants
        svg.polyline("profile", [pxy(0, zlo), pxy(track.length, zlo)])
        # graduations altitude (gauche) : bas, haut, et médiane si place
        for z in {zlo, zhi} | ({(zlo + zhi) / 2} if gh > 12 else set()):
            x, y = pxy(0, z)
            svg.polyline("profile", [(x - 1.0, y), (x, y)])
            svg.path("text", T_body.path_d(fmt_m(z), lab, x - 1.6, y + lab * 0.35, "end"))
        # graduations distance : pas choisi pour ≤ 10 repères
        km = track.length / 1000
        stepk = next(s for s in (1, 2, 5, 10, 20, 50) if km / s <= 10)
        end_lab_w = T_body.width(fmt_km(track.length), lab)
        x_end = pxy(track.length, zlo)[0]
        k = stepk
        while k < km - 0.15 * stepk:
            x, y = pxy(k * 1000, zlo)
            svg.polyline("profile", [(x, y), (x, y + 0.9)])
            # étiquette seulement si elle ne chevauche pas celle de fin
            if x + T_body.width(f"{k:g}", lab) / 2 < x_end - end_lab_w - 0.8:
                svg.path("text", T_body.path_d(f"{k:g}", lab, x, y + 0.9 + lab, "middle"))
            k += stepk
        x, y = pxy(track.length, zlo)
        svg.polyline("profile", [(x, y), (x, y + 0.9)])
        svg.path("text", T_body.path_d(fmt_km(track.length), lab, x, y + 0.9 + lab, "end"))
        x, y = pxy(0, zlo)
        svg.polyline("profile", [(x, y), (x, y + 0.9)])
        svg.path("text", T_body.path_d("0", lab, x, y + 0.9 + lab, "middle"))

    out = args.out or os.path.splitext(args.gpx)[0] + ".svg"
    svg.write(out)
    size_kb = os.path.getsize(out) / 1024
    log(f"SVG écrit : {out} ({size_kb:.0f} ko)")
    if not args.no_preview:
        png = os.path.splitext(out)[0] + "_apercu.png"
        render_preview(svg, png)
        log(f"Aperçu : {png}")

    # résumé JSON (utile pour un lot)
    summary = dict(title=title, gpx=os.path.abspath(args.gpx), svg=os.path.abspath(out),
                   plate_mm=[lay.W, lay.H], scale=f"1:{1000 / lay.scale:.0f}", distance_m=round(track.length),
                   gain_m=round(gain), loss_m=round(loss), ele_min=round(float(e_sm.min())),
                   ele_max=round(float(e_sm.max())), date=start.isoformat() if start else None,
                   duration_s=int(track.duration().total_seconds()) if track.duration() else None,
                   contour_interval=interval, water=water_names, elevation_source=src_name)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
