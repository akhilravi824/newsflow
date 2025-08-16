#!/usr/bin/env python3
"""
News API Service - Monetized Version
A web API that provides access to news scraping with subscription tiers.
"""

import asyncio
import logging
import time
import random
import aiohttp
import requests
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple
from urllib.parse import quote_plus
import pandas as pd
from lxml import html
from dataclasses import dataclass
import json
import subprocess
import platform
import os
import hashlib
import sqlite3

# Flask imports
from flask import Flask, request, jsonify, render_template_string
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import jwt

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('api_service.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-super-secret-key-change-this-in-production'

# Rate limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)

# Database functions
def init_db():
    """Initialize the SQLite database for users."""
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (api_key TEXT PRIMARY KEY, plan TEXT, requests_today INTEGER, 
                  last_reset DATE, created_date DATE, email TEXT)''')
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

def create_user(plan='free', email=None):
    """Create a new user with the specified plan."""
    api_key = hashlib.md5(f"{datetime.now()}{plan}{email}".encode()).hexdigest()
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)''',
              (api_key, plan, 0, datetime.now().date(), datetime.now().date(), email))
    conn.commit()
    conn.close()
    logger.info(f"Created new user with plan: {plan}")
    return api_key

def get_user_limits(api_key):
    """Get user's current plan and usage limits."""
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT plan, requests_today, last_reset FROM users WHERE api_key = ?', (api_key,))
    result = c.fetchone()
    conn.close()
    
    if not result:
        return None
    
    plan, requests_today, last_reset = result
    
    # Reset daily count if it's a new day
    if last_reset != datetime.now().date():
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('UPDATE users SET requests_today = 0, last_reset = ? WHERE api_key = ?',
                  (datetime.now().date(), api_key))
        conn.commit()
        conn.close()
        requests_today = 0
    
    limits = {
        'free': 100,
        'starter': 1000,
        'pro': 10000,
        'enterprise': float('inf')
    }
    
    return {
        'plan': plan,
        'requests_today': requests_today,
        'daily_limit': limits.get(plan, 100)
    }

def increment_request_count(api_key):
    """Increment the user's daily request count."""
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('UPDATE users SET requests_today = requests_today + 1 WHERE api_key = ?', (api_key,))
    conn.commit()
    conn.close()

def get_user_stats(api_key):
    """Get comprehensive user statistics."""
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT plan, requests_today, last_reset, created_date FROM users WHERE api_key = ?', (api_key,))
    result = c.fetchone()
    conn.close()
    
    if not result:
        return None
    
    plan, requests_today, last_reset, created_date = result
    
    limits = {
        'free': 100,
        'starter': 1000,
        'pro': 10000,
        'enterprise': float('inf')
    }
    
    daily_limit = limits.get(plan, 100)
    
    return {
        'plan': plan,
        'requests_today': requests_today,
        'daily_limit': daily_limit,
        'requests_remaining': daily_limit - requests_today if daily_limit != float('inf') else float('inf'),
        'created_date': created_date,
        'last_reset': last_reset
    }


@dataclass
class ProxyConfig:
    """Configuration for proxy settings."""
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    protocol: str = 'http'
    country: Optional[str] = None
    last_used: Optional[datetime] = None
    success_count: int = 0
    fail_count: int = 0
    is_working: bool = True
    
    @property
    def url(self) -> str:
        """Get proxy URL string."""
        if self.username and self.password:
            return f"{self.protocol}://{self.username}:{self.password}@{self.host}:{self.port}"
        return f"{self.protocol}://{self.host}:{self.port}"


@dataclass
class NewsArticle:
    """Data class for news article information."""
    keyword: str
    title: str
    link: str
    description: str
    date: str


