from tavily import AsyncTavilyClient
import httpx
from dotenv import load_dotenv
from agentpress.tool import ToolResult
from utils.config import config
from sandbox.tool_base import SandboxToolsBase
import json
from typing import Optional, Sequence, TYPE_CHECKING
import os
import datetime
import asyncio
import logging
import re
from urllib.parse import urlparse, unquote
import ipaddress
import socket
from utils.agent_run_context import get_agent_run_context

if TYPE_CHECKING:
    from agentpress.adk_thread_manager import ADKThreadManager

# TODO: add subpages, etc... in filters as sometimes its necessary

# 图片下载配置
IMAGE_DOWNLOAD_TIMEOUT = 30  # 每张图片超时时间（秒）
MAX_CONCURRENT_DOWNLOADS = 5  # 最大并发下载数
MAX_IMAGE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB max per image
MAX_URLS_PER_REQUEST = 50  # 单次请求最大 URL 数量
MAX_FOLDER_NAME_LENGTH = 100  # 文件夹名称最大长度
MAX_FILENAME_ATTEMPTS = 1000  # 文件名冲突重试次数上限
VALID_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp'}
VALID_IMAGE_MIMETYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/svg+xml', 'image/bmp'}
# 禁止访问的私有 IP 范围
BLOCKED_HOSTS = {'localhost', '127.0.0.1', '::1', '0.0.0.0'}
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"


_COMPANION_SOLUTION_PATTERNS = (
    r"\banswer\s+key(?:s)?\b",
    r"\banswers?\b",
    r"\bsolution(?:s)?\b",
    r"\bmark\s+scheme(?:s)?\b",
    r"\bmarking\s+scheme(?:s)?\b",
    r"\b解析\b",
    r"\b答案\b",
)
_DOCUMENT_CONTEXT_PATTERNS = (
    r"\buploaded\b",
    r"\bpdf\b",
    r"\bfile(?:s)?\b",
    r"\bdocument(?:s)?\b",
    r"\bquestion\s+paper(?:s)?\b",
    r"\bexam(?:s)?\b",
    r"\bworksheet(?:s)?\b",
    r"\bassignment(?:s)?\b",
    r"\bhomework\b",
    r"\bpaper(?:s)?\b",
    r"\bqp\b",
    r"\b试卷\b",
    r"\b卷子\b",
    r"\b题目\b",
)
_DOCUMENT_IDENTIFIER_PATTERNS = (
    # Cambridge-style paper identifiers, e.g. 9708_s24_qp_33.
    r"\b\d{4}_[a-z]\d{2}_qp_\d{2}\b",
    # A common companion-file form, e.g. 9708/33/M/J/24 or 9708 33 M J 24.
    r"\b\d{4}[/_\s-]\d{1,2}[/_\s-][msw][/_\s-][a-z][/_\s-]\d{2}\b",
)


def _looks_like_companion_solution_lookup(query: str) -> bool:
    """Return True for web searches seeking answer/solution companions to local docs.

    This is intentionally narrower than "all document tasks": normal current web
    research and conceptual searches must still work.  The guard only redirects
    queries that combine answer/solution intent with document/exam context or a
    recognizable document identifier.
    """
    normalized = " ".join(str(query or "").lower().replace("_", " ").split())
    raw_lower = str(query or "").lower()
    if not normalized:
        return False

    has_solution_intent = any(
        re.search(pattern, normalized) or re.search(pattern, raw_lower)
        for pattern in _COMPANION_SOLUTION_PATTERNS
    )
    if not has_solution_intent:
        return False

    has_document_identifier = any(
        re.search(pattern, raw_lower) for pattern in _DOCUMENT_IDENTIFIER_PATTERNS
    )
    if has_document_identifier:
        return True

    has_document_context = any(
        re.search(pattern, normalized) for pattern in _DOCUMENT_CONTEXT_PATTERNS
    )
    return has_document_context


