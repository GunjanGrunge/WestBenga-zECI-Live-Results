from __future__ import annotations

import concurrent.futures
import html
import json
import math
import os
import re
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


ECI_BASE_URL = os.environ.get(
    "ECI_BASE_URL", "https://results.eci.gov.in/ResultAcGenMay2026"
).rstrip("/")
STATE_CODE = os.environ.get("ECI_STATE_CODE", "S25")
PORT = int(os.environ.get("PORT", "8000"))

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "10"))
PAGE_REFRESH_SECONDS = int(os.environ.get("PAGE_REFRESH_SECONDS", "120"))
STATE_CACHE_SECONDS = int(os.environ.get("STATE_CACHE_SECONDS", "10"))
DETAIL_CACHE_SECONDS = int(os.environ.get("DETAIL_CACHE_SECONDS", "60"))
DETAIL_WORKERS = int(os.environ.get("DETAIL_WORKERS", "18"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "10"))

BJP = "Bharatiya Janata Party"
TMC = "All India Trinamool Congress"

state_cache: dict[str, Any] = {"until": 0.0, "value": None}
detail_cache: dict[str, dict[str, Any]] = {}
cache_lock = threading.Lock()


def fetch_text(url: str) -> str:
    result = subprocess.run(
        ["curl", "-s", "--max-time", str(REQUEST_TIMEOUT_SECONDS), url],
        capture_output=True,
        timeout=REQUEST_TIMEOUT_SECONDS + 5,
    )
    if result.returncode != 0:
        raise urllib.error.URLError(f"curl error {result.returncode}: {result.stderr.decode()}")
    return result.stdout.decode("utf-8", errors="replace")


def clean_text(value: str) -> str:
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def clean_table_html(value: str) -> str:
    value = re.sub(r"<div class='tooltip'>.*?</div>", "", value, flags=re.I | re.S)
    value = re.sub(r"<div class=\"tooltip\">.*?</div>", "", value, flags=re.I | re.S)
    value = re.sub(r"<script.*?</script>", "", value, flags=re.I | re.S)
    value = re.sub(r"<style.*?</style>", "", value, flags=re.I | re.S)
    return value


def party_from_state_cell(value: str) -> str:
    match = re.search(r"<td[^>]*align='left'[^>]*>(.*?)</td>", value, flags=re.I | re.S)
    return clean_text(match.group(1) if match else value)


def number(value: str) -> int:
    match = re.search(r"-?[\d,]+", value or "")
    if not match:
        return 0
    return int(match.group(0).replace(",", ""))


def decimal(value: str) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return float(match.group(0)) if match else 0.0


def round_pair(value: str) -> tuple[int, int]:
    match = re.search(r"(\d+)\s*/\s*(\d+)", value or "")
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def last_updated_from(page: str) -> str:
    match = re.search(r"Last Updated at\s*<span>(.*?)</span>", page, re.I | re.S)
    return clean_text(match.group(1)) if match else ""


def discover_state_pages(first_page: str) -> list[str]:
    page_nums = {1}
    for match in re.finditer(rf"statewise{STATE_CODE}(\d+)\.htm", first_page):
        page_nums.add(int(match.group(1)))
    return [f"{ECI_BASE_URL}/statewise{STATE_CODE}{num}.htm" for num in sorted(page_nums)]