class ProxyManager:
    """Manages proxy rotation and validation."""
    
    def __init__(self, config: 'Config'):
        self.config = config
        self.proxies: List[ProxyConfig] = []
        self.current_proxy_index = 0
        self.proxy_file = Path('proxies.json')
        self.load_proxies()
        
    def load_proxies(self):
        """Load proxies from file or initialize with default sources."""
        if self.proxy_file.exists():
            try:
                with open(self.proxy_file, 'r') as f:
                    proxy_data = json.load(f)
                    self.proxies = [ProxyConfig(**p) for p in proxy_data]
                logger.info(f"Loaded {len(self.proxies)} proxies from file")
            except Exception as e:
                logger.error(f"Error loading proxies: {e}")
                self.proxies = []
        
        # If no proxies loaded, try to fetch some
        if not self.proxies:
            self.fetch_free_proxies()
    
    def save_proxies(self):
        """Save current proxy list to file."""
        try:
            proxy_data = []
            for proxy in self.proxies:
                proxy_dict = {
                    'host': proxy.host,
                    'port': proxy.port,
                    'username': proxy.username,
                    'password': proxy.password,
                    'protocol': proxy.protocol,
                    'country': proxy.country,
                    'success_count': proxy.success_count,
                    'fail_count': proxy.fail_count,
                    'is_working': proxy.is_working
                }
                proxy_data.append(proxy_dict)
            
            with open(self.proxy_file, 'w') as f:
                json.dump(proxy_data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving proxies: {e}")
    
    def fetch_free_proxies(self):
        """Fetch free proxies from various sources."""
        logger.info("Fetching free proxies...")
        
        # Source 1: FreeProxyList.net
        try:
            response = requests.get('https://free-proxy-list.net/', timeout=10)
            if response.status_code == 200:
                doc = html.fromstring(response.text)
                proxy_rows = doc.xpath('//table[@class="table table-striped table-bordered"]//tr')[1:]
                
                for row in proxy_rows[:20]:
                    try:
                        cells = row.xpath('.//td/text()')
                        if len(cells) >= 8:
                            ip = cells[0].strip()
                            port = int(cells[1].strip())
                            https = cells[6].strip() == 'yes'
                            country = cells[3].strip()
                            
                            protocol = 'https' if https else 'http'
                            
                            proxy = ProxyConfig(
                                host=ip,
                                port=port,
                                protocol=protocol,
                                country=country
                            )
                            self.proxies.append(proxy)
                    except Exception as e:
                        continue
                        
                logger.info(f"Added {len(self.proxies)} proxies from FreeProxyList.net")
        except Exception as e:
            logger.warning(f"Could not fetch from FreeProxyList.net: {e}")
        
        if self.proxies:
            self.save_proxies()
            logger.info(f"Total proxies loaded: {len(self.proxies)}")
        else:
            logger.warning("No free proxies could be loaded")
    
    def get_next_proxy(self) -> Optional[ProxyConfig]:
        """Get next working proxy using round-robin."""
        if not self.proxies:
            return None
        
        attempts = 0
        while attempts < len(self.proxies):
            self.current_proxy_index = (self.current_proxy_index + 1) % len(self.proxies)
            proxy = self.proxies[self.current_proxy_index]
            
            if proxy.is_working:
                proxy.last_used = datetime.now()
                return proxy
            
            attempts += 1
        
        return self.proxies[0] if self.proxies else None
    
    async def validate_all_proxies(self):
        """Validate all proxies in parallel."""
        if not self.proxies:
            return
            
        logger.info("Validating all proxies...")
        tasks = [self.validate_proxy(proxy) for proxy in self.proxies]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        working_count = sum(1 for r in results if r is True)
        logger.info(f"Proxy validation complete: {working_count}/{len(self.proxies)} working")
        
        # Remove non-working proxies
        self.proxies = [p for p in self.proxies if p.is_working]
        self.save_proxies()
    
    async def validate_proxy(self, proxy: ProxyConfig) -> bool:
        """Test if a proxy is working."""
        try:
            test_url = 'http://httpbin.org/ip'
            timeout = aiohttp.ClientTimeout(total=10)
            
            connector = None
            if proxy.protocol == 'socks5':
                try:
                    from aiohttp_socks import ProxyConnector
                    connector = ProxyConnector.from_url(proxy.url)
                except ImportError:
                    logger.warning("aiohttp_socks not installed, skipping SOCKS proxy")
                    return False
            else:
                connector = aiohttp.TCPConnector(ssl=False)
            
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout
            ) as session:
                async with session.get(test_url) as response:
                    if response.status == 200:
                        proxy.success_count += 1
                        proxy.is_working = True
                        return True
                    else:
                        proxy.fail_count += 1
                        return False
                        
        except Exception as e:
            proxy.fail_count += 1
            proxy.is_working = False
            return False


class Config:
    """Configuration class for scraper settings."""
    
    def __init__(self):
        self.CONCURRENCY = 3
        self.REQUEST_DELAY_MIN = 1
        self.REQUEST_DELAY_MAX = 3
        self.MAX_RETRIES = 3
        self.REQUEST_TIMEOUT = 30
        self.USE_PROXIES = True
        self.USE_TOR = False
        self.PROXY_ROTATION_INTERVAL = 10
        self.USER_AGENTS = [
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 11.5; rv:91.0) Gecko/20100101 Firefox/91.0',
            'Mozilla/5.0 (X11; Ubuntu; Linux i686; rv:91.0) Gecko/20100101 Firefox/91.0',
            'Mozilla/5.0 (X11; Linux x86_64; rv:86.0) Gecko/20100101 Firefox/86.0',
            'Mozilla/5.0 (Windows NT 10.0; WOW64; rv:77.0) Gecko/20100101 Firefox/77.0',
            'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:39.0) Gecko/20100101 Firefox/75.0'
        ]
        
        self.DEFAULT_HEADERS = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
    
    def get_random_user_agent(self) -> str:
        """Get a random user agent from the list."""
        return random.choice(self.USER_AGENTS)


