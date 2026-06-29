#!/usr/bin/env python3

import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config():
    """
    Load config from (in order):
      1. CONFIG_JSON env var (GitHub Actions secret)
      2. config.json file (local dev)
      3. config.example.json (template fallback)

    Returns a dict. Exits with error if no config can be loaded.
    """
    # 1. env var
    env_cfg = os.environ.get("CONFIG_JSON", "").strip()
    if env_cfg:
        try:
            cfg = json.loads(env_cfg)
            print(f"[CONFIG] Loaded from CONFIG_JSON env var ({len(env_cfg)} bytes)")
            return cfg
        except Exception as e:
            sys.stderr.write(f"[CONFIG] ERROR parsing CONFIG_JSON env var: {e}\n")
            sys.exit(1)

    # 2. local file
    local_path = os.path.join(SCRIPT_DIR, "config.json")
    if os.path.exists(local_path):
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            print(f"[CONFIG] Loaded from {local_path}")
            return cfg
        except Exception as e:
            sys.stderr.write(f"[CONFIG] ERROR parsing {local_path}: {e}\n")
            sys.exit(1)

    # 3. fallback template
    template_path = os.path.join(SCRIPT_DIR, "config.example.json")
    if os.path.exists(template_path):
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            print(f"[CONFIG] WARNING: using {template_path} (no real values)")
            print(f"[CONFIG] Create config.json or set CONFIG_JSON env var for production.")
            return cfg
        except Exception as e:
            sys.stderr.write(f"[CONFIG] ERROR parsing {template_path}: {e}\n")
            sys.exit(1)

    sys.stderr.write(
        "[CONFIG] FATAL: no config found. Set CONFIG_JSON env var or create config.json\n"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# HTTP / decode utilities
# ---------------------------------------------------------------------------

def make_http_get(ua, timeout):
    """Returns a http_get function bound to the given UA and timeout."""
    def http_get(url):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": ua, "Accept": "*/*"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            sys.stderr.write(f"WARN: GET {url} -> {e}\n")
            return ""
    return http_get


def b64decode_safe(s):
    """Decodes base64 with auto-padding. If already an URL, returns as-is."""
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        return s
    s2 = s + "=" * ((4 - len(s) % 4) % 4) if len(s) % 4 else s
    try:
        return base64.b64decode(s2).decode("utf-8", errors="replace")
    except Exception:
        return ""


def clean_title(t):
    """Strips BBCode tags [COLOR lime]...[/COLOR] [B]...[/B] [CR]."""
    if not t:
        return ""
    tags = [
        "[B]", "[/B]", "[CR]",
        "[COLOR lime]", "[COLOR aqua]", "[COLOR gold]", "[COLOR yellow]",
        "[COLOR blue]", "[COLOR red]", "[COLOR cyan]", "[COLOR magenta]",
        "[/COLOR]",
    ]
    for tag in tags:
        t = t.replace(tag, " ")
    return " ".join(t.split()).strip()


# ---------------------------------------------------------------------------
# Backend auto-discovery
# ---------------------------------------------------------------------------

def backend_is_alive(backend, backend_path, ua, timeout):
    """Tests if backend responds 200 to filter.php?numTest=JOB200."""
    try:
        url = backend + backend_path.format("JOB200")
        req = urllib.request.Request(
            url,
            headers={"User-Agent": ua, "Accept": "*/*"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _host_only(url):
    """Returns only the host of a URL, stripping scheme and path (for log)."""
    if not url:
        return "(none)"
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else url


def discover_backend(cfg, http_get):
    """
    Reads disclaimer JSON to find candidate backends, tests each,
    returns the first that responds 200. Falls back to cfg['backend_url'].
    Logs only generic status (no URLs/hosts in clear text).
    """
    print("\n[BACKEND DISCOVERY]")
    disclaimer_url = cfg.get("disclaimer_url", "")
    default_backend = cfg.get("backend_url", "")
    backend_path = cfg.get("backend_path", "/filter.php?numTest={}")
    ua = cfg.get("user_agent", "Mozilla/5.0")
    timeout = cfg.get("http_timeout_seconds", 15)

    candidates = []
    if disclaimer_url:
        print("  Reading disclaimer source...")
        body = http_get(disclaimer_url)
        if body:
            try:
                d = json.loads(body)
                def walk(items):
                    if not isinstance(items, list):
                        return
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        link = it.get("externallink", "") or ""
                        m = re.match(r"(https?://[^/]+)/filter\.php", link)
                        if m:
                            candidates.append(m.group(1))
                        if "items" in it and isinstance(it["items"], list):
                            walk(it["items"])
                walk(d.get("items", []))
            except Exception as e:
                sys.stderr.write(f"  WARN: disclaimer JSON invalid: {e}\n")

    if default_backend and default_backend not in candidates:
        candidates.insert(0, default_backend)

    # dedup preserving order
    seen = set()
    unique = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    print(f"  Candidates found: {len(unique)}")

    # sort: default first, HTTPS before HTTP, then alphabetical
    def sort_key(b):
        return (
            0 if b == default_backend else 1,
            0 if b.startswith("https://") else 1,
            b,
        )
    unique.sort(key=sort_key)

    for i, c in enumerate(unique, 1):
        print(f"  Test candidate {i}/{len(unique)} ...", end=" ", flush=True)
        if backend_is_alive(c, backend_path, ua, timeout):
            print("ALIVE")
            return c
        else:
            print("DEAD")

    print("  WARNING: no alive backend! Using fallback.")
    return default_backend


# ---------------------------------------------------------------------------
# Sections auto-discovery
# ---------------------------------------------------------------------------

def discover_sections(cfg, backend, http_get):
    """
    Starts from root_sections, follows externallink references to find
    sub-sections. Merges with known_sections. Returns {numTest: label}.
    Logs only counts (no section IDs in clear text).
    """
    print("\n[SECTIONS DISCOVERY]")
    backend_path = cfg.get("backend_path", "/filter.php?numTest={}")
    root_sections = cfg.get("root_sections", [])
    known = dict(cfg.get("known_sections", {}))
    max_depth = cfg.get("max_discovery_depth", 1)

    discovered = dict(known)
    visited = set()
    queue = [(nt, "", 0) for nt in root_sections]

    while queue:
        numtest, _, depth = queue.pop(0)
        if numtest in visited:
            continue
        visited.add(numtest)
        if depth > max_depth:
            continue

        url = backend + backend_path.format(numtest)
        body = http_get(url)
        if not body:
            continue
        try:
            d = json.loads(body)
        except Exception:
            continue

        sub_links = []
        def walk(obj):
            if isinstance(obj, list):
                for it in obj:
                    walk(it)
                return
            if not isinstance(obj, dict):
                return
            link = obj.get("externallink", "") or ""
            m = re.search(r"filter\.php\?numTest=([A-Za-z0-9_]+)", link)
            if m:
                sub_nt = m.group(1)
                if sub_nt not in visited:
                    sub_links.append((sub_nt, obj.get("title", "")))
            for k in ("items", "channels"):
                if k in obj:
                    walk(obj[k])

        if isinstance(d, dict):
            walk(d.get("items", []))
            if "channels" in d:
                walk(d["channels"])
        else:
            walk(d)

        if sub_links and depth < max_depth:
            for sub_nt, sub_title in sub_links:
                if sub_nt not in discovered:
                    discovered[sub_nt] = clean_title(sub_title)[:50] or sub_nt
                queue.append((sub_nt, "", depth + 1))

        if depth == 0:
            print(f"  Root section: {len(sub_links)} sub-sections discovered")

    print(f"  Total sections to fetch: {len(discovered)}")
    return discovered


# ---------------------------------------------------------------------------
# Stream extraction
# ---------------------------------------------------------------------------

def fetch_section(backend, backend_path, numtest, http_get):
    """Fetches a section JSON. Returns dict or None."""
    url = backend + backend_path.format(numtest)
    body = http_get(url)
    if not body:
        return None
    try:
        return json.loads(body)
    except Exception as e:
        sys.stderr.write(f"  WARN: JSON parse failed for {numtest}: {e}\n")
        return None


def walk_items(items, source_label, field_names, resolver_prefix, found, default_origin):
    """Recursively searches for resolver_prefix in the configured fields."""
    if not isinstance(items, list):
        return
    for it in items:
        if not isinstance(it, dict):
            continue
        title = clean_title(it.get("title", ""))
        # per-item origin override (future-proof)
        item_origin = None
        for ovk in ("origin", "referer"):
            ov = it.get(ovk, "")
            if ov and isinstance(ov, str) and ov.startswith("http"):
                item_origin = ov
                break
        if not item_origin:
            item_origin = default_origin

        for k in field_names:
            v = it.get(k, "")
            if isinstance(v, str) and resolver_prefix in v:
                payload = v.split(resolver_prefix, 1)[1]
                parts = payload.split("|", 1)
                url_b64 = parts[0]
                key_b64 = parts[1] if len(parts) > 1 else "0000"
                url = b64decode_safe(url_b64)
                # decode key
                if key_b64 == "0000" or not key_b64:
                    key = ""
                elif ":" in key_b64 and len(key_b64) <= 100:
                    key = key_b64
                else:
                    k2 = b64decode_safe(key_b64)
                    if "{" in k2:
                        try:
                            obj = json.loads(k2)
                            kid = obj.get("kid", "")
                            kv = obj.get("key", "")
                            key = f"{kid}:{kv}" if kid and kv else ""
                        except Exception:
                            key = k2.replace("{", "").replace("}", "").replace('"', "")
                    else:
                        key = k2
                found.append({
                    "source": source_label,
                    "title": title,
                    "url": url,
                    "key": key,
                    "origin": item_origin,
                })
        # recurse on sub-items
        for subk in ("items", "channels"):
            if subk in it and isinstance(it[subk], list):
                walk_items(it[subk], source_label, field_names, resolver_prefix,
                           found, default_origin)


# ---------------------------------------------------------------------------
# Categorization + M3U builder
# ---------------------------------------------------------------------------

def categorize(url):
    """Returns (family, group_title) for the M3U group-title."""
    if not url:
        return ("unknown", "Misc")
    u = url.lower()
    # Italian providers
    if "dazn" in u:
        if "mocdn" in u:
            return ("DAZN", "DAZN IT (Serie B)")
        return ("DAZN", "DAZN IT (Live)")
    if "skycdp" in u or "cssott" in u:
        return ("SKY", "Sky IT")
    if "timlivetu" in u or ("ticdn.it" in u and "eurosport" in u):
        return ("TIMVision", "EuroSport IT")
    if "netplus.ch" in u:
        return ("Netplus CH", "RAI/Mediaset VPN CH")
    if "msvdn" in u:
        return ("MSVDN", "SuperTennis / MSVDN")
    if "mediaset.net" in u:
        return ("Mediaset", "Mediaset Play")
    if "akamaized" in u and "lba" in u:
        return ("LBA", "Lega Basket (LBA)")
    if "akamaized" in u and "raievent" in u:
        return ("RAI", "RAI 4K")
    # International providers
    if "izzigo" in u:
        return ("Izzi", "IzziESPN")
    if "t-mobile" in u or "lineartv" in u:
        return ("Eleven", "Eleven Sports PL")
    if "cgates" in u:
        return ("Setanta", "Setanta")
    if "tvx.prd.tv.odido" in u:
        return ("Ziggo", "Ziggo NL")
    if "aiv-cdn" in u or "otte.live" in u:
        return ("Amazon", "Amazon/aiv-cdn")
    if "c4assets" in u:
        return ("Channel4", "Channel 4 UK")
    if "netskrt" in u:
        return ("CBS Golazo", "CBS Golazo")
    if "karmakurama" in u:
        return ("KarmaKurama", "KarmaKurama")
    return ("Misc", "Misc")


def build_stream_headers(origin, ua):
    """Builds the stream_headers string with Origin/Referer/UA."""
    return (
        f"Referer={origin}/"
        f"&Origin={origin}"
        f"&User-Agent=" + urllib.parse.quote(ua)
    )


def build_m3u_entry(title, url, key, group, origin, ua):
    """Builds M3U lines for a stream. Compatible with Kodi & OTT Navigator."""
    headers = build_stream_headers(origin, ua)
    lines = []
    lines.append(f'#EXTINF:-1 tvg-name="{title}" group-title="{group}",{title}')
    lines.append("#KODIPROP:inputstreamaddon=inputstream.adaptive")
    lines.append("#KODIPROP:inputstream.adaptive.file_type=mpd")
    lines.append("#KODIPROP:inputstream.adaptive.manifest_headers=" + headers)
    lines.append("#KODIPROP:inputstream.adaptive.stream_headers=" + headers)
    if key and key != "0000":
        lines.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
        lines.append(f"#KODIPROP:inputstream.adaptive.license_key={key}")
    lines.append("#KODIPROP:mimetype=application/dash+xml")
    lines.append(url)
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print(" DASH playlist generator (config-driven)")
    print(f" Run at: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    cfg = load_config()

    ua = cfg.get("user_agent", "Mozilla/5.0")
    timeout = cfg.get("http_timeout_seconds", 15)
    http_get = make_http_get(ua, timeout)

    backend_path = cfg.get("backend_path", "/filter.php?numTest={}")
    default_origin = cfg.get("default_origin", "")
    resolver_prefix = cfg.get("resolver_prefix", "")
    if not resolver_prefix:
        sys.stderr.write("[CONFIG] ERROR: resolver_prefix not set in config\n")
        sys.exit(1)
    field_names = cfg.get("resolver_field_names", ["myresolve", "link", "externallink"])

    # Step 1: discover backend
    backend = discover_backend(cfg, http_get)

    # Step 2: discover sections
    sections = discover_sections(cfg, backend, http_get)

    # Step 3: download all sections, collect streams
    print("\n[DOWNLOAD SECTIONS]")
    all_streams = []
    seen_urls = set()
    total = len(sections)
    idx = 0
    ok_count = 0
    fail_count = 0
    for numtest, label in sorted(sections.items()):
        idx += 1
        print(f"  [{idx}/{total}] ...", end=" ", flush=True)
        d = fetch_section(backend, backend_path, numtest, http_get)
        if d is None:
            print("FAIL")
            fail_count += 1
            continue
        n_top = 0
        n_sub = 0
        if isinstance(d, dict):
            if "items" in d:
                n_top = len(d["items"])
                walk_items(d["items"], numtest, field_names, resolver_prefix,
                           all_streams, default_origin)
            if "channels" in d:
                for ch in d.get("channels", []):
                    if "items" in ch:
                        n_sub += len(ch["items"])
                        walk_items(ch["items"], numtest, field_names, resolver_prefix,
                                   all_streams, default_origin)
        print(f"OK ({n_top}+{n_sub})")
        ok_count += 1
    print(f"  Summary: {ok_count} OK, {fail_count} FAIL")

    # Step 4: dedup
    uniq = []
    for s in all_streams:
        k = s["url"][:120]
        if k in seen_urls:
            continue
        seen_urls.add(k)
        uniq.append(s)
    print(f"\n[RESULT] Streams: {len(all_streams)} total, {len(uniq)} unique")

    # Step 5: build M3U (no JSON dump — keeps provider info out of the repo)
    by_group = defaultdict(list)
    for s in uniq:
        fam, group = categorize(s["url"])
        s["family"] = fam
        s["group"] = group
        s["effective_origin"] = s.get("origin") or default_origin
        by_group[group].append(s)

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header_lines = [
        "#EXTM3U",
        f"# Last refresh: {now_utc}",
        f"# Total channels: {len(uniq)}",
    ] + cfg.get("playlist_header", []) + [""]

    m3u_lines = list(header_lines)
    for group in sorted(by_group.keys()):
        items = by_group[group]
        m3u_lines.append(f"# === {group} ({len(items)} channels) ===")
        for s in sorted(items, key=lambda x: x["title"]):
            entry = build_m3u_entry(
                s["title"], s["url"], s["key"], group, s["effective_origin"], ua
            )
            m3u_lines.extend(entry)
        m3u_lines.append("")

    output_m3u = os.path.join(SCRIPT_DIR, cfg.get("output_m3u", "playlist.m3u"))
    with open(output_m3u, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u_lines))
    print(f"[M3U] Saved: {os.path.basename(output_m3u)}")
    print(f"\nDONE. {len(uniq)} channels written.")


if __name__ == "__main__":
    main()
