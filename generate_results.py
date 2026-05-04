import json
from pathlib import Path

import main


def run() -> None:
    payload = main.load_results()
    Path("results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "wrote results.json:",
        payload["known_constituencies"],
        "constituencies, ECI updated",
        payload.get("last_updated") or payload.get("fetched_at"),
    )


if __name__ == "__main__":
    run()
