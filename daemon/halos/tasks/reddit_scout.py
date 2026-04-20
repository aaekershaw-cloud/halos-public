"""Reddit scouting task for beta-research agent."""

import logging
from typing import Dict, Any, List
from datetime import datetime

from .base import BaseTask
from ..browser import get_browser, close_browser, ScrapedPost

logger = logging.getLogger(__name__)


class RedditScoutTask(BaseTask):
    """Scout Reddit subreddits for opportunities."""
    
    task_type = "reddit_scout"
    
    async def execute(self, payload: Dict[str, Any] = None) -> Dict[str, Any]:
        """Execute Reddit scouting.
        
        Payload:
            subreddits: List[str] - subreddits to scout
            min_score: int - minimum upvotes to consider
            min_comments: int - minimum comments to consider
            keywords: List[str] - keywords to look for in titles
        """
        payload = payload or {}
        subreddits = payload.get("subreddits", ["guitarlessons", "LearnGuitar"])
        min_score = payload.get("min_score", 30)
        min_comments = payload.get("min_comments", 5)
        keywords = [k.lower() for k in payload.get("keywords", [])]
        
        findings = []
        browser = None
        
        try:
            browser = await get_browser()
            
            for subreddit in subreddits:
                logger.info(f"Scouting r/{subreddit}")
                
                try:
                    posts = await browser.scrape_reddit_subreddit(
                        subreddit=subreddit,
                        sort="hot",
                        limit=25
                    )
                    
                    for post in posts:
                        # Filter by engagement
                        if post.score < min_score:
                            continue
                        if post.comment_count and post.comment_count < min_comments:
                            continue
                        
                        # Check for keywords if specified
                        if keywords:
                            title_lower = post.title.lower()
                            if not any(kw in title_lower for kw in keywords):
                                continue
                        
                        findings.append({
                            "subreddit": subreddit,
                            "title": post.title,
                            "url": post.url,
                            "score": post.score,
                            "comments": post.comment_count,
                            "scraped_at": post.scraped_at.isoformat(),
                        })
                        
                except Exception as e:
                    logger.error(f"Failed to scout r/{subreddit}: {e}")
                    continue
                
        finally:
            if browser:
                await close_browser()
        
        # Format findings for Beta
        if findings:
            result_lines = [
                f"Reddit Scout Results ({datetime.now().strftime('%Y-%m-%d')})",
                f"Subreddits checked: {', '.join(subreddits)}",
                f"Threshold: {min_score}+ upvotes, {min_comments}+ comments",
                "",
                f"Found {len(findings)} high-engagement posts:",
                "",
            ]
            
            for f in findings[:10]:  # Top 10
                result_lines.append(f"[{f['score']}↑] {f['title']}")
                result_lines.append(f"   r/{f['subreddit']} | {f['comments']} comments")
                result_lines.append(f"   {f['url']}")
                result_lines.append("")
            
            result = "\n".join(result_lines)
        else:
            result = f"No posts met threshold ({min_score}+ upvotes, {min_comments}+ comments) in {', '.join(subreddits)}"
        
        return {
            "success": True,
            "result": result,
            "findings_count": len(findings),
            "findings": findings,
        }
