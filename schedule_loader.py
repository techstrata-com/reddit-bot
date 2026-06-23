import json
from dataclasses import dataclass
from pathlib import Path

# Mara schedule week: 1=Saturday … 7=Friday, 3=Monday (REST)
DAY_NUMBERS: dict[int, str] = {
    1: "Saturday",
    2: "Sunday",
    3: "Monday",
    4: "Tuesday",
    5: "Wednesday",
    6: "Thursday",
    7: "Friday",
}

SCHEDULE_PATH = Path(__file__).parent / "mara_schedule_structured_with_urls.json"


@dataclass(frozen=True)
class Subreddit:
    name: str
    url: str
    rules: list[dict]


@dataclass(frozen=True)
class Group:
    id: str
    title: str
    category: str
    subreddits: list[Subreddit]


def normalize_subreddit_name(name: str) -> str:
    name = name.strip()
    if not name.startswith("r/"):
        name = f"r/{name}"
    return name


def format_subreddit_rules(rules: list[dict]) -> str:
    if not rules:
        return "No subreddit rules available."

    lines = []
    for rule in rules:
        number = rule.get("number", "?")
        title = (rule.get("title") or "").strip()
        description = (rule.get("description") or "").strip()
        lines.append(f"{number}. {title}: {description}" if description else f"{number}. {title}")

    return "\n".join(lines)


def _load_schedule(schedule_path: Path) -> dict:
    with schedule_path.open(encoding="utf-8") as f:
        return json.load(f)


def build_groups(schedule_path: Path = SCHEDULE_PATH) -> dict[str, Group]:
    schedule = _load_schedule(schedule_path)
    groups: dict[str, Group] = {}

    for category in schedule.get("categories", []):
        for group_data in category.get("groups", []):
            subreddits = []
            for sub_data in group_data.get("subreddits", []):
                url = sub_data.get("url")
                if not url:
                    continue

                subreddits.append(
                    Subreddit(
                        name=normalize_subreddit_name(sub_data["name"]),
                        url=url,
                        rules=sub_data.get("rules", []),
                    )
                )

            groups[group_data["id"]] = Group(
                id=group_data["id"],
                title=group_data.get("title", group_data["id"]),
                category=category.get("name", ""),
                subreddits=subreddits,
            )

    return groups


def get_day_plan(schedule_path: Path, day_number: int) -> tuple[str, list[str]]:
    if day_number not in DAY_NUMBERS:
        valid = ", ".join(f"{n}={name}" for n, name in DAY_NUMBERS.items())
        raise ValueError(f"Invalid day {day_number}. Use: {valid}")

    schedule = _load_schedule(schedule_path)
    day_name = DAY_NUMBERS[day_number]
    plan = schedule.get("weekly_activity_plan", {}).get(day_name)

    if not plan:
        raise ValueError(f"No activity plan found for {day_name}.")

    return day_name, plan.get("groups", [])


def mara_day_number_from_weekday(weekday: int) -> int:
    """Map Python weekday (Mon=0 … Sun=6) to Mara day number (Sat=1 … Fri=7)."""
    return (weekday + 2) % 7 + 1


def get_groups_for_day(schedule_path: Path, day_number: int) -> tuple[str, list[Group]]:
    day_name, group_ids = get_day_plan(schedule_path, day_number)

    if not group_ids:
        return day_name, []

    all_groups = build_groups(schedule_path)
    return day_name, [all_groups[gid] for gid in group_ids if gid in all_groups]


def subreddit_context_for_post(post: dict, group: Group) -> tuple[str, list[dict]]:
    """Match a scraped post to its subreddit entry in the schedule file."""
    community = post.get("communityName") or post.get("parsedCommunityName") or ""
    post_name = normalize_subreddit_name(community) if community else ""

    for subreddit in group.subreddits:
        if subreddit.name == post_name:
            return subreddit.name, subreddit.rules

    return post_name or "unknown", []
