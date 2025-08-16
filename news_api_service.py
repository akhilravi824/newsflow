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
    <html lang="en">
    <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>NewsFlow Analytics Hub</title>
        <style>
            :root { --primary:#667eea; --primary2:#764ba2; --bg:#0e1330; --card:#10163a; --muted:#9aa4b2; }
            * { box-sizing: border-box; }
            body { margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; background: radial-gradient(1200px 600px at 30% -20%, rgba(118,75,162,0.25), transparent), radial-gradient(1000px 500px at 90% 0%, rgba(102,126,234,0.25), transparent), var(--bg); color: #e8edf3; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
            .shell { max-width: 600px; width: 100%; padding: 24px; }
            header { display:flex; align-items:center; justify-content:center; gap:12px; padding: 8px 0 18px; }
            .brand { display:none; }
            nav { display:flex; align-items:center; gap:8px; flex-wrap:wrap; justify-content: center; }
            .tab { background: transparent; border: 1px solid rgba(255,255,255,0.12); color:#dfe7ef; padding:8px 12px; border-radius:8px; cursor:pointer; }
            .tab.active { background: linear-gradient(135deg, rgba(102,126,234,0.18), rgba(118,75,162,0.18)); border-color: transparent; }
            .grid2 { display:grid; grid-template-columns: repeat(12, 1fr); gap:16px; }
            .left { grid-column: span 12; }
            .card { background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02)); border: 1px solid rgba(255,255,255,0.08); border-radius:14px; padding:16px 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.25), inset 0 1px 0 rgba(255,255,255,0.05); }
            h2 { font-size: 1.05rem; margin:0 0 10px; color:#ffffff; letter-spacing:.2px; }
            p.muted, .muted { color: var(--muted); margin: 4px 0 12px; font-size:.95rem; }
            label { display:block; font-size:.85rem; color:#c8d1db; margin:8px 0 6px; }
            input, select, textarea { width:100%; background:#0b1030; color:#e6edf6; border:1px solid rgba(255,255,255,0.12); border-radius:8px; padding:10px 12px; outline:none; }
            input::placeholder{ color:#6e7787; }
            .row { display:flex; gap:10px; flex-wrap:wrap; }
            .row > div { flex:1 1 220px; min-width:220px; }
            .btn { background: linear-gradient(135deg, var(--primary), var(--primary2)); color:#fff; border:none; border-radius:8px; padding:10px 14px; cursor:pointer; box-shadow: 0 10px 20px rgba(118,75,162,0.35); }
            .btn.secondary { background:#0b1030; border:1px solid rgba(255,255,255,0.12); color:#dfe7ef; box-shadow:none; }
            .stack { display:flex; flex-direction:column; gap:10px; }
            .kpi { display:flex; gap:12px; flex-wrap:wrap; }
            .kpi .pill { background:#0b1030; border:1px solid rgba(255,255,255,0.12); color:#cbd5e1; border-radius:999px; padding:6px 10px; font-size:.85rem; }
            .list { display:flex; flex-direction:column; }
            .item { padding:12px; border-top:1px dashed rgba(255,255,255,0.09); }
            .item:first-child{ border-top:none; }
            .item h3 { margin:0 0 4px; font-size:1rem; color:#fff; }
            .right .card { position: sticky; top:16px; }
            code.block { display:block; background:#0b1030; border:1px solid rgba(255,255,255,0.12); padding:12px; border-radius:8px; color:#d1eaff; white-space:pre-wrap; word-break:break-word; }
            .ok{color:#8de38d;} .warn{color:#f0cc7b;} .err{color:#ff8a8a;}
        </style>
    </head>
    <body>
        <div class="shell">
            <header>
                <div class="brand">
                    <div class="logo"></div>
                    <h1>NewsFlow Analytics Hub</h1>
                </div>
                <nav id="tabs">
                    <button class="tab active" data-tab="home">Home</button>
                    <button class="tab" data-tab="search">Search</button>
                    <button class="tab" data-tab="register">Register</button>
                    <button class="tab" data-tab="stats">Stats</button>
                    <button class="tab" data-tab="docs">Docs</button>
                </nav>
            </header>

            <div class="grid2">
                <section class="left">
                    <!-- Home -->
                    <div class="card" data-panel="home" style="text-align: center;">
                        <h2 style="font-size: 2.5rem; margin-bottom: 16px;">NewsFlow Analytics Hub</h2>
                        <p class="muted" style="font-size: 1.1rem; margin-bottom: 24px;">News intelligence platform with search, registration, and API management.</p>
                        <div style="display: flex; justify-content: center; gap: 12px; flex-wrap: wrap;">
                            <button class="btn" onclick="switchTab('search')" style="padding: 12px 24px; font-size: 1rem;">Start Searching</button>
                            <button class="btn secondary" onclick="switchTab('register')" style="padding: 12px 24px; font-size: 1rem;">Create API Key</button>
                        </div>
                    </div>

                    <!-- Search -->
                    <div class="card" data-panel="search" style="display:none;">
                        <h2>Search News</h2>
                        <p class="muted">Enter a keyword, select time/sort and search using your API key.</p>
                        <div class="row">
                            <div>
                                <label>API Key</label>
                                <input id="apiKey" type="text" placeholder="Enter your API key" oninput="persistKey()" />
                            </div>
                        </div>
                        <div class="row">
                            <div>
                                <label>Keyword</label>
                                <input id="kw" type="text" placeholder="e.g., Tesla" />
                            </div>
                            <div>
                                <label>Time Range</label>
                                <select id="time"><option value="d">Day</option><option value="w">Week</option><option value="m">Month</option><option value="y">Year</option></select>
                            </div>
                            <div>
                                <label>Sort</label>
                                <select id="sort"><option value="1">Relevance</option><option value="2">Date</option></select>
                            </div>
                            <div>
                                <label>Page (multiple of 10)</label>
                                <input id="page" type="number" value="0" min="0" step="10" />
                            </div>
                        </div>
                        <div class="row" style="margin-top:10px;">
                            <button class="btn" onclick="doSearch()">Search</button>
                            <button class="btn secondary" onclick="clearResults()">Clear</button>
                            <span id="searchStatus" class="muted"></span>
                        </div>
                        <div id="results" class="list" style="margin-top:10px;"></div>
                    </div>

                    <!-- Register -->
                    <div class="card" data-panel="register" style="display:none;">
                        <h2>Create API Key</h2>
                        <p class="muted">Choose a plan and enter an email (optional) to generate a key.</p>
                        <div class="row">
                            <div>
                                <label>Plan</label>
                                <select id="plan">
                                    <option value="free">Free (100/day)</option>
                                    <option value="starter">Starter (1,000/day)</option>
                                    <option value="pro">Pro (10,000/day)</option>
                                    <option value="enterprise">Enterprise (Unlimited)</option>
                                </select>
                            </div>
                            <div>
                                <label>Email (optional)</label>
                                <input id="email" type="email" placeholder="you@example.com" />
                            </div>
                        </div>
                        <div class="row" style="margin-top:10px;">
                            <button class="btn" onclick="doRegister()">Generate Key</button>
                            <span id="regStatus" class="muted"></span>
                        </div>
                        <div id="regResult" class="stack" style="margin-top:10px;"></div>
                    </div>

                    <!-- Stats -->
                    <div class="card" data-panel="stats" style="display:none;">
                        <h2>Usage & Limits</h2>
                        <p class="muted">Fetch your current plan, daily limit and remaining requests.</p>
                        <div class="row">
                            <button class="btn" onclick="fetchStats()">Refresh</button>
                            <span id="statsStatus" class="muted"></span>
                        </div>
                        <div id="statsResult" class="stack" style="margin-top:10px;"></div>
                    </div>

                    <!-- Docs -->
                    <div class="card" data-panel="docs" style="display:none;">
                        <h2>API Docs (Quick)</h2>
                        <p class="muted">Use <code>X-API-Key</code> header. Endpoint: <code>/api/v1/news/{keyword}</code></p>
                        <div class="stack">
                            <code class="block">GET /api/v1/news/Tesla?sort=1&time=d&page=0\nHeader: X-API-Key: YOUR_API_KEY</code>
                            <code class="block" id="curlSample"></code>
                        </div>
                        <div class="row" style="margin-top:10px;">
                            <a class="btn secondary" href="/docs" target="_blank" rel="noopener">Open Full Docs</a>
                        </div>
                    </div>
                </section>


            </div>


        </div>

        <script>
            // Tabs
            function switchTab(name){
                document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active', t.dataset.tab===name));
                document.querySelectorAll('[data-panel]').forEach(p=>p.style.display = (p.dataset.panel===name?'block':'none'));
            }
            document.getElementById('tabs').addEventListener('click', (e)=>{
                if(e.target.classList.contains('tab')) switchTab(e.target.dataset.tab);
            });

            // API key persistence
            const keyInput = document.getElementById('apiKey');
            function loadKey(){ const k = localStorage.getItem('newsflow_api_key')||''; keyInput.value = k; updateKeyState(); buildCurl(); }
            function persistKey(){ localStorage.setItem('newsflow_api_key', keyInput.value.trim()); updateKeyState(); buildCurl(); }
            function clearKey(){ keyInput.value=''; persistKey(); }
            function copyKey(){ if(!keyInput.value) return; navigator.clipboard.writeText(keyInput.value); document.getElementById('keyState').textContent='Copied to clipboard'; setTimeout(updateKeyState,1200); }
            function updateKeyState(){ const v = keyInput.value.trim(); document.getElementById('keyState').textContent = v? 'Key loaded' : 'No key set'; }

            // Docs sample
            function buildCurl(){
                const k = keyInput.value.trim()||'YOUR_API_KEY';
                const kw = encodeURIComponent(document.getElementById('kw')?.value || 'Tesla');
                const t = document.getElementById('time')?.value || 'd';
                const s = document.getElementById('sort')?.value || '1';
                const curl = `curl -H "X-API-Key: ${k}" "http://localhost:4000/api/v1/news/${kw}?sort=${s}&time=${t}&page=0"`;
                const el = document.getElementById('curlSample'); if(el) el.textContent = curl;
            }

            // Search
            async function doSearch(){
                const apiKey = keyInput.value.trim();
                const kw = document.getElementById('kw').value.trim();
                const t = document.getElementById('time').value; const s = document.getElementById('sort').value; const pg = parseInt(document.getElementById('page').value||'0',10)||0;
                const status = document.getElementById('searchStatus'); const list = document.getElementById('results');
                list.innerHTML=''; if(!apiKey){ status.textContent='Set an API key first'; return; } if(!kw){ status.textContent='Enter a keyword'; return; }
                status.textContent='Searching...';
                try{
                    const res = await fetch(`/api/v1/news/${encodeURIComponent(kw)}?sort=${encodeURIComponent(s)}&time=${encodeURIComponent(t)}&page=${encodeURIComponent(pg)}`, { headers:{'X-API-Key': apiKey }});
                    if(!res.ok){ const e = await res.json().catch(()=>({message:`HTTP ${res.status}`})); throw new Error(e.message); }
                    const data = await res.json();
                    renderArticles(data);
                    status.textContent='Done';
                }catch(err){ status.textContent = err.message || 'Request failed'; }
            }
            function clearResults(){ document.getElementById('results').innerHTML=''; document.getElementById('searchStatus').textContent=''; }
            function renderArticles(resp){
                const list = document.getElementById('results');
                const { data=[], total_results=0, plan='', requests_remaining=null } = resp || {};
                const head = document.createElement('div');
                head.className='muted';
                head.innerHTML = `Results: <strong>${total_results}</strong>` + (plan?` · Plan: ${plan}`:'') + (requests_remaining!=null?` · Remaining: ${requests_remaining}`:'');
                list.appendChild(head);
                if(!data.length){ const d=document.createElement('div'); d.className='muted'; d.textContent='No results'; list.appendChild(d); return; }
                for(const a of data){
                    const it = document.createElement('div'); it.className='item';
                    const title = a.title || '(no title)'; const link = a.link || '#'; const date = a.date || ''; const desc = a.description || ''; const kw=a.keyword||'';
                    it.innerHTML = `<h3><a href="${link}" target="_blank" rel="noopener">${title}</a></h3><div class="muted">${date} ${kw? ' · '+kw:''}</div>${desc?`<div style="margin-top:6px;">${desc}</div>`:''}`;
                    list.appendChild(it);
                }
            }

            // Register
            async function doRegister(){
                const plan = document.getElementById('plan').value; const email = document.getElementById('email').value.trim();
                const status = document.getElementById('regStatus'); const out = document.getElementById('regResult');
                status.textContent='Creating...'; out.innerHTML='';
                try{
                    const res = await fetch('/api/v1/register', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ plan, email: email||undefined }) });
                    if(!res.ok){ const e = await res.json().catch(()=>({error:`HTTP ${res.status}`})); throw new Error(e.error||e.message||'Failed'); }
                    const data = await res.json();
                    out.innerHTML = `<div class="item"><div><strong>Plan:</strong> ${data.plan}</div><div><strong>API Key:</strong> <code>${data.api_key}</code></div></div>`;
                    document.getElementById('apiKey').value = data.api_key || '';
                    persistKey(); buildCurl();
                    status.textContent='Success';
                }catch(err){ status.textContent = err.message || 'Failed'; }
            }

            // Stats
            async function fetchStats(){
                const apiKey = keyInput.value.trim(); const status = document.getElementById('statsStatus'); const out = document.getElementById('statsResult');
                out.innerHTML=''; if(!apiKey){ status.textContent='Set an API key first'; return; }
                status.textContent='Loading...';
                try{
                    const res = await fetch('/api/v1/stats', { headers:{'X-API-Key': apiKey} });
                    if(!res.ok){ const e = await res.json().catch(()=>({error:`HTTP ${res.status}`})); throw new Error(e.error||e.message||'Failed'); }
                    const s = await res.json();
                    out.innerHTML = `<div class="item"><div><strong>Plan:</strong> ${s.plan}</div><div><strong>Requests today:</strong> ${s.requests_today}</div><div><strong>Daily limit:</strong> ${s.daily_limit}</div><div><strong>Remaining:</strong> ${s.requests_remaining}</div><div class="muted">Created: ${s.created_date || '-'} · Last reset: ${s.last_reset || '-'}</div></div>`;
                    status.textContent='';
                }catch(err){ status.textContent = err.message || 'Failed'; }
            }

            // Init
            loadKey(); buildCurl();
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

@app.route('/ui')
def search_ui():
    """Simple search UI that calls the existing API endpoint."""
    html_content = """
    <!DOCTYPE html>
    <html lang=\"en\">
    <head>
        <meta charset=\"UTF-8\" />
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
        <title>NewsFlow - Simple Search UI</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 30px; color: #333; }
            h1 { margin-bottom: 10px; }
            .card { background: #f8f9fa; padding: 16px; border-radius: 8px; margin-bottom: 16px; }
            label { display: block; margin: 8px 0 4px; font-weight: 600; }
            input, select { width: 100%; max-width: 420px; padding: 10px; border: 1px solid #ddd; border-radius: 6px; }
            button { background: #667eea; color: white; border: none; padding: 10px 16px; border-radius: 6px; cursor: pointer; margin-top: 12px; }
            button:hover { opacity: 0.9; }
            .results { margin-top: 20px; }
            .article { padding: 12px; border-bottom: 1px solid #eee; }
            .article h3 { margin: 0 0 6px; font-size: 1.05rem; }
            .muted { color: #777; font-size: 0.9rem; }
            .row { display: flex; gap: 12px; flex-wrap: wrap; }
            .row > div { flex: 1 1 220px; min-width: 220px; }
            .badge { display: inline-block; background: #eef; color: #334; padding: 2px 8px; border-radius: 999px; font-size: 0.8rem; margin-left: 6px; }
            .footer { margin-top: 24px; font-size: 0.9rem; }
            .error { color: #b00020; margin-top: 8px; }
            .ok { color: #2e7d32; }
        </style>
    </head>
    <body>
        <h1>NewsFlow Search</h1>
        <div class=\"muted\">Use your API key to search Google News via the built-in API.</div>

        <div class=\"card\">
            <div class=\"row\">
                <div>
                    <label for=\"apiKey\">API Key</label>
                    <input id=\"apiKey\" type=\"text\" placeholder=\"Enter your API key\" />
                </div>
                <div>
                    <label for=\"keyword\">Keyword</label>
                    <input id=\"keyword\" type=\"text\" placeholder=\"e.g., tesla\" />
                </div>
                <div>
                    <label for=\"time\">Time Range</label>
                    <select id=\"time\">
                        <option value=\"d\">Day</option>
                        <option value=\"w\">Week</option>
                        <option value=\"m\">Month</option>
                        <option value=\"y\">Year</option>
                    </select>
                </div>
                <div>
                    <label for=\"sort\">Sort By</label>
                    <select id=\"sort\">
                        <option value=\"1\">Relevance</option>
                        <option value=\"2\">Date</option>
                    </select>
                </div>
                <div>
                    <label for=\"page\">Page (multiple of 10)</label>
                    <input id=\"page\" type=\"number\" value=\"0\" min=\"0\" step=\"10\" />
                </div>
            </div>
            <button id=\"searchBtn\">Search</button>
            <div id=\"status\" class=\"muted\" style=\"margin-top:8px\"></div>
            <div id=\"error\" class=\"error\"></div>
        </div>

        <div class=\"results\" id=\"results\"></div>

        <div class=\"footer muted\">
            Tip: Register a key at <code>/api/v1/register</code>. Read docs at <code>/docs</code>.
        </div>

        <script>
            const $ = (id) => document.getElementById(id);
            const statusEl = $("status");
            const errorEl = $("error");
            const resultsEl = $("results");

            function setStatus(text, ok=false) {
                statusEl.textContent = text || "";
                statusEl.className = ok ? "muted ok" : "muted";
            }

            function setError(text) {
                errorEl.textContent = text || "";
            }

            function renderResults(resp) {
                const { data = [], total_results = 0, plan = "", requests_remaining = null } = resp || {};
                resultsEl.innerHTML = "";

                const header = document.createElement('div');
                header.className = 'muted';
                header.innerHTML = `Results: <strong>${total_results}</strong> <span class="badge">Plan: ${plan}</span>` +
                                   (requests_remaining !== null ? ` <span class="badge">Remaining: ${requests_remaining}</span>` : "");
                resultsEl.appendChild(header);

                if (!Array.isArray(data) || data.length === 0) {
                    const empty = document.createElement('div');
                    empty.className = 'muted';
                    empty.style.marginTop = '8px';
                    empty.textContent = 'No results.';
                    resultsEl.appendChild(empty);
                    return;
                }

                for (const item of data) {
                    const div = document.createElement('div');
                    div.className = 'article';
                    const title = item.title || '(no title)';
                    const link = item.link || '#';
                    const date = item.date || '';
                    const desc = item.description || '';
                    const kw = item.keyword || '';
                    div.innerHTML = `
                        <h3><a href="${link}" target="_blank" rel="noopener noreferrer">${title}</a></h3>
                        <div class="muted">${date}${kw ? ` · ${kw}` : ''}</div>
                        ${desc ? `<div style="margin-top:6px">${desc}</div>` : ''}
                    `;
                    resultsEl.appendChild(div);
                }
            }

            async function doSearch() {
                setError("");
                setStatus("Searching...");
                resultsEl.innerHTML = "";

                const apiKey = $("apiKey").value.trim();
                const keyword = $("keyword").value.trim();
                const time = $("time").value;
                const sort = $("sort").value;
                const page = parseInt($("page").value || '0', 10) || 0;

                if (!apiKey) { setError("API key is required (use /api/v1/register). "); return; }
                if (!keyword) { setError("Keyword is required."); return; }

                const url = `/api/v1/news/${encodeURIComponent(keyword)}?sort=${encodeURIComponent(sort)}&time=${encodeURIComponent(time)}&page=${encodeURIComponent(page)}`;

                try {
                    const res = await fetch(url, { headers: { 'X-API-Key': apiKey } });
                    if (!res.ok) {
                        const err = await res.json().catch(() => ({}));
                        throw new Error(err.message || `Request failed with ${res.status}`);
                    }
                    const data = await res.json();
                    renderResults(data);
                    setStatus("Done", true);
                } catch (e) {
                    setStatus("");
                    setError(e.message || String(e));
                }
            }

            document.getElementById('searchBtn').addEventListener('click', doSearch);
            document.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') doSearch();
            });
        </script>
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