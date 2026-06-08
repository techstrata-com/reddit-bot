import time
from copy import deepcopy
from datetime import datetime, timezone

from apify_client import ApifyClient

ACTOR_ID = "oAuCIx3ItNrs2okjQ"
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}
POLL_INTERVAL_SECS = 15


def _run_value(run, attr: str):
    """Read a field from Apify Run (Pydantic model or dict)."""
    if isinstance(run, dict):
        camel = {
            "id": "id",
            "status": "status",
            "default_dataset_id": "defaultDatasetId",
        }
        return run.get(camel.get(attr, attr)) or run.get(attr)
    return getattr(run, attr)


def wait_for_run(client: ApifyClient, run_id: str, label: str = ""):
    """Poll Apify run status until terminal. Avoids long-poll TLS drops."""
    run_client = client.run(run_id)
    started = time.monotonic()
    prefix = f"    [{label}] " if label else "    "

    while True:
        try:
            run = run_client.get()
        except Exception as err:
            elapsed = int(time.monotonic() - started)
            print(f"{prefix}poll error ({type(err).__name__}), retrying... ({elapsed // 60}m {elapsed % 60}s)")
            time.sleep(POLL_INTERVAL_SECS)
            continue

        status = _run_value(run, "status")
        if status in TERMINAL_STATUSES:
            elapsed = int(time.monotonic() - started)
            print(f"{prefix}finished: {status} ({elapsed // 60}m {elapsed % 60}s)")
            return run

        elapsed = int(time.monotonic() - started)
        print(f"{prefix}status: {status} — {elapsed // 60}m {elapsed % 60}s elapsed...")
        time.sleep(POLL_INTERVAL_SECS)


def scrape_subreddit(
    client: ApifyClient,
    base_config: dict,
    url: str,
    *,
    subreddit_name: str = "",
) -> dict:
    started_at = datetime.now(timezone.utc)
    run_input = deepcopy(base_config)
    run_input["startUrls"] = [{"url": url}]

    label = subreddit_name or url
    print(f"    [{label}] starting scrape...")

    run = client.actor(ACTOR_ID).start(run_input=run_input)
    run_id = _run_value(run, "id")
    print(f"    [{label}] run: https://console.apify.com/actors/runs/{run_id}")

    run = wait_for_run(client, run_id, label=label)
    if _run_value(run, "status") != "SUCCEEDED":
        raise RuntimeError(f"Apify run failed with status: {_run_value(run, 'status')}")

    dataset_id = _run_value(run, "default_dataset_id")
    items = list(client.dataset(dataset_id).iterate_items(skip_hidden=False))
    finished_at = datetime.now(timezone.utc)

    print(f"    [{label}] got {len(items)} items")

    return {
        "apify_run_id": run_id,
        "apify_dataset_id": dataset_id,
        "items": items,
        "started_at": started_at,
        "finished_at": finished_at,
    }