class GoogleNewsScraper:
    """Main scraper class for Google News with proxy support."""
    
    def __init__(self, config: Config):
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None
        self.semaphore: Optional[asyncio.Semaphore] = None
        self.proxy_manager = ProxyManager(config)
        self.request_count = 0
        
    async def __aenter__(self):
        """Async context manager entry."""
        # Validate proxies
        if self.config.USE_PROXIES:
            await self.proxy_manager.validate_all_proxies()
        
        # Create session with proxy support
        connector = await self._create_connector()
        self.session = aiohttp.ClientSession(
            headers=self.config.DEFAULT_HEADERS,
            timeout=aiohttp.ClientTimeout(total=self.config.REQUEST_TIMEOUT),
            connector=connector
        )
        self.semaphore = asyncio.Semaphore(self.config.CONCURRENCY)
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.session:
            await self.session.close()
    
    async def _create_connector(self) -> aiohttp.TCPConnector:
        """Create HTTP connector with proxy support."""
        if not self.config.USE_PROXIES:
            return aiohttp.TCPConnector(ssl=False)
        
        # Get current proxy
        proxy = self.proxy_manager.get_next_proxy()
        if not proxy:
            logger.warning("No proxies available, using direct connection")
            return aiohttp.TCPConnector(ssl=False)
        
        if proxy.protocol == 'socks5':
            try:
                from aiohttp_socks import ProxyConnector
                return ProxyConnector.from_url(proxy.url)
            except ImportError:
                logger.warning("aiohttp_socks not installed, falling back to HTTP proxy")
                return aiohttp.TCPConnector(ssl=False)
        else:
            return aiohttp.TCPConnector(ssl=False)
    
    async def fetch_with_retry(self, url: str, retries: int = None) -> Optional[str]:
        """Fetch URL content with retry logic and proper error handling."""
        if retries is None:
            retries = self.config.MAX_RETRIES
            
        for attempt in range(retries):
            try:
                async with self.semaphore:
                    # Rotate proxy if needed
                    if self.config.USE_PROXIES and self.request_count % self.config.PROXY_ROTATION_INTERVAL == 0:
                        await self._rotate_proxy()
                    
                    headers = {'User-Agent': self.config.get_random_user_agent()}
                    
                    # Create connector for this specific request
                    connector = await self._create_connector()
                    
                    async with aiohttp.ClientSession(
                        headers=headers,
                        connector=connector,
                        timeout=aiohttp.ClientTimeout(total=self.config.REQUEST_TIMEOUT)
                    ) as temp_session:
                        async with temp_session.get(url) as response:
                            if response.status == 200:
                                self.request_count += 1
                                return await response.text()
                            elif response.status == 429:
                                logger.warning(f"Rate limited (429) for {url}, attempt {attempt + 1}/{retries}")
                                await self._handle_rate_limit()
                            elif response.status == 407:
                                logger.error(f"Proxy authentication error (407) for {url}")
                                return None
                            else:
                                logger.error(f"HTTP {response.status} for {url}")
                                self._log_error(url, response.status)
                                
            except aiohttp.ClientConnectorError as e:
                logger.error(f"Connection error for {url}: {e}")
            except aiohttp.ServerDisconnectedError as e:
                logger.error(f"Server disconnected for {url}: {e}")
            except asyncio.TimeoutError:
                logger.error(f"Timeout for {url}")
            except Exception as e:
                logger.error(f"Unexpected error for {url}: {e}")
            
            if attempt < retries - 1:
                delay = self.config.REQUEST_DELAY_MIN + (attempt * 2)
                logger.info(f"Retrying {url} in {delay} seconds...")
                await asyncio.sleep(delay)
        
        logger.error(f"Failed to fetch {url} after {retries} attempts")
        return None
    
    async def _rotate_proxy(self):
        """Rotate to next available proxy."""
        if not self.config.USE_PROXIES:
            return
            
        old_proxy = self.proxy_manager.get_next_proxy()
        new_proxy = self.proxy_manager.get_next_proxy()
        
        if old_proxy and new_proxy and old_proxy != new_proxy:
            logger.info(f"Rotating proxy: {old_proxy.host}:{old_proxy.port} -> {new_proxy.host}:{new_proxy.port}")
    
    async def _handle_rate_limit(self):
        """Handle rate limiting with exponential backoff."""
        delay = self.config.REQUEST_DELAY_MAX * 2
        logger.info(f"Rate limited, waiting {delay} seconds...")
        await asyncio.sleep(delay)
    
    def _log_error(self, url: str, status: int):
        """Log errors to file."""
        error_log = Path('error_logs.txt')
        with error_log.open('a') as f:
            f.write(f"{datetime.now().isoformat()};{url};{status}\n")
    
    async def parse_google_news(self, search_query: str, page: int = 0) -> List[NewsArticle]:
        """Parse Google News search results."""
        articles = []
        
        # Parse search query components
        query_parts = search_query.split(":")
        if len(query_parts) != 3:
            logger.error(f"Invalid search query format: {search_query}")
            return articles
            
        keyword, sort_by, time_range = query_parts
        
        # Build Google News URL
        base_url = "https://www.google.com/search"
        params = {
            'q': keyword,
            'hl': 'en-US',
            'tbs': f'sbd:{sort_by},qdr:{time_range}',
            'tbm': 'nws'
        }
        
        if page > 0:
            params['start'] = page
            
        url = f"{base_url}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
        logger.info(f"Fetching: {url}")
        
        # Fetch and parse content
        content = await self.fetch_with_retry(url)
        if not content:
            return articles
            
        try:
            doc = html.fromstring(content)
            
            # Handle Google consent form if present
            if doc.xpath('//input[@name="continue"]/@value'):
                content = await self._handle_consent_form(doc)
                if content:
                    doc = html.fromstring(content)
                else:
                    return articles
            
            # Extract news articles
            articles = self._extract_articles(doc, keyword)
            
            # Check for pagination
            if len(articles) > 5 and self._has_next_page(doc):
                next_page_articles = await self.parse_google_news(search_query, page + 10)
                articles.extend(next_page_articles)
                
        except Exception as e:
            logger.error(f"Error parsing content for {search_query}: {e}")
            
        return articles
    
    async def _handle_consent_form(self, doc: html.HtmlElement) -> Optional[str]:
        """Handle Google's consent form."""
        try:
            consent_data = {
                'gl': doc.xpath('//input[@name="gl"]/@value')[0],
                'm': doc.xpath('//input[@name="m"]/@value')[0],
                'pc': doc.xpath('//input[@name="pc"]/@value')[0],
                'continue': doc.xpath('//input[@name="continue"]/@value')[0],
                'ca': doc.xpath('//input[@name="ca"]/@value')[0],
                'x': doc.xpath('//input[@name="x"]/@value')[0],
                'v': doc.xpath('//input[@name="v"]/@value')[0],
                't': doc.xpath('//input[@name="t"]/@value')[0],
                'hl': doc.xpath('//input[@name="hl"]/@value')[0],
                'src': doc.xpath('//input[@name="src"]/@value')[0]
            }
            
            async with self.session.post('https://consent.google.com/s', data=consent_data) as response:
                if response.status == 200:
                    return await response.text()
                    
        except Exception as e:
            logger.error(f"Error handling consent form: {e}")
            
        return None
    
    def _extract_articles(self, doc: html.HtmlElement, keyword: str) -> List[NewsArticle]:
        """Extract news articles from parsed HTML."""
        articles = []
        
        # Find news article links
        article_links = doc.xpath('//div[@id="main"]//div[@id="rcnt"]//div[@id="search"]//a')
        
        for link in article_links:
            try:
                title = ''.join(link.xpath('.//div[@role="heading"]//text()')).strip()
                href = link.xpath('@href')[0]
                description = ''.join(link.xpath('.//div[@role="heading"]/following-sibling::div[1]/text()')).strip()
                date = ''.join(link.xpath('.//div[@role="heading"]/following-sibling::div[last()]//text()')).strip()
                
                if href and title:
                    article = NewsArticle(
                        keyword=keyword.replace(':', '_'),
                        title=title,
                        link=href,
                        description=description,
                        date=date
                    )
                    articles.append(article)
                    
            except Exception as e:
                logger.warning(f"Error extracting article data: {e}")
                continue
                
        return articles
    
    def _has_next_page(self, doc: html.HtmlElement) -> bool:
        """Check if there's a next page available."""
        return bool(doc.xpath('//span[contains(., "Next")]/text()'))


