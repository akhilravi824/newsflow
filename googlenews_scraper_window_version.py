#!/usr/bin/env python3
"""
Google News Scraper - Improved Version with Proxy Rotation
A robust, async web scraper for Google News with better error handling, maintainability, and proxy rotation.
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


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('scraper.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


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
                # Parse HTML to extract proxy list
                doc = html.fromstring(response.text)
                proxy_rows = doc.xpath('//table[@class="table table-striped table-bordered"]//tr')[1:]  # Skip header
                
                for row in proxy_rows[:20]:  # Limit to first 20
                    try:
                        cells = row.xpath('.//td/text()')
                        if len(cells) >= 8:
                            ip = cells[0].strip()
                            port = int(cells[1].strip())
                            https = cells[6].strip() == 'yes'
                            country = cells[3].strip()
                            
                            if https:
                                protocol = 'https'
                            else:
                                protocol = 'http'
                            
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
        
        # Source 2: ProxyNova
        try:
            response = requests.get('https://www.proxynova.com/proxy-server-list/', timeout=10)
            if response.status_code == 200:
                doc = html.fromstring(response.text)
                proxy_scripts = doc.xpath('//script[contains(text(), "document.write")]/text()')
                
                for script in proxy_scripts[:10]:  # Limit to first 10
                    try:
                        # Extract IP and port from JavaScript
                        if 'document.write' in script:
                            # Simple regex-like extraction
                            import re
                            ip_match = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', script)
                            port_match = re.search(r'(\d{4,5})', script)
                            
                            if ip_match and port_match:
                                ip = ip_match.group(1)
                                port = int(port_match.group(1))
                                
                                proxy = ProxyConfig(
                                    host=ip,
                                    port=port,
                                    protocol='http'
                                )
                                self.proxies.append(proxy)
                    except Exception as e:
                        continue
                        
                logger.info(f"Added {len(self.proxies)} proxies from ProxyNova")
        except Exception as e:
            logger.warning(f"Could not fetch from ProxyNova: {e}")
        
        # Source 3: Add some common free proxy services
        common_proxies = [
            ('127.0.0.1', 8080, 'http'),  # Local proxy if you have one
            ('127.0.0.1', 1080, 'socks5'),  # SOCKS proxy
        ]
        
        for host, port, protocol in common_proxies:
            proxy = ProxyConfig(host=host, port=port, protocol=protocol)
            self.proxies.append(proxy)
        
        if self.proxies:
            self.save_proxies()
            logger.info(f"Total proxies loaded: {len(self.proxies)}")
        else:
            logger.warning("No free proxies could be loaded")
    
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
    
    def get_next_proxy(self) -> Optional[ProxyConfig]:
        """Get next working proxy using round-robin."""
        if not self.proxies:
            return None
        
        # Find next working proxy
        attempts = 0
        while attempts < len(self.proxies):
            self.current_proxy_index = (self.current_proxy_index + 1) % len(self.proxies)
            proxy = self.proxies[self.current_proxy_index]
            
            if proxy.is_working:
                proxy.last_used = datetime.now()
                return proxy
            
            attempts += 1
        
        # If no working proxies found, try to refresh
        logger.warning("No working proxies found, attempting to refresh...")
        self.fetch_free_proxies()
        
        if self.proxies:
            return self.proxies[0]
        return None
    
    def mark_proxy_failed(self, proxy: ProxyConfig):
        """Mark a proxy as failed."""
        proxy.fail_count += 1
        if proxy.fail_count >= 3:
            proxy.is_working = False
            logger.warning(f"Proxy {proxy.host}:{proxy.port} marked as failed")
        self.save_proxies()
    
    def mark_proxy_success(self, proxy: ProxyConfig):
        """Mark a proxy as successful."""
        proxy.success_count += 1
        proxy.is_working = True
        self.save_proxies()


class TorManager:
    """Manages Tor network connections for additional anonymity."""
    
    def __init__(self):
        self.tor_process = None
        self.tor_port = 9050
        self.socks_port = 9051
        
    def start_tor(self):
        """Start Tor service if available."""
        try:
            if platform.system() == "Windows":
                # Windows - try to start Tor service
                subprocess.run(['net', 'start', 'tor'], check=False)
            else:
                # Linux/Mac - try to start tor process
                try:
                    self.tor_process = subprocess.Popen(
                        ['tor', '--SocksPort', str(self.socks_port)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    time.sleep(5)  # Wait for Tor to start
                    logger.info("Tor service started")
                except FileNotFoundError:
                    logger.warning("Tor not found, skipping Tor support")
                    
        except Exception as e:
            logger.warning(f"Could not start Tor: {e}")
    
    def stop_tor(self):
        """Stop Tor service."""
        if self.tor_process:
            self.tor_process.terminate()
            self.tor_process.wait()
            logger.info("Tor service stopped")
    
    def get_tor_proxy(self) -> ProxyConfig:
        """Get Tor SOCKS proxy configuration."""
        return ProxyConfig(
            host='127.0.0.1',
            port=self.socks_port,
            protocol='socks5'
        )
    
    async def renew_tor_identity(self):
        """Renew Tor identity (get new IP)."""
        try:
            async with aiohttp.ClientSession() as session:
                # Connect to Tor control port
                control_url = f"http://127.0.0.1:{self.tor_port}"
                async with session.post(f"{control_url}/tor/control/0/signal/newnym") as response:
                    if response.status == 200:
                        logger.info("Tor identity renewed")
                    else:
                        logger.warning("Could not renew Tor identity")
        except Exception as e:
            logger.warning(f"Error renewing Tor identity: {e}")


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
        self.PROXY_ROTATION_INTERVAL = 10  # Rotate proxy every N requests
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
        self.tor_manager = TorManager() if config.USE_TOR else None
        self.request_count = 0
        
    async def __aenter__(self):
        """Async context manager entry."""
        # Start Tor if enabled
        if self.tor_manager:
            self.tor_manager.start_tor()
        
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
        
        if self.tor_manager:
            self.tor_manager.stop_tor()
    
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
    
    def _get_proxy_url(self) -> Optional[str]:
        """Get current proxy URL for requests."""
        if not self.config.USE_PROXIES:
            return None
        
        proxy = self.proxy_manager.get_next_proxy()
        if proxy:
            return proxy.url
        return None
    
    async def fetch_with_retry(self, url: str, retries: int = None) -> Optional[str]:
        """Fetch URL content with retry logic, proxy rotation, and proper error handling."""
        if retries is None:
            retries = self.config.MAX_RETRIES
            
        for attempt in range(retries):
            try:
                async with self.semaphore:
                    # Rotate proxy if needed
                    if self.config.USE_PROXIES and self.request_count % self.config.PROXY_ROTATION_INTERVAL == 0:
                        await self._rotate_proxy()
                    
                    headers = {'User-Agent': self.config.get_random_user_agent()}
                    proxy_url = self._get_proxy_url()
                    
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
                                if proxy_url:
                                    self.proxy_manager.mark_proxy_success(
                                        self.proxy_manager.proxies[self.proxy_manager.current_proxy_index]
                                    )
                                return await response.text()
                            elif response.status == 429:
                                logger.warning(f"Rate limited (429) for {url}, attempt {attempt + 1}/{retries}")
                                await self._handle_rate_limit()
                                if proxy_url:
                                    self.proxy_manager.mark_proxy_failed(
                                        self.proxy_manager.proxies[self.proxy_manager.current_proxy_index]
                                    )
                            elif response.status == 407:
                                logger.error(f"Proxy authentication error (407) for {url}")
                                if proxy_url:
                                    self.proxy_manager.mark_proxy_failed(
                                        self.proxy_manager.proxies[self.proxy_manager.current_proxy_index]
                                    )
                                return None
                            else:
                                logger.error(f"HTTP {response.status} for {url}")
                                self._log_error(url, response.status)
                                if proxy_url:
                                    self.proxy_manager.mark_proxy_failed(
                                        self.proxy_manager.proxies[self.proxy_manager.current_proxy_index]
                                    )
                            
            except aiohttp.ClientConnectorError as e:
                logger.error(f"Connection error for {url}: {e}")
                if proxy_url:
                    self.proxy_manager.mark_proxy_failed(
                        self.proxy_manager.proxies[self.proxy_manager.current_proxy_index]
                    )
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
        
        # Renew Tor identity if using Tor
        if self.tor_manager and self.request_count % (self.config.PROXY_ROTATION_INTERVAL * 2) == 0:
            await self.tor_manager.renew_tor_identity()
    
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
    
    async def scrape_keywords(self, keywords: List[str]) -> Dict[str, List[NewsArticle]]:
        """Scrape news for multiple keywords."""
        results = {}
        
        tasks = []
        for keyword in keywords:
            if keyword.strip():
                task = self.parse_google_news(keyword.strip())
                tasks.append(task)
        
        if tasks:
            all_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for i, result in enumerate(all_results):
                if isinstance(result, Exception):
                    logger.error(f"Error scraping keyword {keywords[i]}: {result}")
                    results[keywords[i]] = []
                else:
                    results[keywords[i]] = result
        
        return results


class DataExporter:
    """Handle data export to CSV files."""
    
    @staticmethod
    def export_to_csv(articles: List[NewsArticle], output_dir: Path = None):
        """Export articles to CSV files."""
        if output_dir is None:
            output_dir = Path('.')
            
        if not articles:
            logger.warning("No articles to export")
            return
            
        # Convert to DataFrame
        data = [
            [article.keyword, article.title, article.link, article.description, article.date]
            for article in articles
        ]
        
        df = pd.DataFrame(data, columns=['keyword', 'title', 'link', 'description', 'date'])
        
        # Export main data
        today = datetime.now().strftime("%Y_%m_%d")
        main_filename = output_dir / f'output_{today}.csv'
        
        # Append to existing file or create new one
        mode = 'a' if main_filename.exists() else 'w'
        header = not main_filename.exists()
        
        df.to_csv(main_filename, mode=mode, index=False, header=header)
        logger.info(f"Exported {len(articles)} articles to {main_filename}")
        
        # Export summary statistics
        summary_data = []
        for keyword in df['keyword'].unique():
            count = len(df[df['keyword'] == keyword])
            summary_data.append([keyword, count])
            
        summary_df = pd.DataFrame(summary_data, columns=['keyword', 'count'])
        summary_filename = output_dir / f'output_count_{today}.csv'
        
        mode = 'a' if summary_filename.exists() else 'w'
        header = not summary_filename.exists()
        
        summary_df.to_csv(summary_filename, mode=mode, index=False, header=header)
        logger.info(f"Exported summary to {summary_filename}")


async def main():
    """Main function to run the scraper."""
    # Load keywords
    try:
        with open('input_keywords.txt', 'r') as f:
            keywords = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        logger.error("input_keywords.txt not found!")
        return
    except Exception as e:
        logger.error(f"Error reading keywords file: {e}")
        return
    
    if not keywords:
        logger.error("No keywords found in input_keywords.txt")
        return
    
    logger.info(f"Starting scraper with {len(keywords)} keywords")
    start_time = time.time()
    
    config = Config()
    
    try:
        async with GoogleNewsScraper(config) as scraper:
            results = await scraper.scrape_keywords(keywords)
            
            # Export results
            all_articles = []
            for keyword, articles in results.items():
                all_articles.extend(articles)
                logger.info(f"Keyword '{keyword}': {len(articles)} articles found")
            
            if all_articles:
                DataExporter.export_to_csv(all_articles)
            else:
                logger.warning("No articles found for any keyword")
                
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        
    finally:
        elapsed_time = time.time() - start_time
        logger.info(f"Scraping completed in {elapsed_time:.2f} seconds")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Scraping interrupted by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
