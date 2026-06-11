from __future__ import annotations

import logging
import warnings
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from langchain_community.utils.openai import is_openai_v1
from langchain_core.embeddings import Embeddings
from langchain_core.pydantic_v1 import BaseModel, Field, root_validator
from langchain_core.utils import get_from_dict_or_env, get_pydantic_field_names
from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from chatchat.server.utils import run_in_thread_pool

logger = logging.getLogger(__name__)


def _create_retry_decorator(embeddings: LocalAIEmbeddings) -> Callable[[Any], Any]:
    import openai

    min_seconds = 4
    max_seconds = 10
    # Wait 2^x * 1 second between each retry starting with
    # 4 seconds, then up to 10 seconds, then 10 seconds afterwards
    return retry(
        reraise=True,
        stop=stop_after_attempt(embeddings.max_retries),
        wait=wait_exponential(multiplier=1, min=min_seconds, max=max_seconds),
        retry=(
            retry_if_exception_type(openai.Timeout)
            | retry_if_exception_type(openai.APIError)
            | retry_if_exception_type(openai.APIConnectionError)
            | retry_if_exception_type(openai.RateLimitError)
            | retry_if_exception_type(openai.InternalServerError)
        ),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )


def _async_retry_decorator(embeddings: LocalAIEmbeddings) -> Any:
    import openai

    min_seconds = 4
    max_seconds = 10
    # Wait 2^x * 1 second between each retry starting with
    # 4 seconds, then up to 10 seconds, then 10 seconds afterwards
    async_retrying = AsyncRetrying(
        reraise=True,
        stop=stop_after_attempt(embeddings.max_retries),
        wait=wait_exponential(multiplier=1, min=min_seconds, max=max_seconds),
        retry=(
            retry_if_exception_type(openai.Timeout)
            | retry_if_exception_type(openai.APIError)
            | retry_if_exception_type(openai.APIConnectionError)
            | retry_if_exception_type(openai.RateLimitError)
            | retry_if_exception_type(openai.InternalServerError)
        ),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )

    def wrap(func: Callable) -> Callable:
        async def wrapped_f(*args: Any, **kwargs: Any) -> Callable:
            async for _ in async_retrying:
                return await func(*args, **kwargs)
            raise AssertionError("this is unreachable")

        return wrapped_f

    return wrap


# https://stackoverflow.com/questions/76469415/getting-embeddings-of-length-1-from-langchain-openaiembeddings
def _check_response(response: dict) -> dict:
    if any([len(d.embedding) == 1 for d in response.data]):
        import openai

        raise openai.APIError("LocalAI API returned an empty embedding")
    return response


def embed_with_retry(embeddings: LocalAIEmbeddings, **kwargs: Any) -> Any:
    """Use tenacity to retry the embedding call."""
    retry_decorator = _create_retry_decorator(embeddings)

    @retry_decorator
    def _embed_with_retry(**kwargs: Any) -> Any:
        response = embeddings.client.create(**kwargs)
        return _check_response(response)

    return _embed_with_retry(**kwargs)


async def async_embed_with_retry(embeddings: LocalAIEmbeddings, **kwargs: Any) -> Any:
    """Use tenacity to retry the embedding call."""

    @_async_retry_decorator(embeddings)
    async def _async_embed_with_retry(**kwargs: Any) -> Any:
        response = await embeddings.async_client.create(**kwargs)
        return _check_response(response)

    return await _async_embed_with_retry(**kwargs)