# Flask API Routes
@app.route('/')
def landing_page():
    """Landing page for the API service."""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>News API Service</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; background: #ffffff; color: #000000; }
            .container { max-width: 1200px; margin: 0 auto; }
            .header { text-align: center; margin-bottom: 30px; }
            .main-content { display: block; }
            .search-section { background: #ffffff; padding: 24px; border-radius: 12px; border: 1px solid #e5e5e5; }
            .pricing { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; }
            .plan { border: 1px solid #000; padding: 16px; text-align: center; border-radius: 8px; background: #fff; }
            .plan.featured { border-width: 2px; }
            .price { font-size: 2em; color: #000; }
            .cta { background: #000; color: #fff; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block; margin-top: 10px; border: 1px solid #000; }
            .form-group { margin-bottom: 16px; }
            .form-group label { display: block; margin-bottom: 6px; font-weight: bold; }
            .form-group input, .form-group select { width: 100%; padding: 10px; border: 1px solid #000; border-radius: 5px; font-size: 16px; background: #fff; color: #000; }
            .search-btn { background: #000; color: #fff; padding: 12px 30px; border: 1px solid #000; border-radius: 5px; font-size: 16px; cursor: pointer; width: 100%; }
            .search-btn:hover { background: #111; }
            .results { margin-top: 20px; }
            .loading { text-align: center; padding: 20px; color: #000; border: 1px solid #e5e5e5; border-radius: 8px; background: #fff; }
            .error { background: #f5f5f5; color: #000; padding: 15px; border-radius: 5px; margin-top: 20px; border-left: 4px solid #000; }
            .success { background: #f5f5f5; color: #000; padding: 15px; border-radius: 5px; margin-top: 20px; border-left: 4px solid #000; }
            .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
            .tab { padding: 10px 14px; text-align: center; background: #fff; border: 1px solid #000; cursor: pointer; border-radius: 6px; }
            .tab.active { background: #000; color: #fff; }
            .tab-content { display: none; }
            .tab-content.active { display: block; }
            .suggestions { position: relative; background: #fff; border: 1px solid #000; border-top: none; max-height: 180px; overflow-y: auto; border-radius: 0 0 6px 6px; }
            .suggestion-item { padding: 8px 10px; cursor: pointer; }
            .suggestion-item:hover { background: #f5f5f5; }
            .search-history { background: #fff; border: 1px solid #000; border-radius: 6px; padding: 12px; margin-bottom: 16px; }
            .history-items { display: flex; flex-wrap: wrap; gap: 8px; }
            .history-chip { background: #fff; border: 1px solid #000; padding: 6px 10px; border-radius: 16px; cursor: pointer; font-size: 13px; }
            .history-chip:hover { background: #f5f5f5; }
            #loadMoreSection { text-align: center; margin-top: 16px; }
            /* ESG heartbeat logo (removed) */
            .esg-logo { display: none; }
            /* Results table */
            .news-table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #000; border-radius: 8px; overflow: hidden; }
            .news-table th, .news-table td { border-bottom: 1px solid #000; padding: 12px; text-align: left; vertical-align: top; }
            .news-table th { background: #000; color: #fff; font-weight: 700; font-size: 0.9em; }
            .news-table tr:hover { background: #f5f5f5; }
            /* Loader flipboard */
            .flipboard { display: grid; grid-template-columns: 1fr; gap: 8px; margin-top: 14px; }
            .flap { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; letter-spacing: 0.5px; background: #fff; color: #000; border: 1px solid #000; border-radius: 8px; padding: 10px 12px; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>News API</h1>
                <p>Real-time news data via a clean API and web interface</p>
            </div>
            
            <div class="main-content">
                <div class="search-section">
                    <div class="tabs">
                        <button class="tab active" onclick="showTab('search')">Search</button>
                        <button class="tab" onclick="showTab('api')">API</button>
                        <button class="tab" onclick="showTab('pricing')">Pricing</button>
                    </div>

                    <div id="search-tab" class="tab-content active">
                        <h2>News Search</h2>
                        <div id="searchHistory" class="search-history" style="display: none;">
                            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
                                <h4 style="margin:0;">Recent Searches</h4>
                                <button id="clearHistoryBtn" type="button" class="search-btn" style="padding:6px 10px; font-size:12px;">Clear</button>
                            </div>
                            <div id="historyItems" class="history-items"></div>
                        </div>
                        <form id="newsForm">
                            <div class="form-group">
                                <label for="keyword">Keyword/Topic:</label>
                                <input type="text" id="keyword" name="keyword" placeholder="e.g., tesla, artificial intelligence, crypto" required autocomplete="off">
                                <div id="suggestions" class="suggestions" style="display: none;"></div>
                                <small>Enter the topic you want to search for news about</small>
                            </div>
                            
                            <div class="form-group">
                                <label for="sort">Sort By:</label>
                                <select id="sort" name="sort">
                                    <option value="1">Relevance (Most relevant first)</option>
                                    <option value="2">Date (Newest first)</option>
                                </select>
                                <small>Choose how to sort the news results</small>
                            </div>
                            
                            <div class="form-group">
                                <label for="time">Time Range:</label>
                                <select id="time" name="time">
                                    <option value="d">Last 24 hours</option>
                                    <option value="w">Last week</option>
                                    <option value="m">Last month</option>
                                    <option value="y">Last year</option>
                                </select>
                                <small>Select how far back to search for news</small>
                            </div>
                            
                            <div class="form-group">
                                <label for="plan">Plan:</label>
                                <select id="plan" name="plan">
                                    <option value="free">Free</option>
                                    <option value="starter">Starter</option>
                                    <option value="pro">Pro</option>
                                    <option value="enterprise">Enterprise</option>
                                </select>
                                <small>Select which plan to use for this request</small>
                            </div>

                            <div class="form-group">
                                <label for="language">Language:</label>
                                <select id="language" name="language">
                                    <option value="en">English</option>
                                    <option value="es">Spanish</option>
                                    <option value="fr">French</option>
                                    <option value="de">German</option>
                                </select>
                                <small>Select language for news results</small>
                            </div>

                            <div class="form-group">
                                <label for="region">Region:</label>
                                <select id="region" name="region">
                                    <option value="US">United States</option>
                                    <option value="GB">United Kingdom</option>
                                    <option value="CA">Canada</option>
                                    <option value="AU">Australia</option>
                                </select>
                                <small>Select region for news results</small>
                            </div>
                            
                            <button type="submit" class="search-btn">Search News</button>
                        </form>
                        
                        <div id="results" class="results"></div>
                        <div id="loadMoreSection" style="display: none;">
                            <button id="loadMoreBtn" class="search-btn" type="button">Load More Results</button>
                        </div>
                    </div>
                    
                    <div id="api-tab" class="tab-content">
                        <h2>API Access</h2>
                        <p>For developers and automated access, use our API endpoints:</p>
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0;">
                            <h4>Get News:</h4>
                            <code>GET /api/v1/news/{keyword}?sort={sort}&time={time}</code>
                            <br><br>
                            <h4>Headers:</h4>
                            <code>X-API-Key: your_api_key_here</code>
                        </div>
                        <p><a href="/docs" class="cta">View Full API Documentation</a></p>
                    </div>
                    <div id="pricing-tab" class="tab-content">
                        <h3>Pricing Plans</h3>
                        <div class="pricing">
                        <div class="plan">
                            <h4>Free</h4>
                            <div class="price">$0</div>
                            <p>100 requests/day</p>
                            <p>Basic data</p>
                            <a href="/api/v1/register?plan=free" class="cta">Get Started</a>
                        </div>
                        
                        <div class="plan featured">
                            <h4>Starter</h4>
                            <div class="price">$29</div>
                            <p>1,000 requests/day</p>
                            <p>Full data + support</p>
                            <a href="/api/v1/register?plan=starter" class="cta">Choose Plan</a>
                        </div>
                        
                        <div class="plan">
                            <h4>Pro</h4>
                            <div class="price">$99</div>
                            <p>10,000 requests/day</p>
                            <p>Priority support</p>
                            <a href="/api/v1/register?plan=pro" class="cta">Choose Plan</a>
                        </div>
                        
                        <div class="plan">
                            <h4>Enterprise</h4>
                            <div class="price">$299</div>
                            <p>Unlimited requests</p>
                            <p>Custom features</p>
                            <a href="/api/v1/register?plan=enterprise" class="cta">Contact Sales</a>
                        </div>
                        </div>
                        <div style="text-align: center; margin-top: 20px;">
                            <h4>Resources</h4>
                            <p><a href="/docs">API Documentation</a></p>
                            <p><a href="/api/v1/register">Register for API Key</a></p>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <script>
            function showTab(tabName) {
                const tabContents = document.querySelectorAll('.tab-content');
                tabContents.forEach(content => content.classList.remove('active'));
                const tabs = document.querySelectorAll('.tab');
                tabs.forEach(tab => tab.classList.remove('active'));
                const target = document.getElementById(tabName + '-tab');
                if (target) target.classList.add('active');
                if (event && event.target) event.target.classList.add('active');
            }
            
            document.getElementById('newsForm').addEventListener('submit', async function(e) {
                e.preventDefault();
                
                const keyword = document.getElementById('keyword').value;
                const sort = document.getElementById('sort').value;
                const time = document.getElementById('time').value;
                 const plan = document.getElementById('plan').value;
                const resultsDiv = document.getElementById('results');
                const suggestionsDiv = document.getElementById('suggestions');
                const historyContainer = document.getElementById('searchHistory');
                const historyItems = document.getElementById('historyItems');
                let currentPage = 0;
                
                // Futuristic flipboard loading sequence
                resultsDiv.innerHTML = `
                    <div class="loading">
                        <div style="font-weight:600;margin-bottom:8px;">Fetching results...</div>
                        <div class="flipboard" id="flipboard">
                            <div class="flap">Parsing feeds ███▒▒▒▒▒▒</div>
                            <div class="flap">Optimizing routes █████▒▒▒</div>
                            <div class="flap">Rotating identities ███████▒</div>
                            <div class="flap">Fetching news ████████</div>
                            <div class="flap">Extracting articles ████████</div>
                        </div>
                    </div>
                `;
                // Animate flipboard text
                (function(){
                    const steps = [
                        'Parsing feeds ███▒▒▒▒▒▒',
                        'Optimizing routes █████▒▒▒',
                        'Rotating identities ███████▒',
                        'Fetching news ████████',
                        'Extracting articles ████████'
                    ];
                    const rows = Array.from(document.querySelectorAll('#flipboard .flap'));
                    let tick = 0; const timer = setInterval(() => {
                        rows.forEach((row, idx) => {
                            const phase = (tick + idx) % steps.length;
                            row.textContent = steps[phase];
                        });
                        tick++;
                    }, 320);
                    // Stop after 12 iterations or when results replace loader
                    setTimeout(() => clearInterval(timer), 4000);
                })();
                
                try {
                    // First try to get a free API key
                    const registerResponse = await fetch(`/api/v1/register?plan=${encodeURIComponent(plan)}`);
                    const registerData = await registerResponse.text();
                    
                    // Extract API key from the response (simple parsing)
                    const apiKeyMatch = registerData.match(/<div class="api-key">([a-f0-9]{32})<\/div>/);
                    if (!apiKeyMatch) {
                        throw new Error('Could not get API key');
                    }
                    
                    const apiKey = apiKeyMatch[1];
                    
                    // Make the news API call
                    const response = await fetch(`/api/v1/news/${keyword}?sort=${sort}&time=${time}`, {
                        headers: {
                            'X-API-Key': apiKey
                        }
                    });
                    
                    if (!response.ok) {
                        throw new Error(`API Error: ${response.status}`);
                    }
                    
                    const data = await response.json();
                    
                    // Display results in table format
                    if (data.data && data.data.length > 0) {
                        let html = `<div class="success">Found ${data.total_results} articles for "${keyword}"</div>`;
                        html += `<table class="news-table"><thead><tr><th>Title</th><th>Description</th><th>Date</th></tr></thead><tbody>`;
                        data.data.forEach(article => {
                            html += `
                                <tr>
                                    <td><a href="${article.link}" target="_blank">${article.title}</a></td>
                                    <td>${article.description || 'No description available'}</td>
                                    <td>${article.date}</td>
                                </tr>
                            `;
                        });
                        html += `</tbody></table>`;
                        resultsDiv.innerHTML = html;
                        // Enable Load More if we have at least 10 results
                        document.getElementById('loadMoreSection').style.display = 'block';
                        currentPage = 0;
                        // Save to history
                        saveSearchHistory(keyword);
                        renderHistory();
                        // Hide suggestions
                        suggestionsDiv.style.display = 'none';
                    } else {
                        resultsDiv.innerHTML = '<div class="error">❌ No news found for this keyword. Try a different search term.</div>';
                        document.getElementById('loadMoreSection').style.display = 'none';
                    }
                    
                } catch (error) {
                    resultsDiv.innerHTML = `<div class="error">❌ Error: ${error.message}</div>`;
                }
            });

            // Load more results
            document.getElementById('loadMoreBtn').addEventListener('click', async function() {
                const keyword = document.getElementById('keyword').value;
                const sort = document.getElementById('sort').value;
                const time = document.getElementById('time').value;
                const plan = document.getElementById('plan').value;
                const resultsDiv = document.getElementById('results');

                try {
                    const registerResponse = await fetch(`/api/v1/register?plan=${encodeURIComponent(plan)}`);
                    const registerData = await registerResponse.text();
                    const apiKeyMatch = registerData.match(/<div class=\"api-key\">([a-f0-9]{32})<\/div>/);
                    if (!apiKeyMatch) { throw new Error('Could not get API key'); }
                    const apiKey = apiKeyMatch[1];

                    const nextPage = (window.__currentPage__ || 0) + 1;
                    const response = await fetch(`/api/v1/news/${keyword}?sort=${sort}&time=${time}&page=${nextPage}`, {
                        headers: { 'X-API-Key': apiKey }
                    });
                    if (!response.ok) { throw new Error(`API Error: ${response.status}`); }
                    const data = await response.json();

                    if (data.data && data.data.length > 0) {
                        let html = '';
                        data.data.forEach(article => {
                            html += `
                                <div class=\"news-item\">
                                    <div class=\"news-title\">
                                        <a href=\"${article.link}\" target=\"_blank\">${article.title}</a>
                                    </div>
                                    ${article.description ? `<div class=\\"news-description\\">${article.description}</div>` : ''}
                                    <div class=\"news-meta\">📅 ${article.date}</div>
                                </div>
                            `;
                        });
                        resultsDiv.insertAdjacentHTML('beforeend', html);
                        window.__currentPage__ = nextPage;
                    } else {
                        document.getElementById('loadMoreSection').style.display = 'none';
                    }
                } catch (err) {
                    console.error(err);
                    document.getElementById('loadMoreSection').style.display = 'none';
                }
            });

            // Search history helpers
            function getHistory() {
                try { return JSON.parse(localStorage.getItem('gn_history') || '[]'); } catch { return []; }
            }
            function saveSearchHistory(keyword) {
                const history = getHistory();
                const filtered = history.filter(item => item !== keyword);
                filtered.unshift(keyword);
                localStorage.setItem('gn_history', JSON.stringify(filtered.slice(0, 8)));
            }
            function renderHistory() {
                const container = document.getElementById('searchHistory');
                const items = document.getElementById('historyItems');
                const history = getHistory();
                if (history.length === 0) { container.style.display = 'none'; return; }
                container.style.display = 'block';
                items.innerHTML = history.map(k => `<span class=\"history-chip\" data-k=\"${k}\">${k}</span>`).join('');
                items.querySelectorAll('.history-chip').forEach(el => {
                    el.addEventListener('click', () => {
                        document.getElementById('keyword').value = el.getAttribute('data-k');
                    });
                });
                document.getElementById('clearHistoryBtn').onclick = () => {
                    localStorage.removeItem('gn_history');
                    renderHistory();
                };
            }
            renderHistory();

            // Simple suggestions based on history
            document.getElementById('keyword').addEventListener('input', function(e) {
                const q = e.target.value.trim().toLowerCase();
                const suggestionBox = document.getElementById('suggestions');
                if (q.length < 2) { suggestionBox.style.display = 'none'; suggestionBox.innerHTML=''; return; }
                const history = getHistory();
                const matches = history.filter(h => h.toLowerCase().includes(q));
                if (matches.length === 0) { suggestionBox.style.display = 'none'; suggestionBox.innerHTML=''; return; }
                suggestionBox.innerHTML = matches.map(m => `<div class=\"suggestion-item\" data-k=\"${m}\">${m}</div>`).join('');
                suggestionBox.style.display = 'block';
                suggestionBox.querySelectorAll('.suggestion-item').forEach(el => {
                    el.addEventListener('click', () => {
                        document.getElementById('keyword').value = el.getAttribute('data-k');
                        suggestionBox.style.display = 'none';
                    });
                });
            });
        </script>
    </body>
    </html>
    """
    return html_content

@app.route('/docs')
def documentation():
    """API documentation page."""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>News API Documentation</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }
            .endpoint { background: #f8f9fa; padding: 20px; margin: 20px 0; border-radius: 8px; }
            code { background: #e9ecef; padding: 2px 6px; border-radius: 4px; }
            .method { background: #007bff; color: white; padding: 4px 8px; border-radius: 4px; font-size: 0.8em; }
        </style>
    </head>
    <body>
        <h1>News API Documentation</h1>
        
        <h2>Authentication</h2>
        <p>Include your API key in the <code>X-API-Key</code> header:</p>
        <div class="endpoint">
            <span class="method">GET</span> <code>/api/v1/news/{keyword}</code>
            <p><strong>Headers:</strong> <code>X-API-Key: your_api_key_here</code></p>
        </div>
        
        <h2>Endpoints</h2>
        
        <div class="endpoint">
            <h3>Get News</h3>
            <span class="method">GET</span> <code>/api/v1/news/{keyword}</code>
            <p><strong>Parameters:</strong></p>
            <ul>
                <li><code>sort</code> - Sort order (1=relevance, 2=date)</li>
                <li><code>time</code> - Time range (d=day, w=week, m=month, y=year)</li>
                <li><code>page</code> - Page number for pagination</li>
            </ul>
            <p><strong>Example:</strong></p>
            <code>GET /api/v1/news/tesla?sort=1&time=d&page=0</code>
        </div>
        
        <div class="endpoint">
            <h3>User Statistics</h3>
            <span class="method">GET</span> <code>/api/v1/stats</code>
            <p>Get your current usage and plan information.</p>
        </div>
        
        <div class="endpoint">
            <h3>Register</h3>
            <span class="method">POST</span> <code>/api/v1/register</code>
            <p>Register for a new API key.</p>
            <p><strong>Body:</strong> <code>{"plan": "starter", "email": "user@example.com"}</code></p>
        </div>
        
        <h2>Response Format</h2>
        <pre><code>{
  "data": [
    {
      "keyword": "tesla",
      "title": "Tesla Announces New Model",
      "link": "https://example.com/news",
      "description": "Tesla has announced...",
      "date": "2 hours ago"
    }
  ],
  "total_results": 1,
  "plan": "starter",
  "requests_remaining": 999
}</code></pre>
        
        <h2>Rate Limits</h2>
        <ul>
            <li><strong>Free:</strong> 100 requests/day</li>
            <li><strong>Starter:</strong> 1,000 requests/day</li>
            <li><strong>Pro:</strong> 10,000 requests/day</li>
            <li><strong>Enterprise:</strong> Unlimited</li>
        </ul>
        
        <p><a href="/">← Back to Home</a></p>
    </body>
    </html>
    """
    return html_content

@app.route('/api/v1/news/<keyword>')
@limiter.limit("100 per minute")
def get_news(keyword):
    """Main news API endpoint."""
    # Get API key from header
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required', 'message': 'Include X-API-Key header'}), 401
    
    # Check user limits
    user_limits = get_user_limits(api_key)
    if not user_limits:
        return jsonify({'error': 'Invalid API key', 'message': 'Please check your API key'}), 401
    
    if user_limits['requests_today'] >= user_limits['daily_limit']:
        return jsonify({
            'error': 'Daily limit exceeded', 
            'plan': user_limits['plan'],
            'message': 'Upgrade your plan for more requests'
        }), 429
    
    # Parse query parameters
    sort_by = request.args.get('sort', '1')
    time_range = request.args.get('time', 'd')
    page = int(request.args.get('page', 0))
    
    # Build search query
    search_query = f"{keyword}:{sort_by}:{time_range}"
    
    try:
        # Run the scraper asynchronously
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Initialize scraper
        config = Config()
        
        async def run_scraper():
            async with GoogleNewsScraper(config) as scraper:
                return await scraper.parse_google_news(search_query, page)
        
        results = loop.run_until_complete(run_scraper())
        loop.close()
        
        # Increment request count
        increment_request_count(api_key)
        
        # Return results based on plan
        if user_limits['plan'] == 'free':
            # Free tier: limited data
            response_data = []
            for article in results[:10]:  # Only first 10 articles
                response_data.append({
                    'title': article.title,
                    'link': article.link,
                    'date': article.date
                })
        else:
            # Paid tiers: full data
            response_data = []
            for article in results:
                response_data.append({
                    'keyword': article.keyword,
                    'title': article.title,
                    'link': article.link,
                    'description': article.description,
                    'date': article.date
                })
        
        return jsonify({
            'data': response_data,
            'total_results': len(results),
            'plan': user_limits['plan'],
            'requests_remaining': user_limits['daily_limit'] - user_limits['requests_today'] - 1,
            'message': 'Success'
        })
        
    except Exception as e:
        logger.error(f"Error in API endpoint: {e}")
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500

@app.route('/api/v1/register', methods=['GET', 'POST'])
def register():
    """User registration endpoint."""
    if request.method == 'GET':
        # Handle GET request (from pricing page links)
        plan = request.args.get('plan', 'free')
        email = request.args.get('email', '')
        
        # Create user
        api_key = create_user(plan, email)
        
        # Return success page
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Registration Successful</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; text-align: center; }}
                .success {{ background: #d4edda; border: 1px solid #c3e6cb; padding: 20px; border-radius: 8px; }}
                .api-key {{ background: #f8f9fa; padding: 15px; margin: 20px 0; border-radius: 5px; font-family: monospace; }}
            </style>
        </head>
        <body>
            <div class="success">
                <h1>🎉 Registration Successful!</h1>
                <p>Your API key has been created successfully.</p>
                <p><strong>Plan:</strong> {plan.title()}</p>
                <p><strong>API Key:</strong></p>
                <div class="api-key">{api_key}</div>
                <p><strong>Keep this key safe!</strong> You'll need it to make API requests.</p>
                <p><a href="/docs">View API Documentation</a></p>
                <p><a href="/">← Back to Home</a></p>
            </div>
        </body>
        </html>
        """
        return html_content
    
    else:
        # Handle POST request
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        plan = data.get('plan', 'free')
        email = data.get('email')
        
        if plan not in ['free', 'starter', 'pro', 'enterprise']:
            return jsonify({'error': 'Invalid plan'}), 400
        
        api_key = create_user(plan, email)
        
        return jsonify({
            'api_key': api_key,
            'plan': plan,
            'message': 'User created successfully'
        })

@app.route('/api/v1/stats')
def get_stats():
    """Get user statistics."""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user_stats = get_user_stats(api_key)
    if not user_stats:
        return jsonify({'error': 'Invalid API key'}), 401
    
    return jsonify(user_stats)

@app.route('/health')
def health_check():
    """Health check endpoint for monitoring."""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'service': 'News API'
    })

@app.route('/test')
def test_page():
    """Simple test page to verify the service is working."""
    return jsonify({
        'message': 'News API Service is running!',
        'endpoints': {
            'homepage': '/',
            'documentation': '/docs',
            'health': '/health',
            'register': '/api/v1/register',
            'news_api': '/api/v1/news/{keyword}'
        },
        'timestamp': datetime.now().isoformat()
    })

# Initialize database when app starts
def setup():
    init_db()

# Initialize database immediately
setup()

if __name__ == '__main__':
    print("Starting News API Service...")
    print("📖 Documentation: http://localhost:4000/docs")
    print("🏠 Homepage: http://localhost:4000")
    print("🔑 Register: http://localhost:4000/api/v1/register")
    print("\n💰 Start making money with your API!")
    
    app.run(debug=True, host='0.0.0.0', port=4000) 