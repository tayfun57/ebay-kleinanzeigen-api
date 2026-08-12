import asyncio
import logging
from typing import List
from playwright.async_api import async_playwright, BrowserContext, Page
from utils.user_agent import get_random_ua
from utils.proxy_pool import ProxyPool

logger = logging.getLogger(__name__)

_BLOCKED_RESOURCE_TYPES = {"image", "script", "stylesheet", "font"}

# A context reused hundreds of times in a row has been observed to start
# silently returning empty result pages (HTTP 200, "success", zero listings)
# for searches that a brand-new context resolves correctly — context.clear_cookies()
# on release doesn't reset everything Kleinanzeigen can key a rate-limit/
# fingerprint off (e.g. localStorage). Retiring contexts well before that
# point trades a bit of context-creation overhead for not silently losing data.
MAX_CONTEXT_USES = 50


async def _block_heavy_resources(route):
    """Aborts image/script/stylesheet/font requests — cuts proxy bandwidth by
    ~80-86% (image+script) plus another ~62-74% on top (+stylesheet+font),
    measured. All scraped fields (price, details table, features, search
    results) are server-rendered HTML and unaffected by any of this — CSS/
    fonts are pure rendering, not needed by page.query_selector(); verified
    by diffing full extraction output with/without each blocked category.
    Only casualty: the JS-driven view counter (extra_info.views), which
    nothing in this app consumes."""
    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
        await route.abort()
    else:
        await route.continue_()


class PlaywrightManager:
    def __init__(self):
        self._playwright = None
        self._browser = None

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)

    async def new_context_page(self):
        context = await self._browser.new_context(user_agent=get_random_ua())
        return await context.new_page()

    async def close_page(self, page):
        await page.close()

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()


class OptimizedPlaywrightManager:
    def __init__(self, max_contexts: int = 10, max_concurrent: int = 5):
        self._playwright = None
        self._browser = None
        self._context_pool: List[BrowserContext] = []
        self._context_in_use: List[BrowserContext] = []
        self._max_contexts = max_contexts
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._context_lock = asyncio.Lock()
        self._proxy_pool = ProxyPool()
        if self._proxy_pool.enabled:
            logger.info(f"[PROXY] {len(self._proxy_pool)} proxy/proxies configured, rotating per context")

        # Performance metrics
        self._contexts_created = 0
        self._contexts_reused = 0
        self._contexts_retired = 0
        self._concurrent_operations = 0
        self._max_concurrent_reached = 0
        self._context_use_count: dict[int, int] = {}

    async def _create_context(self) -> BrowserContext:
        context = await self._browser.new_context(
            user_agent=get_random_ua(), proxy=self._proxy_pool.next()
        )
        await context.route("**/*", _block_heavy_resources)
        self._contexts_created += 1
        return context

    async def start(self):
        """Initialize the browser and create initial context pool"""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)

        # Pre-create some contexts for the pool
        initial_contexts = min(3, self._max_contexts)
        for _ in range(initial_contexts):
            context = await self._create_context()
            self._context_pool.append(context)

    async def get_context(self) -> BrowserContext:
        """Get a browser context from the pool or create a new one"""
        async with self._context_lock:
            if self._context_pool:
                context = self._context_pool.pop()
                self._context_in_use.append(context)
                self._contexts_reused += 1
                self._context_use_count[id(context)] = self._context_use_count.get(id(context), 0) + 1
                return context

            # Create new context if pool is empty and under limit
            if len(self._context_in_use) < self._max_contexts:
                context = await self._create_context()
                self._context_in_use.append(context)
                self._context_use_count[id(context)] = 1
                return context

            # If we're at the limit, wait and try again
            await asyncio.sleep(0.1)
            return await self.get_context()

    async def release_context(self, context: BrowserContext):
        """Return a context to the pool for reuse, or retire it once it's
        been used MAX_CONTEXT_USES times (see the constant's docstring)."""
        async with self._context_lock:
            if context in self._context_in_use:
                self._context_in_use.remove(context)

                # Close all pages and clear session state so reused contexts
                # don't carry Kleinanzeigen tracking cookies into the next request
                for page in context.pages:
                    await page.close()

                uses = self._context_use_count.get(id(context), 0)
                if uses >= MAX_CONTEXT_USES:
                    self._context_use_count.pop(id(context), None)
                    self._contexts_retired += 1
                    logger.info(f"[POOL] retiring context after {uses} uses")
                    await context.close()
                    return

                await context.clear_cookies()

                # Add back to pool if under limit, otherwise close it
                if len(self._context_pool) < self._max_contexts // 2:
                    self._context_pool.append(context)
                else:
                    self._context_use_count.pop(id(context), None)
                    await context.close()

    async def execute_with_semaphore(self, coro):
        """Execute a coroutine with concurrency control"""
        async with self._semaphore:
            self._concurrent_operations += 1
            self._max_concurrent_reached = max(
                self._max_concurrent_reached, self._concurrent_operations
            )
            try:
                result = await coro
                return result
            finally:
                self._concurrent_operations -= 1

    async def new_context_page(self) -> Page:
        """Create a new page using context pooling (backward compatibility)"""
        context = await self.get_context()
        page = await context.new_page()
        # Store context reference on page for cleanup
        page._context_ref = context
        return page

    async def close_page(self, page: Page):
        """Close a page and return its context to the pool"""
        context = getattr(page, "_context_ref", None)
        await page.close()
        if context:
            await self.release_context(context)

    def get_performance_metrics(self) -> dict:
        """Get current performance metrics"""
        return {
            "contexts_created": self._contexts_created,
            "contexts_reused": self._contexts_reused,
            "contexts_retired": self._contexts_retired,
            "contexts_in_pool": len(self._context_pool),
            "contexts_in_use": len(self._context_in_use),
            "max_contexts": self._max_contexts,
            "max_concurrent_reached": self._max_concurrent_reached,
            "current_concurrent": self._concurrent_operations,
            "reuse_ratio": self._contexts_reused / max(self._contexts_created, 1),
        }

    async def close(self):
        """Clean up all resources"""
        # Close all contexts in pool
        for context in self._context_pool:
            await context.close()

        # Close all contexts in use
        for context in self._context_in_use:
            await context.close()

        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

        self._context_pool.clear()
        self._context_in_use.clear()
        self._context_use_count.clear()