class LocalAIEmbeddings(BaseModel, Embeddings):
    """LocalAI embedding models.

    Since LocalAI and OpenAI have 1:1 compatibility between APIs, this class
    uses the ``openai`` Python package's ``openai.Embedding`` as its client.
    Thus, you should have the ``openai`` python package installed, and defeat
    the environment variable ``OPENAI_API_KEY`` by setting to a random string.
    You also need to specify ``OPENAI_API_BASE`` to point to your LocalAI
    service endpoint.

    Example:
        .. code-block:: python

            from langchain_community.embeddings import LocalAIEmbeddings
            openai = LocalAIEmbeddings(
                openai_api_key="random-string",
                openai_api_base="http://localhost:8080"
            )

    """

    client: Any = Field(default=None, exclude=True)  #: :meta private:
    async_client: Any = Field(default=None, exclude=True)  #: :meta private:
    model: str = "text-embedding-ada-002"
    deployment: str = model
    openai_api_version: Optional[str] = None
    openai_api_base: Optional[str] = Field(default=None, alias="base_url")
    # to support explicit proxy for LocalAI
    openai_proxy: Optional[str] = None
    embedding_ctx_length: int = 8191
    """The maximum number of tokens to embed at once."""
    openai_api_key: Optional[str] = Field(default=None, alias="api_key")
    openai_organization: Optional[str] = Field(default=None, alias="organization")
    allowed_special: Union[Literal["all"], Set[str]] = set()
    disallowed_special: Union[Literal["all"], Set[str], Sequence[str]] = "all"
    chunk_size: int = 1000
    """Maximum number of texts to embed in each batch"""
    max_retries: int = 3
    """Maximum number of retries to make when generating."""
    request_timeout: Union[float, Tuple[float, float], Any, None] = Field(
        default=None, alias="timeout"
    )
    """Timeout in seconds for the LocalAI request."""
    headers: Any = None
    show_progress_bar: bool = False
    """Whether to show a progress bar when embedding."""
    model_kwargs: Dict[str, Any] = Field(default_factory=dict)
    """Holds any model parameters valid for `create` call not explicitly specified."""

    class Config:
        """Configuration for this pydantic object."""

        allow_population_by_field_name = True

    @root_validator(pre=True)
    def build_extra(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """Build extra kwargs from additional params that were passed in."""
        all_required_field_names = get_pydantic_field_names(cls)
        extra = values.get("model_kwargs", {})
        for field_name in list(values):
            if field_name in extra:
                raise ValueError(f"Found {field_name} supplied twice.")
            if field_name not in all_required_field_names:
                warnings.warn(
                    f"""WARNING! {field_name} is not default parameter.
                    {field_name} was transferred to model_kwargs.
                    Please confirm that {field_name} is what you intended."""
                )
                extra[field_name] = values.pop(field_name)

        invalid_model_kwargs = all_required_field_names.intersection(extra.keys())
        if invalid_model_kwargs:
            raise ValueError(
                f"Parameters {invalid_model_kwargs} should be specified explicitly. "
                f"Instead they were passed in as part of `model_kwargs` parameter."
            )

        values["model_kwargs"] = extra
        return values

    @root_validator()
    def validate_environment(cls, values: Dict) -> Dict:
        """Validate that api key and python package exists in environment."""
        values["openai_api_key"] = get_from_dict_or_env(
            values, "openai_api_key", "OPENAI_API_KEY"
        )
        values["openai_api_base"] = get_from_dict_or_env(
            values,
            "openai_api_base",
            "OPENAI_API_BASE",
            default="",
        )
        values["openai_proxy"] = get_from_dict_or_env(
            values,
            "openai_proxy",
            "OPENAI_PROXY",
            default="",
        )

        default_api_version = ""
        values["openai_api_version"] = get_from_dict_or_env(
            values,
            "openai_api_version",
            "OPENAI_API_VERSION",
            default=default_api_version,
        )
        values["openai_organization"] = get_from_dict_or_env(
            values,
            "openai_organization",
            "OPENAI_ORGANIZATION",
            default="",
        )
        try:
            import openai

            if is_openai_v1():
                client_params = {
                    "api_key": values["openai_api_key"],
                    "organization": values["openai_organization"],
                    "base_url": values["openai_api_base"],
                    "timeout": values["request_timeout"],
                    "max_retries": values["max_retries"],
                }

                if not values.get("client"):
                    values["client"] = openai.OpenAI(**client_params).embeddings
                if not values.get("async_client"):
                    values["async_client"] = openai.AsyncOpenAI(
                        **client_params
                    ).embeddings
            elif not values.get("client"):
                values["client"] = openai.Embedding
            else:
                pass
        except ImportError:
            raise ImportError(
                "Could not import openai python package. "
                "Please install it with `pip install openai`."
            )
        return values

    @property
    def _invocation_params(self) -> Dict:
        openai_args = {
            "model": self.model,
            "timeout": self.request_timeout,
            "extra_headers": self.headers,
            **self.model_kwargs,
        }
        if self.openai_proxy:
            import openai

            openai.proxy = {
                "http": self.openai_proxy,
                "https": self.openai_proxy,
            }  # type: ignore[assignment]  # noqa: E501
        return openai_args

    def _embedding_func(self, text: str, *, engine: str) -> List[float]:
        """Call out to LocalAI's embedding endpoint."""
        # handle large input text
        if self.model.endswith("001"):
            # See: https://github.com/openai/openai-python/issues/418#issuecomment-1525939500
            # replace newlines, which can negatively affect performance.
            text = text.replace("\n", " ")
        return (
            embed_with_retry(
                self,
                input=[text],
                **self._invocation_params,
            )
            .data[0]
            .embedding
        )

    async def _aembedding_func(self, text: str, *, engine: str) -> List[float]:
        """Call out to LocalAI's embedding endpoint."""
        # handle large input text
        if self.model.endswith("001"):
            # See: https://github.com/openai/openai-python/issues/418#issuecomment-1525939500
            # replace newlines, which can negatively affect performance.
            text = text.replace("\n", " ")
        return (
            (
                await async_embed_with_retry(
                    self,
                    input=[text],
                    **self._invocation_params,
                )
            )
            .data[0]
            .embedding
        )

    def embed_documents(
        self, texts: List[str], chunk_size: Optional[int] = 0
    ) -> List[List[float]]:
        """Call out to LocalAI's embedding endpoint for embedding search docs.

        Args:
            texts: The list of texts to embed.
            chunk_size: The chunk size of embeddings. If None, will use the chunk size
                specified by the class.

        Returns:
            List of embeddings, one for each text.
        """

        # call _embedding_func for each text with multithreads
        def task(seq, text):
            return (seq, self._embedding_func(text, engine=self.deployment))

        params = [{"seq": i, "text": text} for i, text in enumerate(texts)]
        result = list(run_in_thread_pool(func=task, params=params))
        result = sorted(result, key=lambda x: x[0])
        return [x[1] for x in result]

    def embed_batch_documents(self, texts: List[str], chunk_size: Optional[int] = None) -> List[List[float]]:
        """
        按批次调用向量模型API，使用线程池并行处理多个批次。
        
        Args:
            texts: 要嵌入的文本列表
            chunk_size: 每批处理的文本数量
            
        Returns:
            List[List[float]]: 嵌入向量列表，顺序与输入文本列表相同
        """
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from chatchat.utils import build_logger
        logger = build_logger()
        
        # 使用传入的chunk_size或默认值
        _chunk_size = chunk_size or self.chunk_size or 20
        
        # 预处理文本
        processed_texts = []
        for text in texts:
            if self.model.endswith("001"):
                text = text.replace("\n", " ")
            processed_texts.append(text)
        
        total_texts = len(processed_texts)
        logger.info(f"开始处理总计 {total_texts} 个文本的向量嵌入，批次大小: {_chunk_size}")
        
        # 预分配结果列表，确保结果顺序与输入顺序一致
        all_embeddings = [None] * total_texts
        start_time_total = time.time()
        
        # 定义批次处理任务
        def process_batch(batch_idx, batch_texts):
            start_idx = batch_idx * _chunk_size
            end_idx = min(start_idx + len(batch_texts), total_texts)
            batch_indices = list(range(start_idx, end_idx))
            
            start_time_batch = time.time()
            logger.info(f"开始处理批次 {batch_idx+1}/{len(batches)}，文本索引 {start_idx} 到 {end_idx-1} (共 {len(batch_texts)} 个)")
            
            try:
                # 直接批量调用API
                response = embed_with_retry(
                    self,
                    input=batch_texts,
                    **self._invocation_params,
                )
                
                # 提取嵌入结果
                batch_embeddings = [item.embedding for item in response.data]
                
                # 验证返回的向量数量是否正确
                if len(batch_embeddings) != len(batch_texts):
                    raise ValueError(f"模型返回的向量数量 ({len(batch_embeddings)}) 与请求数量 ({len(batch_texts)}) 不匹配")
                
                # 计算批次性能
                elapsed_batch = time.time() - start_time_batch
                rate = len(batch_texts) / elapsed_batch if elapsed_batch > 0 else 0
                logger.info(f"批次 {batch_idx+1} 完成，耗时: {elapsed_batch:.2f}秒, "
                        f"速率: {rate:.2f} 文本/秒")
                
                # 返回批次索引和结果
                return batch_idx, batch_indices, batch_embeddings
                
            except Exception as e:
                logger.error(f"批次 {batch_idx+1} 处理失败: {str(e)}")
                raise RuntimeError(f"批次 {batch_idx+1} 处理失败: {str(e)}") from e
        
        # 准备批次
        batches = []
        for i in range(0, total_texts, _chunk_size):
            batch_end = min(i + _chunk_size, total_texts)
            batches.append((i // _chunk_size, processed_texts[i:batch_end]))
        
        with ThreadPoolExecutor() as executor:
            # 提交所有任务
            future_to_batch = {
                executor.submit(process_batch, batch_idx, batch_texts): batch_idx
                for batch_idx, batch_texts in batches
            }
            
            try:
                # 收集结果
                for future in as_completed(future_to_batch):
                    batch_idx, batch_indices, batch_embeddings = future.result()
                    
                    for idx, embedding in zip(batch_indices, batch_embeddings):
                        all_embeddings[idx] = embedding
                    
                    logger.info(f"已完成批次 {batch_idx+1}/{len(batches)} 的结果收集")
                    
            except Exception as e:
                for f in future_to_batch:
                    f.cancel()
                raise RuntimeError(f"部分文本嵌入失败,终止所有任务: {str(e)}") from e
            
        # 检查是否所有文本都成功处理 TODO 提示不需要这一步检查
        if None in all_embeddings:
            # failed_indices = [i for i, emb in enumerate(all_embeddings) if emb is None]
            raise RuntimeError(f"部分文本嵌入失败，失败的索引")
        
        # 计算总体性能
        total_elapsed = time.time() - start_time_total
        total_rate = total_texts / total_elapsed if total_elapsed > 0 else 0
        logger.info(f"全部 {total_texts} 个文本嵌入完成，总耗时: {total_elapsed:.2f}秒, "
                f"平均速率: {total_rate:.2f} 文本/秒")
        
        return all_embeddings
    async def aembed_documents(
        self, texts: List[str], chunk_size: Optional[int] = 0
    ) -> List[List[float]]:
        """Call out to LocalAI's embedding endpoint async for embedding search docs.

        Args:
            texts: The list of texts to embed.
            chunk_size: The chunk size of embeddings. If None, will use the chunk size
                specified by the class.

        Returns:
            List of embeddings, one for each text.
        """
        embeddings = []
        for text in texts:
            response = await self._aembedding_func(text, engine=self.deployment)
            embeddings.append(response)
        return embeddings

    def embed_query(self, text: str) -> List[float]:
        """Call out to LocalAI's embedding endpoint for embedding query text.

        Args:
            text: The text to embed.

        Returns:
            Embedding for the text.
        """
        embedding = self._embedding_func(text, engine=self.deployment)
        return embedding

    async def aembed_query(self, text: str) -> List[float]:
        """Call out to LocalAI's embedding endpoint async for embedding query text.

        Args:
            text: The text to embed.

        Returns:
            Embedding for the text.
        """
        embedding = await self._aembedding_func(text, engine=self.deployment)
        return embedding
