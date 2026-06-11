"""
Conversation thread management system for AgentPress.

This module provides comprehensive conversation management, including:
- Thread creation and persistence
- Message handling with support for text and images
- Tool registration and execution
- LLM interaction with streaming support
- Error handling and cleanup
- Context summarization to manage token limits
"""

import json
from typing import List, Dict, Any, Optional, Type, Union, AsyncGenerator, Literal, cast
from services.llm import make_llm_api_call
from agentpress.tool import Tool
from agentpress.tool_registry import ToolRegistry
from agentpress.context_manager import ContextManager
from agentpress.response_processor import (
    ResponseProcessor,
    ProcessorConfig
)
from services.postgresql import DBConnection
from utils.logger import logger
# langfuse.client removed in v3 — use Any for dead fufanmanus type annotations
try:
    from langfuse._client.span import LangfuseSpan as StatefulTraceClient, LangfuseGeneration as StatefulGenerationClient  # type: ignore
except Exception:
    from typing import Any
    StatefulTraceClient = Any  # type: ignore
    StatefulGenerationClient = Any  # type: ignore
from services.langfuse import langfuse
import datetime
from litellm.utils import token_counter # type: ignore

# Type alias for tool choice
ToolChoice = Literal["auto", "required", "none"]

class ThreadManager:
    """
    管理与大型语言模型的对话线程以及工具的执行过程。
    提供全面的对话管理功能，包括消息线程处理、工具注册以及与语言模型的交互，支持标准和基于 XML 的工具执行模式。 
    """

    def __init__(self, trace: Optional[StatefulTraceClient] = None, is_agent_builder: bool = False, target_agent_id: Optional[str] = None, agent_config: Optional[dict] = None): # type: ignore
        """Initialize ThreadManager.

        Args:
            trace: Optional trace client for logging
            is_agent_builder: Whether this is an agent builder session
            target_agent_id: ID of the agent being built (if in agent builder mode)
            agent_config: Optional agent configuration with version information
        """
        self.db = DBConnection()
        self.tool_registry = ToolRegistry()
        self.trace = trace
        self.is_agent_builder = is_agent_builder
        self.target_agent_id = target_agent_id
        self.agent_config = agent_config
        if not self.trace:
            self.trace = langfuse.trace(name="anonymous:thread_manager")
        self.response_processor = ResponseProcessor(
            tool_registry=self.tool_registry,
            add_message_callback=self.add_message,
            trace=self.trace,
            is_agent_builder=self.is_agent_builder,
            target_agent_id=self.target_agent_id,
            agent_config=self.agent_config
        )
        self.context_manager = ContextManager()

    def add_tool(self, tool_class: Type[Tool], function_names: Optional[List[str]] = None, **kwargs):
        """Add a tool to the ThreadManager."""
        self.tool_registry.register_tool(tool_class, function_names, **kwargs)

    async def create_thread(
        self,
        account_id: Optional[str] = None,
        project_id: Optional[str] = None,
        is_public: bool = False,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Create a new thread in the database.

        Args:
            account_id: Optional account ID for the thread. If None, creates an orphaned thread.
            project_id: Optional project ID for the thread. If None, creates an orphaned thread.
            is_public: Whether the thread should be public (defaults to False).
            metadata: Optional metadata dictionary for additional thread context.

        Returns:
            The thread_id of the newly created thread.

        Raises:
            Exception: If thread creation fails.
        """
        logger.debug(f"Creating new thread (account_id: {account_id}, project_id: {project_id}, is_public: {is_public})")
        client = await self.db.client

        # Prepare data for thread creation
        thread_data = {
            'is_public': is_public,
            'metadata': metadata or {}
        }

        # Add optional fields only if provided
        if account_id:
            thread_data['account_id'] = account_id
        if project_id:
            thread_data['project_id'] = project_id

        try:
            # Insert the thread and get the thread_id
            result = await client.table('threads').insert(thread_data).execute()
            
            if result.data and len(result.data) > 0 and isinstance(result.data[0], dict) and 'thread_id' in result.data[0]:
                thread_id = result.data[0]['thread_id']
                logger.info(f"Successfully created thread: {thread_id}")
                return thread_id
            else:
                logger.error(f"Thread creation failed or did not return expected data structure. Result data: {result.data}")
                raise Exception("Failed to create thread: no thread_id returned")

        except Exception as e:
            logger.error(f"Failed to create thread: {str(e)}", exc_info=True)
            raise Exception(f"Thread creation failed: {str(e)}")

    async def add_message(
        self,
        thread_id: str,
        type: str,
        content: Union[Dict[str, Any], List[Any], str],
        is_llm_message: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        agent_id: Optional[str] = None,
        agent_version_id: Optional[str] = None
    ):
        """Add a message to the thread in the database.

        Args:
            thread_id: The ID of the thread to add the message to.
            type: The type of the message (e.g., 'text', 'image_url', 'tool_call', 'tool', 'user', 'assistant').
            content: The content of the message. Can be a dictionary, list, or string.
                     It will be stored as JSONB in the database.
            is_llm_message: Flag indicating if the message originated from the LLM.
                            Defaults to False (user message).
            metadata: Optional dictionary for additional message metadata.
                      Defaults to None, stored as an empty JSONB object if None.
            agent_id: Optional ID of the agent associated with this message.
            agent_version_id: Optional ID of the specific agent version used.
        """
        logger.debug(f"Adding message of type '{type}' to thread {thread_id} (agent: {agent_id}, version: {agent_version_id})")
        client = await self.db.client

        # Prepare data for insertion
        data_to_insert = {
            'thread_id': thread_id,
            'type': type,
            'content': content,
            'is_llm_message': is_llm_message,
            'metadata': metadata or {},
        }
        
        # Add agent information if provided
        if agent_id:
            data_to_insert['agent_id'] = agent_id
        if agent_version_id:
            data_to_insert['agent_version_id'] = agent_version_id

        try:
            # Insert the message and get the inserted row data including the id
            result = await client.table('messages').insert(data_to_insert)
            logger.info(f"Successfully added message to thread {thread_id}")

            if result.data and len(result.data) > 0 and isinstance(result.data[0], dict) and 'message_id' in result.data[0]:
                return result.data[0]
            else:
                logger.error(f"Insert operation failed or did not return expected data structure for thread {thread_id}. Result data: {result.data}")
                return None
        except Exception as e:
            logger.error(f"Failed to add message to thread {thread_id}: {str(e)}", exc_info=True)
            raise

    async def get_llm_messages(self, thread_id: str) -> List[Dict[str, Any]]:
        """Get all messages for a thread from events table.

        This method fetches messages from the events table and formats them
        for LLM consumption.

        Args:
            thread_id: The ID of the thread to get messages for.

        Returns:
            List of message objects.
        """
        logger.debug(f"Getting messages for thread {thread_id} from events table")
        client = await self.db.client

        try:
            # Fetch events in batches of 1000 to avoid overloading the database
            all_events = []
            batch_size = 1000
            offset = 0
            
            while True:
                # 从 events 表获取消息，按时间戳排序
                result = await client.table('events').select(
                    'id, author, content, timestamp'
                ).eq('session_id', thread_id).in_(
                    'author', ['user', 'assistant']
                ).order('timestamp').range(offset, offset + batch_size - 1).execute()
                
                if not result.data or len(result.data) == 0:
                    break
                    
                all_events.extend(result.data)
                
                # If we got fewer than batch_size records, we've reached the end
                if len(result.data) < batch_size:
                    break
                    
                offset += batch_size
            
            # Parse the returned data and convert to LLM message format
            if not all_events:
                return []

            # Convert events to LLM message format
            messages = []
            for event in all_events:
                try:
                    # 确保event是字典格式
                    if hasattr(event, '__dict__'):
                        event = dict(event)
                    
                    # 解析事件内容
                    content = event.get('content', {})
                    if isinstance(content, str):
                        try:
                            content = json.loads(content)
                        except json.JSONDecodeError:
                            # 如果不是JSON，当作纯文本处理
                            content = {"content": content}
                    
                    # 构建LLM消息格式
                    message = {
                        "role": event.get('author', 'user'),
                        "message_id": event.get('id'),
                        "timestamp": event.get('timestamp')
                    }
                    
                    # 处理timestamp字段，确保datetime对象被转换为字符串
                    if message.get('timestamp') and hasattr(message['timestamp'], 'isoformat'):
                        message['timestamp'] = message['timestamp'].isoformat()
                    
                    # 处理内容格式
                    if isinstance(content, dict):
                        # 如果content是对象，提取文本内容
                        if 'content' in content:
                            message["content"] = content['content']
                        else:
                            # 如果没有content字段，将整个对象转为字符串
                            message["content"] = json.dumps(content)
                    else:
                        message["content"] = str(content)
                    
                    messages.append(message)
                    
                except Exception as e:
                    logger.error(f"Failed to parse event {event.get('id')}: {e}")
                    continue

            logger.debug(f"Retrieved {len(messages)} messages from events table for thread {thread_id}")
            return messages

        except Exception as e:
            logger.error(f"Failed to get messages for thread {thread_id}: {str(e)}", exc_info=True)
            return []

    async def run_thread(
        self,
        thread_id: str,
        system_prompt: Dict[str, Any],
        stream: bool = True,
        temporary_message: Optional[Dict[str, Any]] = None,
        llm_model: str = "gpt-5",
        llm_temperature: float = 0,
        llm_max_tokens: Optional[int] = None,
        processor_config: Optional[ProcessorConfig] = None,
        tool_choice: ToolChoice = "auto",
        native_max_auto_continues: int = 25,
        max_xml_tool_calls: int = 0,
        enable_thinking: Optional[bool] = False,
        reasoning_effort: Optional[str] = 'low',
        enable_context_manager: bool = True,
        generation: Optional[StatefulGenerationClient] = None,
    ) -> Union[Dict[str, Any], AsyncGenerator]:
        """Run a conversation thread with LLM integration and tool execution.

        Args:
            thread_id: The ID of the thread to run
            system_prompt: System message to set the assistant's behavior
            stream: Use streaming API for the LLM response
            temporary_message: Optional temporary user message for this run only
            llm_model: The name of the LLM model to use
            llm_temperature: Temperature parameter for response randomness (0-1)
            llm_max_tokens: Maximum tokens in the LLM response
            processor_config: Configuration for the response processor
            tool_choice: Tool choice preference ("auto", "required", "none")
            native_max_auto_continues: Maximum number of automatic continuations when
                                      finish_reason="tool_calls" (0 disables auto-continue)
            max_xml_tool_calls: Maximum number of XML tool calls to allow (0 = no limit)
            enable_thinking: Whether to enable thinking before making a decision
            reasoning_effort: The effort level for reasoning
            enable_context_manager: Whether to enable automatic context summarization.

        Returns:
            An async generator yielding response chunks or error dict
        """

        print(f"Starting thread execution for thread {thread_id}")
        print(f"Using model: {llm_model}")
        print(f"Parameters: model={llm_model}, temperature={llm_temperature}, max_tokens={llm_max_tokens}")
        print(f"Auto-continue: max={native_max_auto_continues}, XML tool limit={max_xml_tool_calls}")

        # Log model info
        print(f"🤖 Thread {thread_id}: Using model {llm_model}")

        # Ensure processor_config is not None
        config = processor_config or ProcessorConfig()

        # Apply max_xml_tool_calls if specified and not already set in config
        if max_xml_tool_calls > 0 and not config.max_xml_tool_calls:
            config.max_xml_tool_calls = max_xml_tool_calls

        # Create a working copy of the system prompt to potentially modify
        working_system_prompt = system_prompt.copy()

        # Add XML tool calling instructions to system prompt if requested
        if config.xml_tool_calling:
            openapi_schemas = self.tool_registry.get_openapi_schemas()
            usage_examples = self.tool_registry.get_usage_examples()
            
            if openapi_schemas:
                # Convert schemas to JSON string
                schemas_json = json.dumps(openapi_schemas, indent=2)
                
                # Build usage examples section if any exist
                usage_examples_section = ""
                if usage_examples:
                    usage_examples_section = "\n\nUsage Examples:\n"
                    for func_name, example in usage_examples.items():
                        usage_examples_section += f"\n{func_name}:\n{example}\n"
                
                examples_content = f"""
In this environment you have access to a set of tools you can use to answer the user's question.

You can invoke functions by writing a <function_calls> block like the following as part of your reply to the user:

<function_calls>
<invoke name="function_name">
<parameter name="param_name">param_value</parameter>
...
</invoke>
</function_calls>

String and scalar parameters should be specified as-is, while lists and objects should use JSON format.

Here are the functions available in JSON Schema format:

```json
{schemas_json}
```

When using the tools:
- Use the exact function names from the JSON schema above
- Include all required parameters as specified in the schema
- Format complex data (objects, arrays) as JSON strings within the parameter tags
- Boolean values should be "true" or "false" (lowercase)
{usage_examples_section}"""

                # # Save examples content to a file
                # try:
                #     with open('xml_examples.txt', 'w') as f:
                #         f.write(examples_content)
                #     logger.debug("Saved XML examples to xml_examples.txt")
                # except Exception as e:
                #     logger.error(f"Failed to save XML examples to file: {e}")

                system_content = working_system_prompt.get('content')

                if isinstance(system_content, str):
                    working_system_prompt['content'] += examples_content
                    logger.debug("Appended XML examples to string system prompt content.")
                elif isinstance(system_content, list):
                    appended = False
                    for item in working_system_prompt['content']: # Modify the copy
                        if isinstance(item, dict) and item.get('type') == 'text' and 'text' in item:
                            item['text'] += examples_content
                            logger.debug("Appended XML examples to the first text block in list system prompt content.")
                            appended = True
                            break
                    if not appended:
                        logger.warning("System prompt content is a list but no text block found to append XML examples.")
                else:
                    logger.warning(f"System prompt content is of unexpected type ({type(system_content)}), cannot add XML examples.")
        
        # Control whether we need to auto-continue due to tool_calls finish reason
        auto_continue = True
        auto_continue_count = 0
        
        # Shared state for continuous streaming across auto-continues
        continuous_state = {
            'accumulated_content': '',
            'thread_run_id': None
        }

        # Define inner function to handle a single run
        async def _run_once(temp_msg=None):
            try:
                print(f"🔄 ===== _run_once 开始执行 =====")
                print(f"  📋 temp_msg: {temp_msg}")
                
                # Ensure config is available in this scope
                nonlocal config
                # Note: config is now guaranteed to exist due to check above
                print(f"  ✅ config 获取成功")

                # 1. Get messages from thread for LLM call
                print(f"  🔄 开始获取消息...")
                messages = await self.get_llm_messages(thread_id)
                print(f"  ✅ 消息获取完成")
                print(f"messages: {messages}")
                # 2. Check token count before proceeding
                print(f"  🔄 开始检查token数量...")
                token_count = 0
                try:
                    # Use the potentially modified working_system_prompt for token counting
                    print(f"    📊 计算token数量...")
                    token_count = token_counter(model=llm_model, messages=[working_system_prompt] + messages)
                    token_threshold = self.context_manager.token_threshold
                    print(f"    ✅ Token数量: {token_count}/{token_threshold} ({(token_count/token_threshold)*100:.1f}%)")
                    logger.info(f"Thread {thread_id} token count: {token_count}/{token_threshold} ({(token_count/token_threshold)*100:.1f}%)")

                except Exception as e:
                    print(f"    ❌ Token计算失败: {str(e)}")
                    logger.error(f"Error counting tokens or summarizing: {str(e)}")

                # 3. Prepare messages for LLM call + add temporary message if it exists
                print(f"  🔄 开始准备消息...")
                # Use the working_system_prompt which may contain the XML examples
                prepared_messages = [working_system_prompt]
                print(f"    ✅ 系统提示词已添加")

                # Find the last user message index
                print(f"    🔍 查找最后用户消息索引...")
                last_user_index = -1
                for i, msg in enumerate(messages):
                    if isinstance(msg, dict) and msg.get('role') == 'user':
                        last_user_index = i
                print(f"    📍 最后用户消息索引: {last_user_index}")

                # Insert temporary message before the last user message if it exists
                if temp_msg and last_user_index >= 0:
                    print(f"    📝 在最后用户消息前插入临时消息...")
                    prepared_messages.extend(messages[:last_user_index])
                    prepared_messages.append(temp_msg)
                    prepared_messages.extend(messages[last_user_index:])
                    logger.debug("Added temporary message before the last user message")
                else:
                    # If no user message or no temporary message, just add all messages
                    print(f"    📝 添加所有消息...")
                    prepared_messages.extend(messages)
                    if temp_msg:
                        prepared_messages.append(temp_msg)
                        logger.debug("Added temporary message to the end of prepared messages")
                print(f"    ✅ 消息准备完成，共 {len(prepared_messages)} 条消息")

                # Add partial assistant content for auto-continue context (without saving to DB)
                print(f"  🔄 检查是否需要添加部分助手内容...")
                if auto_continue_count > 0 and continuous_state.get('accumulated_content'):
                    print(f"    📝 添加部分助手内容...")
                    partial_content = continuous_state.get('accumulated_content', '')
                    
                    # Create temporary assistant message with just the text content
                    temporary_assistant_message = {
                        "role": "assistant",
                        "content": partial_content
                    }
                    prepared_messages.append(temporary_assistant_message)
                    print(f"    ✅ 已添加部分助手内容，长度: {len(partial_content)} 字符")
                    logger.info(f"Added temporary assistant message with {len(partial_content)} chars for auto-continue context")
                else:
                    print(f"    ⏭️ 跳过添加部分助手内容")

                # # 4. Prepare tools for LLM call
                # openapi_tool_schemas = None
                # if config.native_tool_calling:
                #     openapi_tool_schemas = self.tool_registry.get_openapi_schemas()
                #     logger.debug(f"Retrieved {len(openapi_tool_schemas) if openapi_tool_schemas else 0} OpenAPI tool schemas")

                # # print(f"\n\n\n\n prepared_messages: {prepared_messages}\n\n\n\n")

                # prepared_messages = self.context_manager.compress_messages(prepared_messages, llm_model)

                # 5. Make LLM API call
                print("Making LLM API call")
                try:
                    if generation:
                        generation.update(
                            input=prepared_messages,
                            start_time=datetime.datetime.now(datetime.timezone.utc),
                            model=llm_model,
                            model_parameters={
                              "max_tokens": llm_max_tokens,
                              "temperature": llm_temperature,
                              "enable_thinking": enable_thinking,
                              "reasoning_effort": reasoning_effort,
                              "tool_choice": tool_choice,
                              # "tools": openapi_tool_schemas,
                            }
                        )

                    print(f"我要开始执行 make_llm_api_call 了！！！！！！！！！！！！！")
                    print(f"🔍 检查 prepared_messages 类型:")
                    for i, msg in enumerate(prepared_messages):
                        print(f"  [{i}] 类型: {type(msg)}, 内容: {str(msg)[:100]}...")
                        if hasattr(msg, '__dict__'):
                            print(f"    ⚠️ 发现非字典对象，转换为字典")
                            prepared_messages[i] = dict(msg)
                    print(f"🔍 检查 stream 类型:")
                    print(f"  stream: {stream}")
                    llm_response = await make_llm_api_call(
                        prepared_messages, # Pass the potentially modified messages
                        llm_model,
                        temperature=llm_temperature,
                        max_tokens=llm_max_tokens,
                        # tools=openapi_tool_schemas,
                        tool_choice=tool_choice if config.native_tool_calling else "none",
                        stream=stream,
                        enable_thinking=enable_thinking,
                        reasoning_effort=reasoning_effort
                    )

                    print(f"llm_response: {llm_response}")
                    logger.debug("Successfully received raw LLM API response stream/object")

                except Exception as e:
                    logger.error(f"Failed to make LLM API call: {str(e)}", exc_info=True)
                    raise

                # 6. Process LLM response using the ResponseProcessor
                if stream:
                    print("我进入的是 流式！！！")
                    logger.debug("Processing streaming response")

                    async def fake_response_generator():
                        # 1. 开始事件
                        yield {
                            "sequence": 0,
                            "message_id": "msg_001",
                            "thread_id": "thread_123",
                            "type": "status",
                            "is_llm_message": False,
                            "content": '{"status_type": "thread_run_start", "thread_run_id": "run_456"}',
                            "metadata": '{"thread_run_id": "run_456"}',
                            "created_at": "2025-08-25T10:30:00Z",
                            "updated_at": "2025-08-25T10:30:00Z"
                        }
                        
                        yield {
                            "sequence": 1,
                            "message_id": "msg_002", 
                            "thread_id": "thread_123",
                            "type": "status",
                            "is_llm_message": False,
                            "content": '{"status_type": "assistant_response_start"}',
                            "metadata": '{"thread_run_id": "run_456"}',
                            "created_at": "2025-08-25T10:30:01Z",
                            "updated_at": "2025-08-25T10:30:01Z"
                        }
                        
                        # 2. 内容块 (流式输出)
                        yield {
                            "sequence": 2,
                            "message_id": None,  # 内容块不保存到数据库
                            "thread_id": "thread_123",
                            "type": "assistant",
                            "is_llm_message": True,
                            "content": '{"role": "assistant", "content": "你好！我是Suna.so"}',
                            "metadata": '{"stream_status": "chunk", "thread_run_id": "run_456"}',
                            "created_at": "2025-08-25T10:30:02Z",
                            "updated_at": "2025-08-25T10:30:02Z"
                        }
                        
                        yield {
                            "sequence": 3,
                            "message_id": None,
                            "thread_id": "thread_123", 
                            "type": "assistant",
                            "is_llm_message": True,
                            "content": '{"role": "assistant", "content": "，一个由Kortix团队创建的自主AI助手。"}',
                            "metadata": '{"stream_status": "chunk", "thread_run_id": "run_456"}',
                            "created_at": "2025-08-25T10:30:03Z",
                            "updated_at": "2025-08-25T10:30:03Z"
                        }
                        
                        # 3. 工具调用 (如果有的话)
                        yield {
                            "sequence": 4,
                            "message_id": "msg_003",
                            "thread_id": "thread_123",
                            "type": "status", 
                            "is_llm_message": True,
                            "content": '{"role": "assistant", "status_type": "tool_started", "tool_name": "web_search", "tool_args": {"query": "最新科技新闻"}}',
                            "metadata": '{"thread_run_id": "run_456", "tool_index": 0}',
                            "created_at": "2025-08-25T10:30:04Z",
                            "updated_at": "2025-08-25T10:30:04Z"
                        }
                        
                        # 4. 工具结果
                        yield {
                            "sequence": 5,
                            "message_id": "msg_004",
                            "thread_id": "thread_123",
                            "type": "tool",
                            "is_llm_message": True,
                            "content": '{"role": "tool", "tool_name": "web_search", "result": "找到了最新的科技新闻..."}',
                            "metadata": '{"thread_run_id": "run_456", "tool_index": 0}',
                            "created_at": "2025-08-25T10:30:05Z",
                            "updated_at": "2025-08-25T10:30:05Z"
                        }
                        
                        # 5. 最终助手消息 (保存到数据库)
                        yield {
                            "sequence": 6,
                            "message_id": "msg_005",
                            "thread_id": "thread_123",
                            "type": "assistant",
                            "is_llm_message": True,
                            "content": '{"role": "assistant", "content": "你好！我是Suna.so，一个由Kortix团队创建的自主AI助手。我具备广泛的能力，可以执行复杂的任务..."}',
                            "metadata": '{"thread_run_id": "run_456", "finish_reason": "stop"}',
                            "created_at": "2025-08-25T10:30:06Z",
                            "updated_at": "2025-08-25T10:30:06Z"
                        }
                        
                        # 6. 结束事件
                        yield {
                            "sequence": 7,
                            "message_id": "msg_006",
                            "thread_id": "thread_123",
                            "type": "status",
                            "is_llm_message": False,
                            "content": '{"status_type": "assistant_response_end", "finish_reason": "stop"}',
                            "metadata": '{"thread_run_id": "run_456"}',
                            "created_at": "2025-08-25T10:30:07Z",
                            "updated_at": "2025-08-25T10:30:07Z"
                        }
                    
                    response_generator = fake_response_generator()
                    
                    # # Ensure we have an async generator for streaming
                    # if hasattr(llm_response, '__aiter__'):
                    #     print("我发现有 __aiter__！！！")
                    #     response_generator = self.response_processor.process_streaming_response(
                    #         llm_response=cast(AsyncGenerator, llm_response),
                    #         thread_id=thread_id,
                    #         config=config,
                    #         prompt_messages=prepared_messages,
                    #         llm_model=llm_model,
                    #         can_auto_continue=(native_max_auto_continues > 0),
                    #         auto_continue_count=auto_continue_count,
                    #         continuous_state=continuous_state
                    #     )
                    # else:
                    #     print("我进入的是 非流式！！！")
                    #     # Fallback to non-streaming if response is not iterable
                    #     response_generator = self.response_processor.process_non_streaming_response(
                    #         llm_response=llm_response,
                    #         thread_id=thread_id,
                    #         config=config,
                    #         prompt_messages=prepared_messages,
                    #         llm_model=llm_model,
                    #     )

                    return response_generator
                else:
                    logger.debug("Processing non-streaming response")
                    # Pass through the response generator without try/except to let errors propagate up
                    response_generator = self.response_processor.process_non_streaming_response(
                        llm_response=llm_response,
                        thread_id=thread_id,
                        config=config,
                        prompt_messages=prepared_messages,
                        llm_model=llm_model,
                    )
                    return response_generator # Return the generator

            except Exception as e:
                logger.error(f"Error in run_thread: {str(e)}", exc_info=True)
                # Return the error as a dict to be handled by the caller
                return {
                    "type": "status",
                    "status": "error",
                    "message": str(e)
                }

        # Define a wrapper generator that handles auto-continue logic
        async def auto_continue_wrapper():
            nonlocal auto_continue, auto_continue_count

            while auto_continue and (native_max_auto_continues == 0 or auto_continue_count < native_max_auto_continues):
                # Reset auto_continue for this iteration
                auto_continue = False

                # Run the thread once, passing the potentially modified system prompt
                # Pass temp_msg only on the first iteration
                try:
                    response_gen = await _run_once(temporary_message if auto_continue_count == 0 else None)

                    # Handle error responses
                    if isinstance(response_gen, dict) and "status" in response_gen and response_gen["status"] == "error":
                        logger.error(f"Error in auto_continue_wrapper: {response_gen.get('message', 'Unknown error')}")
                        yield response_gen
                        return  # Exit the generator on error

                    # Process each chunk
                    try:
                        if hasattr(response_gen, '__aiter__'):
                            async for chunk in cast(AsyncGenerator, response_gen):
                                # Check if this is a finish reason chunk with tool_calls or xml_tool_limit_reached
                                if chunk.get('type') == 'finish':
                                    if chunk.get('finish_reason') == 'tool_calls':
                                        # Only auto-continue if enabled (max > 0)
                                        if native_max_auto_continues > 0:
                                            logger.info(f"Detected finish_reason='tool_calls', auto-continuing ({auto_continue_count + 1}/{native_max_auto_continues})")
                                            auto_continue = True
                                            auto_continue_count += 1
                                            # Don't yield the finish chunk to avoid confusing the client
                                            continue
                                    elif chunk.get('finish_reason') == 'xml_tool_limit_reached':
                                        # Don't auto-continue if XML tool limit was reached
                                        logger.info(f"Detected finish_reason='xml_tool_limit_reached', stopping auto-continue")
                                        auto_continue = False
                                        # Still yield the chunk to inform the client

                                elif chunk.get('type') == 'status':
                                    # if the finish reason is length, auto-continue
                                    content = json.loads(chunk.get('content'))
                                    if content.get('finish_reason') == 'length':
                                        logger.info(f"Detected finish_reason='length', auto-continuing ({auto_continue_count + 1}/{native_max_auto_continues})")
                                        auto_continue = True
                                        auto_continue_count += 1
                                        continue
                                # Otherwise just yield the chunk normally
                                yield chunk
                        else:
                            # response_gen is not iterable (likely an error dict), yield it directly
                            yield response_gen

                        # If not auto-continuing, we're done
                        if not auto_continue:
                            break
                    except Exception as e:
                        if ("AnthropicException - Overloaded" in str(e)):
                            logger.error(f"AnthropicException - Overloaded detected - Falling back to OpenRouter: {str(e)}", exc_info=True)
                            nonlocal llm_model
                            # Remove "-20250514" from the model name if present
                            model_name_cleaned = llm_model.replace("-20250514", "")
                            llm_model = f"openrouter/{model_name_cleaned}"
                            auto_continue = True
                            continue # Continue the loop
                        else:
                            # If there's any other exception, log it, yield an error status, and stop execution
                            logger.error(f"Error in auto_continue_wrapper generator: {str(e)}", exc_info=True)
                            yield {
                                "type": "status",
                                "status": "error",
                                "message": f"Error in thread processing: {str(e)}"
                            }
                        return  # Exit the generator on any error
                except Exception as outer_e:
                    # Catch exceptions from _run_once itself
                    logger.error(f"Error executing thread: {str(outer_e)}", exc_info=True)
                    yield {
                        "type": "status",
                        "status": "error",
                        "message": f"Error executing thread: {str(outer_e)}"
                    }
                    return  # Exit immediately on exception from _run_once

            # If we've reached the max auto-continues, log a warning
            if auto_continue and auto_continue_count >= native_max_auto_continues:
                logger.warning(f"Reached maximum auto-continue limit ({native_max_auto_continues}), stopping.")
                yield {
                    "type": "content",
                    "content": f"\n[Agent reached maximum auto-continue limit of {native_max_auto_continues}]"
                }
        # If auto-continue is disabled (max=0), just run once
        if native_max_auto_continues == 0:
            print("我现在是进到这里来了！！！！！！！！！！！！！")
            print("Auto-continue is disabled (native_max_auto_continues=0)")
            # Pass the potentially modified system prompt and temp message
            return await _run_once(temporary_message)

        # Otherwise return the auto-continue wrapper generator
        return auto_continue_wrapper()

