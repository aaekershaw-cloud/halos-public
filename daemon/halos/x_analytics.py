"""Lightweight X (Twitter) analytics for Beta Analytics."""

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    import tweepy
except ImportError:
    tweepy = None

logger = logging.getLogger(__name__)


def _load_env(path: str):
    if Path(path).exists():
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())


def _get_client() -> Optional["tweepy.Client"]:
    if tweepy is None:
        logger.warning("tweepy not installed. Run: pip install tweepy")
        return None

    # Try standard credential locations
    for p in [
        ".env",
    ]:
        _load_env(p)

    required = ["X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.warning(f"Missing X credentials: {missing}")
        return None

    return tweepy.Client(
        consumer_key=os.environ["X_CONSUMER_KEY"],
        consumer_secret=os.environ["X_CONSUMER_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
        bearer_token=os.environ.get("X_BEARER_TOKEN"),
        wait_on_rate_limit=True,
    )


async def get_account_metrics(handle: str, days: int = 7) -> dict:
    """Pull recent tweets + follower count for an X handle."""
    client = _get_client()
    if not client:
        return {"error": "X API client not available"}

    try:
        user = client.get_user(username=handle.lstrip("@"), user_fields=["public_metrics"])
        if not user or not user.data:
            return {"error": f"User @{handle} not found"}

        user_id = user.data.id
        followers = user.data.public_metrics.get("followers_count", 0)
        following = user.data.public_metrics.get("following_count", 0)
        tweet_count = user.data.public_metrics.get("tweet_count", 0)

        # Pull tweets from last N days
        start_time = (datetime.utcnow() - timedelta(days=days)).isoformat("T") + "Z"
        tweets = tweepy.Paginator(
            client.get_users_tweets,
            id=user_id,
            tweet_fields=["created_at", "public_metrics", "text"],
            start_time=start_time,
            max_results=100,
        ).flatten(limit=100)

        tweet_summaries = []
        total_impressions = 0
        total_likes = 0
        total_retweets = 0
        total_replies = 0
        total_quotes = 0

        for t in tweets:
            m = t.public_metrics or {}
            impressions = m.get("impression_count", 0)  # may be 0 on free tier for non-own tweets
            likes = m.get("like_count", 0)
            retweets = m.get("retweet_count", 0)
            replies = m.get("reply_count", 0)
            quotes = m.get("quote_count", 0)

            total_impressions += impressions
            total_likes += likes
            total_retweets += retweets
            total_replies += replies
            total_quotes += quotes

            tweet_summaries.append({
                "id": t.id,
                "text": t.text[:120],
                "created_at": str(t.created_at),
                "likes": likes,
                "retweets": retweets,
                "replies": replies,
                "quotes": quotes,
                "impressions": impressions,
            })

        return {
            "handle": handle,
            "followers": followers,
            "following": following,
            "total_tweets": tweet_count,
            "period_days": days,
            "tweets_in_period": len(tweet_summaries),
            "aggregates": {
                "impressions": total_impressions,
                "likes": total_likes,
                "retweets": total_retweets,
                "replies": total_replies,
                "quotes": total_quotes,
                "engagement_score": total_likes + total_retweets + total_replies + total_quotes,
            },
            "recent_tweets": tweet_summaries[:10],  # top 10 for brevity
        }

    except Exception as e:
        logger.exception(f"X analytics failed for @{handle}")
        return {"error": str(e)}