def parse_state_page(page: str) -> list[dict[str, Any]]:
    page = clean_table_html(page)
    rows: list[dict[str, Any]] = []
    pattern = re.compile(
        r"<tr>\s*"
        r"<td[^>]*align='left'[^>]*>(?P<constituency>.*?)</td>\s*"
        r"<td[^>]*align='right'[^>]*>(?P<const_no>.*?)</td>\s*"
        r"<td[^>]*align='left'[^>]*>(?P<leading_candidate>.*?)</td>\s*"
        r"<td[^>]*>(?P<leading_party_cell>.*?)</td>\s*"
        r"<td[^>]*align='left'[^>]*>(?P<trailing_candidate>.*?)</td>\s*"
        r"<td[^>]*>(?P<trailing_party_cell>.*?)</td>\s*"
        r"<td[^>]*align='right'[^>]*>(?P<margin>.*?)</td>\s*"
        r"<td[^>]*align='right'[^>]*>(?P<round>.*?)</td>\s*"
        r"<td[^>]*align='left'[^>]*>(?P<status>.*?)</td>\s*</tr>",
        re.I | re.S,
    )
    for match in pattern.finditer(page):
        done, total = round_pair(clean_text(match.group("round")))
        const_no = number(clean_text(match.group("const_no")))
        if not const_no:
            continue
        rows.append(
            {
                "constituency": clean_text(match.group("constituency")),
                "constituency_no": const_no,
                "leading_candidate": clean_text(match.group("leading_candidate")),
                "leading_party": party_from_state_cell(match.group("leading_party_cell")),
                "trailing_candidate": clean_text(match.group("trailing_candidate")),
                "trailing_party": party_from_state_cell(match.group("trailing_party_cell")),
                "margin": number(clean_text(match.group("margin"))),
                "round_done": done,
                "round_total": total,
                "round_left": max(total - done, 0),
                "status": clean_text(match.group("status")),
            }
        )
    return rows


def parse_constituency_detail(page: str) -> dict[str, Any]:
    done, round_total = round_pair(clean_text(page))
    rows = re.findall(
        r"<tr>\s*"
        r"<td[^>]*>.*?</td>\s*"
        r"<td[^>]*align='left'[^>]*>(?P<candidate>.*?)</td>\s*"
        r"<td[^>]*align='left'[^>]*>(?P<party>.*?)</td>\s*"
        r"<td[^>]*align='right'[^>]*>(?P<evm>.*?)</td>\s*"
        r"<td[^>]*align='right'[^>]*>(?P<postal>.*?)</td>\s*"
        r"<td[^>]*align='right'[^>]*>(?P<total>.*?)</td>\s*"
        r"<td[^>]*align='right'[^>]*>(?P<pct>.*?)</td>\s*</tr>",
        page,
        flags=re.I | re.S,
    )

    candidates = []
    bjp_votes = 0
    tmc_votes = 0
    total_votes = 0
    for candidate, party, evm, postal, total_cell, pct in rows:
        party_name = clean_text(party)
        votes = number(clean_text(total_cell))
        total_votes += votes
        candidate_row = {
            "candidate": clean_text(candidate),
            "party": party_name,
            "evm_votes": number(clean_text(evm)),
            "postal_votes": number(clean_text(postal)),
            "total_votes": votes,
            "vote_pct": decimal(clean_text(pct)),
        }
        candidates.append(candidate_row)
        if party_name == BJP:
            bjp_votes = votes
        elif party_name == TMC:
            tmc_votes = votes

    candidates.sort(key=lambda row: row["total_votes"], reverse=True)
    return {
        "round_done": done,
        "round_total": round_total,
        "round_left": max(round_total - done, 0),
        "bjp_votes": bjp_votes,
        "tmc_votes": tmc_votes,
        "total_votes": total_votes,
        "candidates": candidates,
    }


def get_constituency_detail(constituency_no: int) -> dict[str, Any]:
    key = str(constituency_no)
    now = time.time()
    with cache_lock:
        cached = detail_cache.get(key)
        if cached and cached["until"] > now:
            return cached["value"]

    url = f"{ECI_BASE_URL}/Constituencywise{STATE_CODE}{constituency_no}.htm"
    try:
        value = parse_constituency_detail(fetch_text(url))
        value["detail_url"] = url
        value["detail_error"] = ""
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        value = {
            "round_done": 0,
            "round_total": 0,
            "round_left": 0,
            "bjp_votes": 0,
            "tmc_votes": 0,
            "total_votes": 0,
            "candidates": [],
            "detail_url": url,
            "detail_error": str(exc),
        }

    with cache_lock:
        detail_cache[key] = {"until": now + DETAIL_CACHE_SECONDS, "value": value}
    return value


