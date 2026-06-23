from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING

from db.connection import get_db
from select_posts import has_title_and_body


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PipelineRepository:
    def __init__(self):
        self.db = get_db()
        self.runs = self.db.pipeline_runs
        self.group_runs = self.db.group_runs
        self.raw_posts = self.db.raw_posts
        self.selected_posts = self.db.selected_posts
        self.comments = self.db.comments
        self.job_runs = self.db.job_runs
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        self.runs.create_index([("run_key", ASCENDING)], unique=True)
        self.runs.create_index([("day_number", ASCENDING), ("started_at", DESCENDING)])

        self.group_runs.create_index(
            [("pipeline_run_id", ASCENDING), ("group_id", ASCENDING)], unique=True
        )

        self.raw_posts.create_index([("run_key", ASCENDING)])
        self.raw_posts.create_index([("day_number", ASCENDING), ("group_id", ASCENDING)])
        self.raw_posts.create_index(
            [("pipeline_run_id", ASCENDING), ("reddit_post_id", ASCENDING)], unique=True
        )

        self.selected_posts.create_index([("run_key", ASCENDING)])
        self.selected_posts.create_index([("day_number", ASCENDING), ("group_id", ASCENDING)])
        self.selected_posts.create_index(
            [("pipeline_run_id", ASCENDING), ("group_id", ASCENDING), ("rank", ASCENDING)],
            unique=True,
        )

        self.comments.create_index([("pipeline_run_id", ASCENDING)])
        self.comments.create_index([("run_key", ASCENDING), ("group_id", ASCENDING)])
        self.comments.create_index([("publish_status", ASCENDING)])

        self.job_runs.create_index([("calendar_date", ASCENDING)], unique=True)
        self.job_runs.create_index([("workflow_run_key", ASCENDING)])

    def get_job_run(self, calendar_date: str) -> dict | None:
        return self.job_runs.find_one({"calendar_date": calendar_date})

    def create_job_run(
        self,
        *,
        calendar_date: str,
        day_number: int,
        day_name: str,
    ) -> str:
        doc = {
            "calendar_date": calendar_date,
            "day_number": day_number,
            "day_name": day_name,
            "workflow_status": "pending",
            "workflow_run_key": None,
            "workflow_started_at": None,
            "workflow_finished_at": None,
            "workflow_duration_secs": None,
            "workflow_error": None,
            "workflow_groups": [],
            "telegram_slot_hour": None,
            "telegram_scheduled_at": None,
            "telegram_status": "pending",
            "telegram_started_at": None,
            "telegram_finished_at": None,
            "telegram_duration_secs": None,
            "telegram_error": None,
            "telegram_comments_sent": 0,
            "telegram_comments_failed": 0,
            "telegram_comments_published": 0,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        return str(self.job_runs.insert_one(doc).inserted_id)

    def ensure_job_run(self, calendar_date: str, day_number: int, day_name: str) -> dict:
        existing = self.get_job_run(calendar_date)
        if existing:
            return existing
        self.create_job_run(
            calendar_date=calendar_date,
            day_number=day_number,
            day_name=day_name,
        )
        return self.get_job_run(calendar_date)

    def update_job_run(self, calendar_date: str, **fields) -> None:
        fields["updated_at"] = utcnow()
        self.job_runs.update_one({"calendar_date": calendar_date}, {"$set": fields})

    def get_yesterday_telegram_slot(self, calendar_date: str) -> int | None:
        from datetime import date, timedelta

        yesterday = (date.fromisoformat(calendar_date) - timedelta(days=1)).isoformat()
        doc = self.get_job_run(yesterday)
        if not doc:
            return None
        if doc.get("telegram_status") not in {"sent", "partial"}:
            return None
        return doc.get("telegram_slot_hour")

    def count_published_comments_for_run(self, run_key: str) -> int:
        return self.comments.count_documents({
            "run_key": run_key,
            "publish_status": "published",
        })

    def create_run(
        self,
        *,
        run_key: str,
        day_number: int,
        day_name: str,
        top_n: int,
        group_ids: list[str],
        config_snapshot: dict,
    ) -> str:
        doc = {
            "run_key": run_key,
            "day_number": day_number,
            "day_name": day_name,
            "top_n": top_n,
            "group_ids": group_ids,
            "config_snapshot": config_snapshot,
            "status": "running",
            "phase": "starting",
            "started_at": utcnow(),
            "finished_at": None,
            "stats": {
                "groups_total": len(group_ids),
                "groups_completed": 0,
                "raw_posts": 0,
                "selected_posts": 0,
                "comments": 0,
            },
            "error": None,
        }
        return str(self.runs.insert_one(doc).inserted_id)

    def set_run_phase(self, pipeline_run_id: str, phase: str) -> None:
        self.runs.update_one({"_id": self._oid(pipeline_run_id)}, {"$set": {"phase": phase}})

    def finish_run(self, pipeline_run_id: str) -> None:
        run = self.runs.find_one({"_id": self._oid(pipeline_run_id)})
        self.runs.update_one(
            {"_id": self._oid(pipeline_run_id)},
            {
                "$set": {
                    "status": "completed",
                    "phase": "done",
                    "finished_at": utcnow(),
                    "stats": run.get("stats", {}) if run else {},
                }
            },
        )

    def fail_run(self, pipeline_run_id: str, error: str) -> None:
        self.runs.update_one(
            {"_id": self._oid(pipeline_run_id)},
            {"$set": {"status": "failed", "phase": "failed", "finished_at": utcnow(), "error": error}},
        )

    def get_run_by_key(self, run_key: str) -> dict | None:
        return self.runs.find_one({"run_key": run_key})

    def get_group_run(self, pipeline_run_id: str, group_id: str) -> dict | None:
        return self.group_runs.find_one({
            "pipeline_run_id": self._oid(pipeline_run_id),
            "group_id": group_id,
        })

    def get_selected_posts(self, pipeline_run_id: str, group_id: str) -> list[dict]:
        return list(
            self.selected_posts.find({
                "pipeline_run_id": self._oid(pipeline_run_id),
                "group_id": group_id,
            }).sort("rank", ASCENDING)
        )

    def has_comment(self, pipeline_run_id: str, reddit_post_id: str) -> bool:
        return self.comments.count_documents({
            "pipeline_run_id": self._oid(pipeline_run_id),
            "reddit_post_id": reddit_post_id,
        }, limit=1) > 0

    def get_comments_for_group(self, pipeline_run_id: str, group_id: str) -> list[dict]:
        return list(
            self.comments.find({
                "pipeline_run_id": self._oid(pipeline_run_id),
                "group_id": group_id,
            }).sort("created_at", ASCENDING)
        )

    def get_unsent_comments(self, pipeline_run_id: str, group_id: str) -> list[dict]:
        return list(
            self.comments.find({
                "pipeline_run_id": self._oid(pipeline_run_id),
                "group_id": group_id,
                "publish_status": {"$nin": ["sent", "posted", "published"]},
            }).sort("created_at", ASCENDING)
        )

    def get_comment_by_id(self, comment_id: str) -> dict | None:
        return self.comments.find_one({"_id": self._oid(comment_id)})

    def mark_comment_sent(
        self,
        comment_id: str,
        *,
        telegram_message_id: int | str,
        telegram_chat_id: str,
        reddit_username: str | None = None,
    ) -> None:
        fields = {
            "publish_status": "sent",
            "published_at": utcnow(),
            "telegram_message_id": telegram_message_id,
            "telegram_chat_id": telegram_chat_id,
            "publish_error": None,
        }
        if reddit_username:
            fields["reddit_username"] = reddit_username
        self.comments.update_one({"_id": self._oid(comment_id)}, {"$set": fields})

    def mark_comment_failed(self, comment_id: str, error: str) -> None:
        self.comments.update_one(
            {"_id": self._oid(comment_id)},
            {
                "$set": {
                    "publish_status": "failed",
                    "published_at": utcnow(),
                    "publish_error": error,
                }
            },
        )

    def mark_comment_published(self, comment_id: str) -> None:
        self.comments.update_one(
            {"_id": self._oid(comment_id)},
            {
                "$set": {
                    "publish_status": "published",
                    "published_at": utcnow(),
                    "publish_error": None,
                }
            },
        )

    def update_generated_comment(
        self,
        comment_id: str,
        generated_comment: str,
        *,
        latency_ms: int,
        llm_provider: str,
        llm_model: str,
    ) -> None:
        self.comments.update_one(
            {"_id": self._oid(comment_id)},
            {
                "$set": {
                    "generated_comment": generated_comment,
                    "latency_ms": latency_ms,
                    "llm_provider": llm_provider,
                    "llm_model": llm_model,
                    "regenerated_at": utcnow(),
                },
                "$inc": {"regenerate_count": 1},
            },
        )

    def increment_stat(self, pipeline_run_id: str, field: str, amount: int = 1) -> None:
        self.runs.update_one(
            {"_id": self._oid(pipeline_run_id)},
            {"$inc": {f"stats.{field}": amount}},
        )

    def create_group_run(
        self,
        pipeline_run_id: str,
        *,
        run_key: str,
        day_number: int,
        day_name: str,
        group_id: str,
        group_title: str,
        subreddit_names: list[str],
    ) -> str:
        doc = {
            "pipeline_run_id": self._oid(pipeline_run_id),
            "run_key": run_key,
            "day_number": day_number,
            "day_name": day_name,
            "group_id": group_id,
            "group_title": group_title,
            "subreddit_names": subreddit_names,
            "status": "running",
            "phase": "scraping",
            "started_at": utcnow(),
            "finished_at": None,
            "raw_posts_count": 0,
            "selected_posts_count": 0,
            "error": None,
        }
        return str(self.group_runs.insert_one(doc).inserted_id)

    def set_group_phase(self, group_run_id: str, phase: str) -> None:
        self.group_runs.update_one({"_id": self._oid(group_run_id)}, {"$set": {"phase": phase}})

    def finish_group_run(self, group_run_id: str, *, raw_count: int, selected_count: int) -> None:
        self.group_runs.update_one(
            {"_id": self._oid(group_run_id)},
            {
                "$set": {
                    "status": "completed",
                    "phase": "done",
                    "finished_at": utcnow(),
                    "raw_posts_count": raw_count,
                    "selected_posts_count": selected_count,
                }
            },
        )

    def fail_group_run(self, group_run_id: str, error: str) -> None:
        self.group_runs.update_one(
            {"_id": self._oid(group_run_id)},
            {"$set": {"status": "failed", "phase": "failed", "finished_at": utcnow(), "error": error}},
        )

    def save_raw_posts(
        self,
        *,
        pipeline_run_id: str,
        group_run_id: str,
        run_key: str,
        day_number: int,
        day_name: str,
        group_id: str,
        group_title: str,
        subreddit_name: str,
        items: list[dict],
        apify_run_id: str,
    ) -> int:
        now = utcnow()
        docs = []
        for item in items:
            if item.get("dataType") != "post":
                continue
            reddit_post_id = item.get("id") or item.get("parsedId")
            if not reddit_post_id:
                continue

            docs.append({
                "pipeline_run_id": self._oid(pipeline_run_id),
                "group_run_id": self._oid(group_run_id),
                "run_key": run_key,
                "day_number": day_number,
                "day_name": day_name,
                "group_id": group_id,
                "group_title": group_title,
                "subreddit_name": item.get("communityName") or subreddit_name,
                "reddit_post_id": reddit_post_id,
                "url": item.get("url"),
                "title": item.get("title"),
                "body": item.get("body"),
                "upvotes": item.get("upVotes"),
                "comment_count": item.get("numberOfComments"),
                "upvote_ratio": item.get("upVoteRatio"),
                "username": item.get("username"),
                "content_type": item.get("contentType"),
                "created_at_reddit": item.get("createdAt"),
                "apify_run_id": apify_run_id,
                "scraped_at": now,
                "raw": item,
            })

        if not docs:
            return 0

        inserted = 0
        for doc in docs:
            result = self.raw_posts.update_one(
                {
                    "pipeline_run_id": doc["pipeline_run_id"],
                    "reddit_post_id": doc["reddit_post_id"],
                },
                {"$set": doc},
                upsert=True,
            )
            if result.upserted_id or result.modified_count:
                inserted += 1

        return inserted

    def save_selected_posts(
        self,
        *,
        pipeline_run_id: str,
        group_run_id: str,
        run_key: str,
        day_number: int,
        day_name: str,
        group_id: str,
        group_title: str,
        top_n: int,
        selected: list[dict],
    ) -> int:
        # Clear previous selections for this group in this run
        self.selected_posts.delete_many({
            "pipeline_run_id": self._oid(pipeline_run_id),
            "group_id": group_id,
        })

        docs = []
        for rank, post in enumerate(selected, start=1):
            reddit_post_id = post.get("id") or post.get("parsedId")
            docs.append({
                "pipeline_run_id": self._oid(pipeline_run_id),
                "group_run_id": self._oid(group_run_id),
                "run_key": run_key,
                "day_number": day_number,
                "day_name": day_name,
                "group_id": group_id,
                "group_title": group_title,
                "top_n": top_n,
                "rank": rank,
                "reddit_post_id": reddit_post_id,
                "subreddit_name": post.get("communityName"),
                "url": post.get("url"),
                "title": post.get("title"),
                "body": post.get("body"),
                "upvotes": post.get("upVotes"),
                "comment_count": post.get("numberOfComments"),
                "selection_meta": {
                    "has_title_and_body": has_title_and_body(post),
                    "comment_count": post.get("numberOfComments") or 0,
                    "upvotes": post.get("upVotes") or 0,
                },
                "selected_at": utcnow(),
                "raw": post,
            })

        if not docs:
            return 0

        self.selected_posts.insert_many(docs)
        return len(docs)

    def insert_comment(
        self,
        *,
        pipeline_run_id: str,
        group_run_id: str,
        run_key: str,
        day_number: int,
        day_name: str,
        group_id: str,
        reddit_post_id: str,
        subreddit_name: str,
        post_url: str,
        post_title: str,
        prompt_input: dict,
        prompt_text: str,
        generated_comment: str,
        llm_provider: str,
        llm_model: str,
        latency_ms: int,
        upvotes,
        comment_count,
    ) -> str:
        doc = {
            "pipeline_run_id": self._oid(pipeline_run_id),
            "group_run_id": self._oid(group_run_id),
            "run_key": run_key,
            "day_number": day_number,
            "day_name": day_name,
            "group_id": group_id,
            "reddit_post_id": reddit_post_id,
            "subreddit_name": subreddit_name,
            "post_url": post_url,
            "post_title": post_title,
            "upvotes": upvotes,
            "comment_count": comment_count,
            "prompt_input": prompt_input,
            "prompt_text": prompt_text,
            "generated_comment": generated_comment,
            "llm_provider": llm_provider,
            "llm_model": llm_model,
            "latency_ms": latency_ms,
            "created_at": utcnow(),
            "publish_status": "pending",
            "published_at": None,
            "telegram_message_id": None,
            "telegram_chat_id": None,
            "publish_error": None,
        }
        return str(self.comments.insert_one(doc).inserted_id)

    @staticmethod
    def _oid(value: str):
        from bson import ObjectId

        return ObjectId(value)
