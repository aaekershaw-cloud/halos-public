"""Headless browser automation for agent research tasks."""

import asyncio
import random
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from datetime import datetime

try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

logger = logging.getLogger(__name__)


@dataclass
class ScrapedPost:
    """A scraped Reddit/HN/forum post."""
    platform: str
    title: str
    url: str
    score: Optional[int] = None
    comment_count: Optional[int] = None
    content: Optional[str] = None
    author: Optional[str] = None
    created_at: Optional[str] = None
    subreddit: Optional[str] = None
    scraped_at: datetime = None
    
    def __post_init__(self):
        if self.scraped_at is None:
            self.scraped_at = datetime.now()


class HeadlessBrowser:
    """Headless browser for agent research automation."""
    
    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self._screenshot_dir = Path.home() / ".halos" / "screenshots"
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
    
    async def start(self):
        """Initialize the browser."""
        if not HAS_PLAYWRIGHT:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")
        
        self.playwright = await async_playwright().start()
        
        # Stealth mode - avoid detection
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                '--headless=new',
                '--disable-blink-features=AutomationControlled',
                '--disable-web-security',
                '--disable-features=IsolateOrigins,site-per-process',
            ]
        )
        
        # Context with human-like settings
        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.0',
            locale='en-US',
            timezone_id='America/Edmonton',
        )
        
        # Add stealth script
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
        """)
        
        logger.info("Headless browser started")
    
    async def stop(self):
        """Clean up browser resources."""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Headless browser stopped")
    
    async def _rate_limit(self):
        """Human-like delay between requests."""
        delay = random.uniform(2, 5)
        await asyncio.sleep(delay)
    
    async def screenshot(self, page: Page, name: str) -> str:
        """Take a screenshot for evidence."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{name}_{timestamp}.png"
        filepath = self._screenshot_dir / filename
        await page.screenshot(path=str(filepath), full_page=True)
        return str(filepath)
    
    async def scrape_reddit_subreddit(
        self, 
        subreddit: str, 
        sort: str = "hot",
        limit: int = 25
    ) -> List[ScrapedPost]:
        """Scrape posts from a subreddit. Falls back to new.reddit if old.reddit is blocked."""
        if not self.context:
            raise RuntimeError("Browser not started. Call start() first.")
        
        posts = await self._scrape_old_reddit(subreddit, sort, limit)
        if not posts:
            logger.warning(f"old.reddit returned 0 posts for r/{subreddit}, trying new.reddit fallback")
            posts = await self._scrape_new_reddit(subreddit, sort, limit)
        return posts
    
    async def _scrape_old_reddit(self, subreddit: str, sort: str, limit: int) -> List[ScrapedPost]:
        page = await self.context.new_page()
        posts = []
        try:
            url = f"https://old.reddit.com/r/{subreddit}/{sort}/"
            logger.info(f"Scraping r/{subreddit} ({sort}) via old.reddit")
            await page.goto(url, wait_until='networkidle')
            await self._rate_limit()
            entries = await page.query_selector_all('.thing')
            for entry in entries[:limit]:
                try:
                    title_el = await entry.query_selector('a.title')
                    title = await title_el.inner_text() if title_el else ""
                    href = await title_el.get_attribute('href') if title_el else ""
                    if href and href.startswith('/'):
                        href = f"https://old.reddit.com{href}"
                    score_el = await entry.query_selector('.score.unvoted')
                    score_text = await score_el.inner_text() if score_el else "0"
                    score = self._parse_score(score_text)
                    comments_el = await entry.query_selector('.comments')
                    comments_text = await comments_el.inner_text() if comments_el else "0 comments"
                    comments = self._parse_comments(comments_text)
                    posts.append(ScrapedPost(
                        platform="reddit",
                        subreddit=subreddit,
                        title=title,
                        url=href,
                        score=score,
                        comment_count=comments,
                    ))
                except Exception as e:
                    logger.warning(f"Failed to parse old.reddit entry: {e}")
                    continue
            await self.screenshot(page, f"reddit_{subreddit}")
            logger.info(f"Scraped {len(posts)} posts from r/{subreddit} via old.reddit")
        finally:
            await page.close()
        return posts
    
    async def _scrape_new_reddit(self, subreddit: str, sort: str, limit: int) -> List[ScrapedPost]:
        page = await self.context.new_page()
        posts = []
        try:
            url = f"https://www.reddit.com/r/{subreddit}/{sort}/"
            logger.info(f"Scraping r/{subreddit} ({sort}) via new.reddit")
            await page.goto(url, wait_until='domcontentloaded')
            await self._rate_limit()
            # Wait for JS-rendered content
            await page.wait_for_timeout(5000)
            # New reddit uses faceplate-tracker elements in the feed
            entries = await page.query_selector_all('faceplate-tracker[data-testid="post-container"]')
            if not entries:
                # Fallback: try shreddit-post
                entries = await page.query_selector_all('shreddit-post')
            for entry in entries[:limit]:
                try:
                    # Try various selectors for title and permalink
                    title_el = await entry.query_selector('a[data-testid="post-title"]')
                    if not title_el:
                        title_el = await entry.query_selector('[slot="title"]')
                    title = await title_el.inner_text() if title_el else ""
                    href = await title_el.get_attribute('href') if title_el else ""
                    
                    # Fallback to permalink attribute on shreddit-post
                    if not href and await entry.get_attribute('permalink'):
                        href = await entry.get_attribute('permalink')
                    
                    if href and href.startswith('/r/'):
                        href = f"https://www.reddit.com{href}"
                    elif href and href.startswith('/'):
                        href = f"https://www.reddit.com{href}"
                    
                    # Score parsing: try attribute first, then DOM text
                    score = 0
                    score_attr = await entry.get_attribute('score')
                    if score_attr:
                        score = self._parse_score(score_attr)
                    if score == 0:
                        score_el = await entry.query_selector('[data-testid="upvote-button"] ~ div, [data-click-id="upvote"] + div')
                        if score_el:
                            score_text = await score_el.inner_text()
                            score = self._parse_score(score_text)
                    
                    # Comments parsing
                    comments = 0
                    comments_el = await entry.query_selector('[data-testid="comment-button"] span, a[href*="comments"] span')
                    if comments_el:
                        comments_text = await comments_el.inner_text()
                        comments = self._parse_comments(comments_text)
                    
                    posts.append(ScrapedPost(
                        platform="reddit",
                        subreddit=subreddit,
                        title=title,
                        url=href,
                        score=score,
                        comment_count=comments,
                    ))
                except Exception as e:
                    logger.warning(f"Failed to parse new.reddit entry: {e}")
                    continue
            await self.screenshot(page, f"reddit_{subreddit}_new")
            logger.info(f"Scraped {len(posts)} posts from r/{subreddit} via new.reddit")
        finally:
            await page.close()
        return posts
    
    async def scrape_hacker_news(self, limit: int = 30) -> List[ScrapedPost]:
        """Scrape front page of Hacker News."""
        if not self.context:
            raise RuntimeError("Browser not started. Call start() first.")
        
        page = await self.context.new_page()
        posts = []
        
        try:
            await page.goto("https://news.ycombinator.com/", wait_until='networkidle')
            await self._rate_limit()
            
            entries = await page.query_selector_all('.athing')
            
            for entry in entries[:limit]:
                try:
                    title_el = await entry.query_selector('.titleline > a')
                    title = await title_el.inner_text() if title_el else ""
                    url = await title_el.get_attribute('href') if title_el else ""
                    
                    # Get score from subtext
                    subtext = await entry.query_selector('~ .subtext')
                    score = 0
                    if subtext:
                        score_el = await subtext.query_selector('.score')
                        score_text = await score_el.inner_text() if score_el else "0"
                        score = self._parse_score(score_text)
                    
                    posts.append(ScrapedPost(
                        platform="hackernews",
                        title=title,
                        url=url,
                        score=score,
                    ))
                except Exception as e:
                    logger.warning(f"Failed to parse HN entry: {e}")
                    continue
            
            await self.screenshot(page, "hackernews")
            logger.info(f"Scraped {len(posts)} posts from HN")
            
        finally:
            await page.close()
        
        return posts
    
    async def search_google(self, query: str) -> List[Dict[str, str]]:
        """Perform a Google search and extract results."""
        if not self.context:
            raise RuntimeError("Browser not started. Call start() first.")
        
        page = await self.context.new_page()
        results = []
        
        try:
            # Use duckduckgo html version to avoid Google bot detection
            search_url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
            await page.goto(search_url, wait_until='networkidle')
            await self._rate_limit()
            
            entries = await page.query_selector_all('.result')
            
            for entry in entries[:10]:
                try:
                    title_el = await entry.query_selector('.result__title > a')
                    title = await title_el.inner_text() if title_el else ""
                    url = await title_el.get_attribute('href') if title_el else ""
                    
                    snippet_el = await entry.query_selector('.result__snippet')
                    snippet = await snippet_el.inner_text() if snippet_el else ""
                    
                    results.append({
                        'title': title,
                        'url': url,
                        'snippet': snippet,
                    })
                except Exception as e:
                    logger.warning(f"Failed to parse search result: {e}")
                    continue
            
            logger.info(f"Found {len(results)} search results for '{query}'")
            
        finally:
            await page.close()
        
        return results
    
    def _parse_score(self, text: str) -> int:
        """Parse score text like '42' or '1.2k' to integer."""
        try:
            text = text.strip().lower().replace('points', '').strip()
            if 'k' in text:
                return int(float(text.replace('k', '')) * 1000)
            return int(text)
        except:
            return 0
    
    def _parse_comments(self, text: str) -> int:
        """Parse comment count like '5 comments' to integer."""
        try:
            text = text.strip().lower().replace('comments', '').replace('comment', '').strip()
            return int(text)
        except:
            return 0


# Singleton instance for reuse
_browser_instance: Optional[HeadlessBrowser] = None


async def get_browser() -> HeadlessBrowser:
    """Get or create the singleton browser instance."""
    global _browser_instance
    if _browser_instance is None:
        _browser_instance = HeadlessBrowser()
        await _browser_instance.start()
    return _browser_instance


async def close_browser():
    """Close the singleton browser instance."""
    global _browser_instance
    if _browser_instance:
        await _browser_instance.stop()
        _browser_instance = None