def probability_for(row: dict[str, Any]) -> dict[str, Any]:
    bjp_votes = row["bjp_votes"]
    tmc_votes = row["tmc_votes"]
    total_votes = max(row["total_votes"], bjp_votes + tmc_votes, 1)
    done = row["round_done"]
    total_rounds = row["round_total"]
    status = row["status"].lower()

    if bjp_votes == tmc_votes == 0:
        return {"lead_pct": 0.0, "probability": 50, "probability_note": "No BJP/TMC vote detail yet"}

    if "declared" in status or "won" in status:
        probability = 99 if row["leading_party"] in {BJP, TMC} else 95
    else:
        margin_votes = abs(bjp_votes - tmc_votes) or row["margin"]
        progress = done / total_rounds if total_rounds else 0.0
        margin_pct = margin_votes / total_votes * 100
        score = margin_pct * 3.8 + progress * 42
        probability = max(51, min(98, round(50 + score)))

    lead_pct = (row["margin"] / total_votes * 100) if total_votes else 0.0
    return {
        "lead_pct": round(lead_pct, 2),
        "probability": int(probability),
        "probability_note": "Heuristic from margin, vote share, and counting progress",
    }


def load_results() -> dict[str, Any]:
    now = time.time()
    with cache_lock:
        if state_cache["value"] is not None and state_cache["until"] > now:
            return state_cache["value"]

    first_url = f"{ECI_BASE_URL}/statewise{STATE_CODE}1.htm"
    first_page = fetch_text(first_url)
    page_urls = discover_state_pages(first_page)
    pages = {first_url: first_page}

    def fetch_page(url: str) -> tuple[str, str]:
        return url, fetch_text(url)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        for url, page in executor.map(fetch_page, [url for url in page_urls if url != first_url]):
            pages[url] = page

    rows: list[dict[str, Any]] = []
    for url in page_urls:
        rows.extend(parse_state_page(pages.get(url, "")))

    rows_by_no = {row["constituency_no"]: row for row in rows}
    rows = [rows_by_no[key] for key in sorted(rows_by_no)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as executor:
        details = list(executor.map(lambda row: get_constituency_detail(row["constituency_no"]), rows))

    for row, detail in zip(rows, details):
        row.update(
            {
                "bjp_votes": detail["bjp_votes"],
                "tmc_votes": detail["tmc_votes"],
                "total_votes": detail["total_votes"],
                "detail_url": detail["detail_url"],
                "detail_error": detail["detail_error"],
            }
        )
        if detail["round_total"]:
            row["round_done"] = detail["round_done"]
            row["round_total"] = detail["round_total"]
            row["round_left"] = detail["round_left"]
        if row["bjp_votes"] or row["tmc_votes"]:
            if row["bjp_votes"] > row["tmc_votes"]:
                row["bjp_tmc_lead_party"] = "BJP"
            elif row["tmc_votes"] > row["bjp_votes"]:
                row["bjp_tmc_lead_party"] = "TMC"
            else:
                row["bjp_tmc_lead_party"] = "Tie"
        else:
            row["bjp_tmc_lead_party"] = ""
        row.update(probability_for(row))

    bjp_leads = sum(1 for row in rows if row["leading_party"] == BJP)
    tmc_leads = sum(1 for row in rows if row["leading_party"] == TMC)
    in_progress = sum(1 for row in rows if "progress" in row["status"].lower())
    errors = sum(1 for row in rows if row["detail_error"])

    value = {
        "source": ECI_BASE_URL,
        "state_code": STATE_CODE,
        "fetched_at_epoch": time.time(),
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "last_updated": last_updated_from(first_page),
        "poll_seconds": POLL_SECONDS,
        "page_refresh_seconds": PAGE_REFRESH_SECONDS,
        "known_constituencies": len(rows),
        "summary": {
            "bjp_leading_or_won": bjp_leads,
            "tmc_leading_or_won": tmc_leads,
            "in_progress": in_progress,
            "detail_errors": errors,
        },
        "rows": rows,
    }
    with cache_lock:
        state_cache["until"] = now + STATE_CACHE_SECONDS
        state_cache["value"] = value
    return value


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{page_refresh}">
  <title>West Bengal ECI Live Dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0d1117;
      color: #eef2f7;
    }}
    body {{ margin: 0; background: #0d1117; }}
    header {{
      position: sticky; top: 0; z-index: 5; background: rgba(13, 17, 23, 0.96);
      border-bottom: 1px solid #263244; padding: 16px 22px;
    }}
    h1 {{ margin: 0; font-size: 22px; letter-spacing: 0; }}
    .sub {{ color: #9fb0c6; font-size: 13px; margin-top: 5px; }}
    .toolbar {{
      display: grid; grid-template-columns: minmax(180px, 1fr) 190px 170px; gap: 10px;
      margin-top: 14px; align-items: center;
    }}
    input, select {{
      background: #151b24; border: 1px solid #2f3a4a; color: #eef2f7; border-radius: 6px;
      padding: 10px 12px; font-size: 14px; outline: none;
    }}
    main {{ padding: 18px 22px 28px; }}
    .cards {{ display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr)); gap: 10px; margin-bottom: 16px; }}
    .card {{ background: #141a23; border: 1px solid #263244; border-radius: 8px; padding: 14px; }}
    .label {{ color: #9fb0c6; font-size: 12px; }}
    .value {{ margin-top: 6px; font-size: 24px; font-weight: 750; }}
    .table-wrap {{ overflow: auto; border: 1px solid #263244; border-radius: 8px; }}
    table {{ border-collapse: collapse; width: 100%; min-width: 1120px; background: #111821; }}
    th, td {{ padding: 10px 11px; border-bottom: 1px solid #253042; text-align: left; font-size: 13px; white-space: nowrap; }}
    th {{ position: sticky; top: 115px; background: #17202b; z-index: 3; color: #c9d6e5; }}
    tr:hover td {{ background: #17202b; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .party {{ font-weight: 700; }}
    .bjp {{ color: #ffb36a; }}
    .tmc {{ color: #75d184; }}
    .other {{ color: #8fc7ff; }}
    .bar {{ width: 86px; height: 7px; background: #253042; border-radius: 999px; overflow: hidden; }}
    .bar span {{ display: block; height: 100%; background: #75d184; }}
    .error {{ color: #ff8f8f; }}
    .muted {{ color: #9fb0c6; }}
    @media (max-width: 850px) {{
      .toolbar {{ grid-template-columns: 1fr; }}
      .cards {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
      th {{ top: 190px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>West Bengal ECI Live Results</h1>
    <div class="sub">Browser polls every {poll}s. Page auto-refreshes every {page_refresh_minutes} min. Probability is a heuristic, not an ECI metric.</div>
    <div class="toolbar">
      <input id="search" placeholder="Search constituency">
      <select id="partyFilter">
        <option value="">All leading parties</option>
        <option value="Bharatiya Janata Party">BJP leading</option>
        <option value="All India Trinamool Congress">TMC leading</option>
        <option value="other">Other leading</option>
      </select>
      <select id="sortBy">
        <option value="constituency_no">Sort by AC number</option>
        <option value="margin">Sort by margin</option>
        <option value="lead_pct">Sort by % lead</option>
        <option value="probability">Sort by probability</option>
        <option value="round_left">Sort by rounds left</option>
      </select>
    </div>
  </header>
  <main>
    <section class="cards">
      <div class="card"><div class="label">Known Seats</div><div class="value" id="known">-</div></div>
      <div class="card"><div class="label">BJP Leading/Won</div><div class="value bjp" id="bjp">-</div></div>
      <div class="card"><div class="label">TMC Leading/Won</div><div class="value tmc" id="tmc">-</div></div>
      <div class="card"><div class="label">In Progress</div><div class="value" id="progress">-</div></div>
      <div class="card"><div class="label">ECI Updated</div><div class="value" id="updated" style="font-size:16px">-</div></div>
    </section>
    <div id="status" class="sub">Loading ECI data...</div>
    <div class="table-wrap" style="margin-top:10px">
      <table>
        <thead>
          <tr>
            <th>AC</th>
            <th>Constituency</th>
            <th class="num">BJP Votes</th>
            <th class="num">TMC Votes</th>
            <th>Party In Lead</th>
            <th class="num">Round Done</th>
            <th class="num">Round Left</th>
            <th class="num">% Lead</th>
            <th class="num">Prob. To Win</th>
            <th class="num">Margin</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </main>
  <script>
    const pollMs = {poll} * 1000;
    let data = [];

    const fmt = new Intl.NumberFormat('en-IN');
    const $ = (id) => document.getElementById(id);
    const partyClass = (party) => party.includes('Bharatiya Janata') ? 'bjp' : party.includes('Trinamool') ? 'tmc' : 'other';

    function render() {{
      const q = $('search').value.trim().toLowerCase();
      const party = $('partyFilter').value;
      const sortBy = $('sortBy').value;
      let rows = data.filter((row) => {{
        const matchesSearch = !q || row.constituency.toLowerCase().includes(q);
        const matchesParty = !party
          || row.leading_party === party
          || (party === 'other' && row.leading_party !== 'Bharatiya Janata Party' && row.leading_party !== 'All India Trinamool Congress');
        return matchesSearch && matchesParty;
      }});
      rows.sort((a, b) => {{
        if (sortBy === 'constituency_no') return a.constituency_no - b.constituency_no;
        return (b[sortBy] || 0) - (a[sortBy] || 0);
      }});
      $('rows').innerHTML = rows.map((row) => `
        <tr>
          <td class="num">${{row.constituency_no}}</td>
          <td><a href="${{row.detail_url}}" target="_blank" style="color:#dbe7f6;text-decoration:none">${{row.constituency}}</a></td>
          <td class="num bjp">${{fmt.format(row.bjp_votes || 0)}}</td>
          <td class="num tmc">${{fmt.format(row.tmc_votes || 0)}}</td>
          <td class="party ${{partyClass(row.leading_party)}}">${{row.leading_party || '-'}}</td>
          <td class="num">${{row.round_done}}/${{row.round_total}}</td>
          <td class="num">${{row.round_left}}</td>
          <td class="num">${{(row.lead_pct || 0).toFixed(2)}}%</td>
          <td class="num">
            <div style="display:flex;gap:8px;align-items:center;justify-content:flex-end">
              <div class="bar"><span style="width:${{row.probability || 0}}%"></span></div>
              ${{row.probability || 0}}%
            </div>
          </td>
          <td class="num">${{fmt.format(row.margin || 0)}}</td>
          <td class="${{row.detail_error ? 'error' : 'muted'}}">${{row.detail_error ? 'Detail fetch issue' : row.status}}</td>
        </tr>`).join('');
    }}

    async function load() {{
      const started = Date.now();
      try {{
        const response = await fetch('/api/results?ts=' + Date.now(), {{ cache: 'no-store' }});
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const payload = await response.json();
        data = payload.rows || [];
        $('known').textContent = fmt.format(payload.known_constituencies || 0);
        $('bjp').textContent = fmt.format(payload.summary?.bjp_leading_or_won || 0);
        $('tmc').textContent = fmt.format(payload.summary?.tmc_leading_or_won || 0);
        $('progress').textContent = fmt.format(payload.summary?.in_progress || 0);
        $('updated').textContent = payload.last_updated || payload.fetched_at || '-';
        const elapsed = ((Date.now() - started) / 1000).toFixed(1);
        $('status').textContent = `Fetched ${{data.length}} constituencies in ${{elapsed}}s from ECI source. Next poll in {poll}s.`;
        render();
      }} catch (error) {{
        $('status').innerHTML = `<span class="error">Could not load ECI data: ${{error.message}}</span>`;
      }}
    }}

    ['search', 'partyFilter', 'sortBy'].forEach((id) => $(id).addEventListener('input', render));
    load();
    setInterval(load, pollMs);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/results":
            self.send_json(load_results())
            return
        if path in {"/", "/index.html"}:
            self.send_html(
                HTML.format(
                    poll=POLL_SECONDS,
                    page_refresh=PAGE_REFRESH_SECONDS,
                    page_refresh_minutes=math.ceil(PAGE_REFRESH_SECONDS / 60),
                )
            )
            return
        self.send_error(404, "Not found")

    def send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), DashboardHandler)
    print(f"Live dashboard: http://127.0.0.1:{PORT}")
    print(f"ECI source: {ECI_BASE_URL}, state: {STATE_CODE}")
    print(f"Browser polling: {POLL_SECONDS}s, page refresh: {PAGE_REFRESH_SECONDS}s")
    server.serve_forever()


if __name__ == "__main__":
    main()
