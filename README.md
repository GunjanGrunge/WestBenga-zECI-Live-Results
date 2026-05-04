# West Bengal ECI Live Results

Live West Bengal ECI results dashboard for the May 2026 Assembly election.

## GitHub Pages

The hosted static dashboard is served from `index.html` and fetches official ECI result pages directly in the browser.

## Local Python Server

```bash
python3 main.py
```

Then open `http://127.0.0.1:8000`.

## Notes

- The dashboard polls every 10 seconds and the page auto-refreshes every 10 minutes.
- Win probability is a local heuristic from margin, vote totals, and counting progress. It is not an ECI-provided value.
- Official ECI source: `https://results.eci.gov.in/ResultAcGenMay2026`