class SandboxWebSearchTool(SandboxToolsBase):
    """Tool for performing web searches using Tavily API and web scraping using Tavily Extract/Firecrawl."""

    def __init__(
        self,
        project_id: str,
        thread_manager: "ADKThreadManager",
        *,
        shadow_clone_run_id: Optional[str] = None,
        strict_sandbox: bool = False,
    ):
        super().__init__(
            project_id=project_id,
            thread_manager=thread_manager,
            sandbox_type="code",
            shadow_clone_run_id=shadow_clone_run_id,
            strict_sandbox=strict_sandbox,
        )
        # 加载当前的环境变量，覆盖默认的环境变量
        load_dotenv(override=True)
    
        self.tavily_api_key = config.TAVILY_API_KEY
        self.firecrawl_api_key = config.FIRECRAWL_API_KEY
        self.firecrawl_url = config.FIRECRAWL_URL
        
        if not self.tavily_api_key:
            raise ValueError("TAVILY_API_KEY not found in configuration")
        if not self.firecrawl_api_key:
            raise ValueError("FIRECRAWL_API_KEY not found in configuration")

        # 获取 Tavily 的异步搜索客户端
        self.tavily_client = AsyncTavilyClient(api_key=self.tavily_api_key)

    async def _sandbox_make_dir(self, path: str) -> None:
        await self._run_blocking_sandbox_call(
            "files.make_dir",
            lambda: self.sandbox.files.make_dir(path),
        )

    async def _sandbox_write_file(self, path: str, content) -> None:
        await self._run_blocking_sandbox_call(
            "files.write",
            lambda: self.sandbox.files.write(path, content),
        )
        await self._persist_workspace_artifact(
            path,
            content,
            source="sandbox_web_search_tool.write_file",
        )

    async def _sandbox_read_file(self, path: str):
        return await self._run_blocking_sandbox_call(
            "files.read",
            lambda: self.sandbox.files.read(path),
        )

    async def web_search(
        self, 
        query: str,
        num_results: int = 20
    ) -> ToolResult:
        """
        Search the web for up-to-date information on a specific topic using the Tavily API.
        
        This tool allows you to gather real-time information from the internet to answer user queries, 
        research topics, validate facts, and find recent developments. Results include titles, URLs, 
        and publication dates. Use this tool for discovering relevant web pages before potentially 
        crawling them for complete content.
        
        Usage Examples:
            {
                "name": "web_search",
                "parameters": {
                    "query": "what is FuFanManus and what are they building?",
                    "num_results": 20
                }
            }
            
            # Another search example
            {
                "name": "web_search",
                "parameters": {
                    "query": "latest AI research on transformer models",
                    "num_results": 20
                }
            }
        
        Args:
            query: The search query to find relevant web pages. Be specific and include key terms 
                  to improve search accuracy. For best results, use natural language questions or 
                  keyword combinations that precisely describe what you're looking for.
            num_results: The number of search results to return. Increase for more comprehensive 
                        research or decrease for focused, high-relevance results. Default is 20.
        
        Returns:
            ToolResult: Success with JSON string of search results, or failure with error message.
        """
        try:
            # 确保有一个有效的查询
            if not query or not isinstance(query, str):
                return self.fail_response("A valid search query is required.")

            if _looks_like_companion_solution_lookup(query):
                return ToolResult(
                    success=False,
                    output={
                        "error_code": "DOCUMENT_COMPANION_SEARCH_REDIRECT",
                        "error": (
                            "This looks like a search for companion answer/solution files "
                            "for a document-analysis task. Unless the user explicitly asked "
                            "for external sources, use the provided files and user-supplied "
                            "facts first: perform targeted extraction, infer patterns from "
                            "the available evidence, then write and verify the requested "
                            "deliverables."
                        ),
                        "query": query,
                        "suggested_action": (
                            "Return to the uploaded/provided documents; do not fetch external "
                            "answer or solution companions for them."
                        ),
                    },
                )

            # 使用 Tavily 执行搜索
            search_response = await self.tavily_client.search(
                query=query,
                max_results=num_results,
                include_images=True,
                include_answer="advanced",
                search_depth="advanced",
            )
            
            # 检查是否实际有结果或答案
            results = search_response.get('results', [])
            answer = search_response.get('answer', '')
            
            # 返回完整的 Tavily 响应
            # 这包括查询、答案、结果、图像等
            
            # 考虑搜索成功，如果结果或答案存在
            if len(results) > 0 or (answer and answer.strip()):
                return ToolResult(
                    success=True,
                    output=json.dumps(search_response, ensure_ascii=False)
                )
            else:
                # 没有结果或答案找到
                logging.warning(f"No search results or answer found for query: '{query}'")
                return ToolResult(
                    success=False,
                    output=json.dumps(search_response, ensure_ascii=False)
                )
        
        except Exception as e:
            error_message = str(e)
            logging.error(f"Error performing web search for '{query}': {error_message}")
            simplified_message = f"Error performing web search: {error_message[:200]}"
            if len(error_message) > 200:
                simplified_message += "..."
            return self.fail_response(simplified_message)

    async def scrape_webpage(
        self,
        urls: str | Sequence[str],
        query: Optional[str] = None,
        chunks_per_source: Optional[int] = None,
        extract_depth: str = "basic",
        format: str = "markdown",
        include_images: bool = False,
        include_favicon: bool = False,
        timeout: Optional[float] = None,
    ) -> ToolResult:
        """
        Extract full text content from multiple webpages in a single operation.
        
        IMPORTANT: You should ALWAYS collect multiple relevant URLs from web-search results and
        scrape them all in a single call for efficiency. This tool saves time by processing
        multiple pages simultaneously rather than one at a time. The extracted text includes
        the main content of each page without HTML markup.

        This method prefers Tavily Extract for full content and falls back to Firecrawl if needed.
        
        ALWAYS collect multiple relevant URLs from search results and scrape them all at once
        rather than making separate calls for each URL. This is much more efficient.
        
        Usage Example:
            {
                "name": "scrape_webpage",
                "parameters": {
                    "urls": "https://github.com/fufankeji"
                }
            }
        
        Args:
            urls: Multiple URLs to scrape, separated by commas. You should ALWAYS include several
                 URLs when possible for efficiency.
                 Example: 'https://example.com/page1,https://example.com/page2,https://example.com/page3'
            query: Optional query for Tavily Extract reranking.
            chunks_per_source: When query is provided, max chunks per source (1-5).
            extract_depth: Tavily Extract depth ("basic" or "advanced").
            format: Output format ("markdown" or "text").
            include_images: Include images in Tavily Extract response.
            include_favicon: Include favicon in Tavily Extract response.
            timeout: Tavily Extract timeout in seconds (1-60).
        
        Returns:
            ToolResult: Success with message about scraped content and file paths, or failure with error message.
        """

        try:            
            # 确保 sandbox 已初始化
            await self._ensure_sandbox()
            
            # 解析URL参数
            if not urls:
                logging.warning("Scrape attempt with empty URLs")
                return self.fail_response("Valid URLs are required.")
            
            # 切分URL字符串为列表
            if isinstance(urls, (list, tuple)):
                url_list = [url.strip() for url in urls if isinstance(url, str) and url.strip()]
            elif isinstance(urls, str):
                url_list = [url.strip() for url in urls.split(',') if url.strip()]
            else:
                return self.fail_response("Valid URLs are required.")
            
            if not url_list:
                logging.warning("No valid URLs found in the input")
                return self.fail_response("No valid URLs provided.")
                
            if len(url_list) == 1:
                logging.warning("Only a single URL provided - for efficiency you should scrape multiple URLs at once")
            
            logging.info(f"Processing {len(url_list)} URLs: {url_list}")

            normalized_depth = extract_depth if extract_depth in ("basic", "advanced") else "basic"
            normalized_format = format if format in ("markdown", "text") else "markdown"
            query_value = query.strip() if query else ""
            if chunks_per_source is not None and not 1 <= chunks_per_source <= 5:
                logging.warning("chunks_per_source out of range, defaulting to 3")
                chunks_per_source = 3
            if query_value and chunks_per_source is None:
                chunks_per_source = 3

            tavily_results = await self._tavily_extract_urls(
                url_list,
                extract_depth=normalized_depth,
                output_format=normalized_format,
                query=query_value or None,
                chunks_per_source=chunks_per_source,
                include_images=include_images,
                include_favicon=include_favicon,
                timeout=timeout,
            )
            successful_results = [r for r in tavily_results if r.get("success", False)]
            failed_urls = [
                r.get("url") for r in tavily_results if not r.get("success", False) and r.get("url")
            ]

            fallback_results = []
            if failed_urls:
                logging.info(
                    "Tavily Extract fallback: retrying %d URLs with Firecrawl",
                    len(failed_urls),
                )
                tasks = [self._scrape_single_url(url) for url in failed_urls]
                firecrawl_results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, result in enumerate(firecrawl_results):
                    if isinstance(result, Exception):
                        logging.error(f"Error processing URL {failed_urls[i]}: {str(result)}")
                        fallback_results.append({
                            "url": failed_urls[i],
                            "success": False,
                            "error": str(result),
                        })
                    else:
                        fallback_results.append(result)

            results = successful_results + fallback_results

            
            # 总结结果
            successful = sum(1 for r in results if r.get("success", False))
            failed = len(results) - successful
            
            # 创建成功/失败消息
            if successful == len(results):
                message = f"Successfully scraped all {len(results)} URLs. Results saved to:"
                for r in results:
                    if r.get("file_path"):
                        message += f"\n- {r.get('file_path')}"
            elif successful > 0:
                message = f"Scraped {successful} URLs successfully and {failed} failed. Results saved to:"
                for r in results:
                    if r.get("success", False) and r.get("file_path"):
                        message += f"\n- {r.get('file_path')}"
                message += "\n\nFailed URLs:"
                for r in results:
                    if not r.get("success", False):
                        message += f"\n- {r.get('url')}: {r.get('error', 'Unknown error')}"
            else:
                error_details = "; ".join([f"{r.get('url')}: {r.get('error', 'Unknown error')}" for r in results])
                return self.fail_response(f"Failed to scrape all {len(results)} URLs. Errors: {error_details}")
            
            return ToolResult(
                success=True,
                output=message
            )
        
        except Exception as e:
            error_message = str(e)
            logging.error(f"Error in scrape_webpage: {error_message}")
            return self.fail_response(f"Error processing scrape request: {error_message[:200]}")

    async def _tavily_extract_urls(
        self,
        url_list: list[str],
        *,
        extract_depth: str,
        output_format: str,
        query: Optional[str],
        chunks_per_source: Optional[int],
        include_images: bool,
        include_favicon: bool,
        timeout: Optional[float],
    ) -> list[dict]:
        """Attempt full-text extraction via Tavily Extract for multiple URLs."""
        if not url_list:
            return []

        payload = {
            "urls": url_list if len(url_list) > 1 else url_list[0],
            "extract_depth": extract_depth,
            "format": output_format,
            "include_images": include_images,
            "include_favicon": include_favicon,
        }
        if query:
            payload["query"] = query
            if chunks_per_source is not None:
                payload["chunks_per_source"] = chunks_per_source
        if timeout is not None:
            payload["timeout"] = timeout

        request_timeout = 30 if extract_depth == "advanced" else 10
        if timeout is not None:
            request_timeout = max(1, min(timeout, 60))

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    TAVILY_EXTRACT_URL,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.tavily_api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=request_timeout,
                )
                response.raise_for_status()
                data = response.json()
        except Exception as e:
            logging.error(f"Tavily Extract request failed: {e}")
            return [
                {"url": url, "success": False, "error": f"Tavily Extract error: {e}"}
                for url in url_list
            ]

        results = []
        seen_urls = set()
        scrape_dir = f"{self.workspace_path}/scrape"
        await self._sandbox_make_dir(scrape_dir)

        for item in data.get("results", []):
            url = item.get("url")
            raw_content = item.get("raw_content", "")
            if not url:
                continue
            seen_urls.add(url)
            if not raw_content:
                results.append({
                    "url": url,
                    "success": False,
                    "error": "Empty content from Tavily Extract",
                })
                continue

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            parsed_url = urlparse(url)
            domain = parsed_url.netloc.replace("www.", "")
            domain = "".join([c if c.isalnum() else "_" for c in domain])
            safe_filename = f"{timestamp}_{domain}.json"
            results_file_path = f"{scrape_dir}/{safe_filename}"

            result_payload = {
                "title": item.get("title", ""),
                "url": url,
                "text": raw_content,
                "metadata": {
                    "source": "tavily_extract",
                    "format": output_format,
                    "extract_depth": extract_depth,
                },
            }

            images = item.get("images")
            favicon = item.get("favicon")
            if images:
                result_payload["metadata"]["images"] = images
            if favicon:
                result_payload["metadata"]["favicon"] = favicon

            json_content = json.dumps(result_payload, ensure_ascii=False, indent=2)
            await self._sandbox_write_file(results_file_path, json_content.encode())
            await self._append_research_source({
                "url": url,
                "title": result_payload.get("title", ""),
                "method": "tavily_extract",
                "extracted_at": datetime.datetime.now().isoformat(),
                "content_length": len(raw_content),
                "file_path": results_file_path,
            })

            results.append({
                "url": url,
                "success": True,
                "title": "",
                "file_path": results_file_path,
                "content_length": len(raw_content),
            })

        for failed in data.get("failed_results", []):
            url = failed.get("url")
            if not url:
                continue
            seen_urls.add(url)
            results.append({
                "url": url,
                "success": False,
                "error": failed.get("error", "Unknown error"),
            })

        for url in url_list:
            if url not in seen_urls:
                results.append({
                    "url": url,
                    "success": False,
                    "error": "URL missing from Tavily Extract response",
                })

        return results
    
    async def _scrape_single_url(self, url: str) -> dict:
        """
        Helper function to scrape a single URL and return the result information.
        """
        
        # # Add protocol if missing
        # if not (url.startswith('http://') or url.startswith('https://')):
        #     url = 'https://' + url
        #     logging.info(f"Added https:// protocol to URL: {url}")
            
        logging.info(f"Scraping single URL: {url}")
        
        try:
            # ---------- Firecrawl scrape endpoint ----------
            logging.info(f"Sending request to Firecrawl for URL: {url}")
            async with httpx.AsyncClient() as client:
                headers = {
                    "Authorization": f"Bearer {self.firecrawl_api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "url": url,
                    "formats": ["markdown"],
                    "onlyMainContent": True,  # v2: 只提取主要内容，排除 headers/navs/footers
                }
                
                # Use longer timeout and retry logic for more reliability
                max_retries = 3
                timeout_seconds = 30
                retry_count = 0
                
                while retry_count < max_retries:
                    try:
                        logging.info(f"Sending request to Firecrawl (attempt {retry_count + 1}/{max_retries})")
                        response = await client.post(
                            f"{self.firecrawl_url}/v2/scrape",
                            json=payload,
                            headers=headers,
                            timeout=timeout_seconds,
                        )
                        response.raise_for_status()
                        data = response.json()
                        logging.info(f"Successfully received response from Firecrawl for {url}")
                        break
                    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ReadError) as timeout_err:
                        retry_count += 1
                        logging.warning(f"Request timed out (attempt {retry_count}/{max_retries}): {str(timeout_err)}")
                        if retry_count >= max_retries:
                            raise Exception(f"Request timed out after {max_retries} attempts with {timeout_seconds}s timeout")
                        # Exponential backoff
                        logging.info(f"Waiting {2 ** retry_count}s before retry")
                        await asyncio.sleep(2 ** retry_count)
                    except Exception as e:
                        # Don't retry on non-timeout errors
                        logging.error(f"Error during scraping: {str(e)}")
                        raise e

            # Format the response
            title = data.get("data", {}).get("metadata", {}).get("title", "")
            markdown_content = data.get("data", {}).get("markdown", "")
            logging.info(f"Extracted content from {url}: title='{title}', content length={len(markdown_content)}")
            
            formatted_result = {
                "title": title,
                "url": url,
                "text": markdown_content
            }
            
            # Add metadata if available
            if "metadata" in data.get("data", {}):
                formatted_result["metadata"] = data["data"]["metadata"]
                logging.info(f"Added metadata: {data['data']['metadata'].keys()}")
            
            # Create a simple filename from the URL domain and date
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # Extract domain from URL for the filename
            from urllib.parse import urlparse
            parsed_url = urlparse(url)
            domain = parsed_url.netloc.replace("www.", "")
            
            # Clean up domain for filename
            domain = "".join([c if c.isalnum() else "_" for c in domain])
            safe_filename = f"{timestamp}_{domain}.json"
            
            logging.info(f"Generated filename: {safe_filename}")
            
            # Save results to a file in the /workspace/scrape directory
            scrape_dir = f"{self.workspace_path}/scrape"
            await self._sandbox_make_dir(scrape_dir)
            
            results_file_path = f"{scrape_dir}/{safe_filename}"
            json_content = json.dumps(formatted_result, ensure_ascii=False, indent=2)
            logging.info(f"Saving content to file: {results_file_path}, size: {len(json_content)} bytes")
            
            await self._sandbox_write_file(results_file_path, json_content.encode())
            
            await self._append_research_source({
                "url": url,
                "title": title,
                "method": "firecrawl",
                "extracted_at": datetime.datetime.now().isoformat(),
                "content_length": len(markdown_content),
                "file_path": results_file_path,
            })
            
            return {
                "url": url,
                "success": True,
                "title": title,
                "file_path": results_file_path,
                "content_length": len(markdown_content)
            }
        
        except Exception as e:
            error_message = str(e)
            logging.error(f"Error scraping URL '{url}': {error_message}")
            
            # Create an error result
            return {
                "url": url,
                "success": False,
                "error": error_message
            }

    async def _append_research_source(self, source: dict) -> None:
        """Append source metadata to /workspace/sources-<thread_run_id>.json if available."""
        agent_run_id, _ = get_agent_run_context()
        try:
            if agent_run_id:
                sources_path = f"{self.workspace_path}/sources-{agent_run_id}.json"
                evidence_path = f"{self.workspace_path}/evidence-{agent_run_id}.md"
            else:
                sources_path = f"{self.workspace_path}/sources.json"
                evidence_path = f"{self.workspace_path}/evidence.md"

            existing_sources = []
            try:
                raw = await self._sandbox_read_file(sources_path)
                if raw:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="ignore")
                    existing_sources = json.loads(raw) if raw else []
            except Exception:
                existing_sources = []

            existing_sources.append(source)
            json_content = json.dumps(existing_sources, ensure_ascii=False, indent=2)
            await self._sandbox_write_file(sources_path, json_content.encode())

            try:
                existing_evidence = await self._sandbox_read_file(evidence_path)
                if not existing_evidence:
                    raise FileNotFoundError("evidence.md empty")
            except Exception:
                header = "| Key Claim | Evidence Snippet | Source |\n| --- | --- | --- |\n"
                await self._sandbox_write_file(evidence_path, header.encode())
        except Exception as append_error:
            logging.warning(f"Failed to append research source metadata: {append_error}")

    async def download_images(
        self,
        urls: str,
        folder_name: str = ""
    ) -> ToolResult:
        """
        Batch download images from URLs to the sandbox filesystem.

        This tool downloads multiple images from URLs (typically from web_search results)
        and saves them to /workspace/images/. Use this after web_search to download
        images for further processing or analysis.

        Usage Example:
            {
                "name": "download_images",
                "parameters": {
                    "urls": "https://example.com/img1.jpg,https://example.com/img2.png",
                    "folder_name": "ai_research_images"
                }
            }

        Args:
            urls: Comma-separated image URLs to download. You can pass multiple URLs
                 from web_search results' images field.
                 Example: 'https://example.com/img1.jpg,https://example.com/img2.png'
            folder_name: Optional subfolder name under /workspace/images/.
                        If not provided, a timestamp-based folder will be created.

        Returns:
            ToolResult: Success with download summary (successful/failed counts and paths),
                       or failure with error message.
        """
        try:
            # 确保 sandbox 已初始化
            await self._ensure_sandbox()

            # 解析 URL 参数
            if not urls:
                logging.warning("Download attempt with empty URLs")
                return self.fail_response("Valid image URLs are required.")

            # 切分 URL 字符串为列表
            url_list = [url.strip() for url in urls.split(',') if url.strip()]

            if not url_list:
                logging.warning("No valid URLs found in the input")
                return self.fail_response("No valid image URLs provided.")

            # 限制 URL 数量
            if len(url_list) > MAX_URLS_PER_REQUEST:
                return self.fail_response(
                    f"Too many URLs provided ({len(url_list)}). Maximum is {MAX_URLS_PER_REQUEST}."
                )

            logging.info(f"Processing {len(url_list)} image URLs for download")

            # 创建保存目录
            if not folder_name:
                folder_name = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

            # 清理文件夹名称并限制长度
            folder_name = "".join([c if c.isalnum() or c in ('_', '-') else '_' for c in folder_name])
            folder_name = folder_name[:MAX_FOLDER_NAME_LENGTH]

            images_dir = f"{self.workspace_path}/images/{folder_name}"
            await self._sandbox_make_dir(images_dir)
            logging.info(f"Created images directory: {images_dir}")

            # 使用信号量限制并发下载数
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

            async def download_with_semaphore(url: str, index: int) -> dict:
                async with semaphore:
                    return await self._download_single_image(url, images_dir, index)

            # 并发下载所有图片
            tasks = [download_with_semaphore(url, i) for i, url in enumerate(url_list)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 处理结果
            processed_results = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logging.error(f"Error downloading image {url_list[i]}: {str(result)}")
                    processed_results.append({
                        "url": url_list[i],
                        "success": False,
                        "error": str(result)
                    })
                else:
                    processed_results.append(result)

            # 统计结果
            successful = [r for r in processed_results if r.get("success", False)]
            failed = [r for r in processed_results if not r.get("success", False)]

            # 创建结果消息
            if len(successful) == len(processed_results):
                message = f"Successfully downloaded all {len(processed_results)} images to {images_dir}:"
                for r in successful:
                    message += f"\n- {r.get('file_path')}"
            elif len(successful) > 0:
                message = f"Downloaded {len(successful)} images successfully, {len(failed)} failed."
                message += f"\n\nSuccessful downloads saved to {images_dir}:"
                for r in successful:
                    message += f"\n- {r.get('file_path')}"
                message += "\n\nFailed downloads:"
                for r in failed:
                    message += f"\n- {r.get('url')}: {r.get('error', 'Unknown error')}"
            else:
                error_details = "; ".join([f"{r.get('url')}: {r.get('error', 'Unknown error')}" for r in failed])
                return self.fail_response(f"Failed to download all {len(processed_results)} images. Errors: {error_details}")

            return ToolResult(
                success=True,
                output=message
            )

        except Exception as e:
            error_message = str(e)
            logging.error(f"Error in download_images: {error_message}")
            return self.fail_response(f"Error processing image download request: {error_message[:200]}")

    async def _download_single_image(self, url: str, save_dir: str, index: int) -> dict:
        """
        Helper function to download a single image and save it to the sandbox.

        Args:
            url: Image URL to download
            save_dir: Directory to save the image
            index: Index of the image (for generating unique filenames)

        Returns:
            dict: Result with success status, file path, and error if any
        """
        logging.info(f"Downloading image: {url[:100]}...")

        try:
            # 验证 URL 安全性（防止 SSRF）
            is_safe, error_msg = self._is_safe_url(url)
            if not is_safe:
                return {
                    "url": url,
                    "success": False,
                    "error": error_msg
                }

            parsed_url = urlparse(url)

            # 使用 httpx 下载图片（不重试以节省时间）
            max_retries = 1
            retry_count = 0

            async with httpx.AsyncClient(follow_redirects=True) as client:
                while retry_count < max_retries:
                    try:
                        logging.info(f"Downloading image (attempt {retry_count + 1}/{max_retries})")
                        response = await client.get(
                            url,
                            timeout=IMAGE_DOWNLOAD_TIMEOUT,
                            headers={
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                            }
                        )
                        response.raise_for_status()
                        break
                    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ReadError) as timeout_err:
                        retry_count += 1
                        logging.warning(f"Download timed out (attempt {retry_count}/{max_retries}): {str(timeout_err)}")
                        if retry_count >= max_retries:
                            return {
                                "url": url,
                                "success": False,
                                "error": f"Download timed out after {max_retries} attempts"
                            }
                        # 指数退避
                        await asyncio.sleep(2 ** retry_count)
                    except httpx.HTTPStatusError as http_err:
                        return {
                            "url": url,
                            "success": False,
                            "error": f"HTTP error: {http_err.response.status_code}"
                        }

                # 验证文件大小
                image_content = response.content
                if len(image_content) > MAX_IMAGE_SIZE_BYTES:
                    return {
                        "url": url,
                        "success": False,
                        "error": f"Image too large ({len(image_content)} bytes, max {MAX_IMAGE_SIZE_BYTES})"
                    }

                # 验证 Content-Type 是否为图片
                content_type = response.headers.get("content-type", "").lower()
                is_valid_mimetype = any(mime in content_type for mime in VALID_IMAGE_MIMETYPES)

                # 从 URL 或 Content-Disposition 提取文件名
                filename = None

                # 尝试从 Content-Disposition 获取文件名
                content_disposition = response.headers.get("content-disposition", "")
                if "filename=" in content_disposition:
                    try:
                        filename = content_disposition.split("filename=")[1].strip('"\'')
                        filename = unquote(filename)
                    except Exception:
                        pass

                # 如果没有从 header 获取到，从 URL 路径提取
                if not filename:
                    path = parsed_url.path
                    filename = os.path.basename(unquote(path))

                # 验证文件扩展名
                _, ext = os.path.splitext(filename)
                ext = ext.lower()

                # 如果扩展名无效但 MIME 类型有效，根据 MIME 类型推断扩展名
                if ext not in VALID_IMAGE_EXTENSIONS:
                    if is_valid_mimetype:
                        mime_to_ext = {
                            'image/jpeg': '.jpg',
                            'image/png': '.png',
                            'image/gif': '.gif',
                            'image/webp': '.webp',
                            'image/svg+xml': '.svg',
                            'image/bmp': '.bmp',
                        }
                        for mime, extension in mime_to_ext.items():
                            if mime in content_type:
                                ext = extension
                                break
                        else:
                            ext = '.jpg'  # 默认扩展名
                    else:
                        return {
                            "url": url,
                            "success": False,
                            "error": f"Not a valid image (content-type: {content_type})"
                        }

                # 生成安全的文件名
                base_name = os.path.splitext(filename)[0] if filename else f"image_{index}"
                base_name = "".join([c if c.isalnum() or c in ('_', '-') else '_' for c in base_name])
                base_name = base_name[:50]  # 限制文件名长度

                # 处理文件名冲突（带上限保护）
                final_filename = f"{base_name}{ext}"
                file_path = f"{save_dir}/{final_filename}"

                counter = 1
                while counter < MAX_FILENAME_ATTEMPTS:
                    try:
                        await self._sandbox_read_file(file_path)
                        final_filename = f"{base_name}_{counter}{ext}"
                        file_path = f"{save_dir}/{final_filename}"
                        counter += 1
                    except Exception:
                        break

                if counter >= MAX_FILENAME_ATTEMPTS:
                    return {
                        "url": url,
                        "success": False,
                        "error": "Could not generate unique filename after maximum attempts"
                    }

                # 保存图片到沙箱
                await self._sandbox_write_file(file_path, image_content)

                logging.info(f"Successfully saved image to: {file_path}, size: {len(image_content)} bytes")

                return {
                    "url": url,
                    "success": True,
                    "file_path": file_path,
                    "size_bytes": len(image_content),
                    "content_type": content_type
                }

        except Exception as e:
            error_message = str(e)
            logging.error(f"Error downloading image: {error_message}")
            return {
                "url": url,
                "success": False,
                "error": error_message
            }

    def _is_safe_url(self, url: str) -> tuple:
        """
        Validate URL is safe to request (not internal/private).

        Args:
            url: URL to validate

        Returns:
            tuple: (is_safe: bool, error_message: str)
        """
        try:
            parsed = urlparse(url)

            # Only allow http/https
            if parsed.scheme not in ('http', 'https'):
                return False, "Only HTTP and HTTPS URLs are allowed"

            if not parsed.netloc:
                return False, "Invalid URL format"

            hostname = parsed.netloc.split(':')[0]  # Remove port if present

            # Block localhost variations
            if hostname.lower() in BLOCKED_HOSTS:
                return False, "Localhost URLs are not allowed"

            # Resolve hostname and check if IP is private
            try:
                ip = ipaddress.ip_address(socket.gethostbyname(hostname))
                if ip.is_private or ip.is_loopback or ip.is_reserved:
                    return False, "Private/internal IP addresses are not allowed"
            except (socket.gaierror, ValueError):
                pass  # Could not resolve, allow (will fail on actual request)

            return True, ""
        except Exception as e:
            return False, f"URL validation error: {str(e)}"
