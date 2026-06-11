async def process_adk_streaming_response(
    self,
    adk_response: AsyncGenerator,                  # ADK Runner.run_async(...) 的返回
    thread_id: str,
    prompt_messages: List[Dict[str, Any]],
    llm_model: str,
    config: ProcessorConfig = ProcessorConfig(),
    can_auto_continue: bool = False,
    auto_continue_count: int = 0,
    continuous_state: Optional[Dict[str, Any]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    适配 Google ADK 事件流的处理器：
    - 按事件粒度产出“chunk”，并在 metadata 中标注 ADK 状态
    - 不依赖 finish_reason；以 event.is_final_response() / event.partial / actions / long_running_tool_ids 记录状态
    - 汇总 usage_metadata（通常出现在最终事件上）
    - 可与现有 XML 工具调用/本地工具策略共存（保持占位逻辑，便于你后续接入）
    """

    import json, uuid, traceback
    from datetime import datetime, timezone

    def format_for_yield(msg_obj: Dict[str, Any]) -> Dict[str, Any]:
        # 你项目里已有的工具方法，这里做个兜底
        return msg_obj

    def _now_ts():
        return datetime.now(timezone.utc).timestamp()

    def _safe_text(x) -> str:
        # 规避“can only concatenate str (not 'list') to str”
        if isinstance(x, str):
            return x
        if isinstance(x, (list, tuple)):
            return "".join(str(t) for t in x)
        return str(x)

    def _event_is_final(e) -> bool:
        try:
            # ADK 提供的最终响应判定
            return bool(getattr(e, "is_final_response", None) and e.is_final_response())
        except Exception:
            # 回退逻辑：partial==False 且有 usage_metadata 时大概率为最终
            return bool(getattr(e, "partial", None) is False and getattr(e, "usage_metadata", None) is not None)

    # ============ 初始化 ============
    continuous_state = continuous_state or {}
    thread_run_id = continuous_state.get('thread_run_id') or str(uuid.uuid4())
    continuous_state['thread_run_id'] = thread_run_id

    accumulated_content = continuous_state.get('accumulated_content', "")
    xml_ongoing_buffer = continuous_state.get('xml_ongoing_buffer', "")
    __sequence = int(continuous_state.get('sequence', 0))

    # 工具相关（如果你开启了 XML 或 Native 工具）
    xml_chunks_buffer = []
    complete_native_tool_calls = []
    tool_result_message_objects = {}

    usage_snapshot = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    finish_reason = None           # 不作为流控制，仅用于最后记录
    saw_error = False
    saw_length_limit = False

    streaming_metadata = {
        "model": llm_model,
        "created": int(_now_ts()),
        "usage": usage_snapshot,
        "response_ms": None,
        "first_chunk_time": None,
        "last_chunk_time": None,
    }

    print(
        f"ADK Streaming Config: XML={config.xml_tool_calling}, "
        f"Native={config.native_tool_calling}, Execute on stream={config.execute_on_stream}, "
        f"Strategy={config.tool_execution_strategy}"
    )
    print("这是我thread_run_id的值", thread_run_id)

    # ============（可选）开始事件 ============
    # 如需恢复原先的“开始状态消息”，可取消注释
    # if auto_continue_count == 0:
    #     start_msg = await self.add_message(thread_id, "status",
    #         {"status_type": "thread_run_start", "thread_run_id": thread_run_id},
    #         is_llm_message=False, metadata={"thread_run_id": thread_run_id})
    #     if start_msg: yield format_for_yield(start_msg)
    #     assist_start = await self.add_message(thread_id, "status",
    #         {"status_type": "assistant_response_start"},
    #         is_llm_message=False, metadata={"thread_run_id": thread_run_id})
    #     if assist_start: yield format_for_yield(assist_start)

    try:
        print("🔍 [ADK PROCESSOR DEBUG] 开始处理 ADK 事件...")

        async for event in adk_response:
            # ===== 基本时序元数据 =====
            now_ts = _now_ts()
            if streaming_metadata["first_chunk_time"] is None:
                streaming_metadata["first_chunk_time"] = now_ts
            streaming_metadata["last_chunk_time"] = now_ts

            # ===== usage（若出现就更新；通常最终事件上才有）=====
            um = getattr(event, "usage_metadata", None)
            if um:
                # GenerateContentResponseUsageMetadata: candidates_token_count / prompt_token_count / total_token_count
                usage_snapshot["prompt_tokens"] = getattr(um, "prompt_token_count", usage_snapshot["prompt_tokens"])
                usage_snapshot["completion_tokens"] = getattr(um, "candidates_token_count", usage_snapshot["completion_tokens"])
                usage_snapshot["total_tokens"] = getattr(um, "total_token_count", usage_snapshot["total_tokens"])

            # ===== 错误 & 截断探测（ADK 的错误码/消息）=====
            error_code = getattr(event, "error_code", None)
            error_msg = getattr(event, "error_message", None)
            if error_code:
                saw_error = True
                # 约定：如果错误码等价于 token 截断
                if str(error_code).upper() in {"MAX_TOKENS", "TOKEN_LIMIT", "LENGTH"}:
                    saw_length_limit = True

            # ===== 事件层面的“状态位” =====
            partial = getattr(event, "partial", None)
            turn_complete = getattr(event, "turn_complete", None)
            is_final = _event_is_final(event)

            # ADK 的动作（移交、升级、状态/工件 delta、鉴权请求等）
            actions = getattr(event, "actions", None)
            long_run_tools = list(getattr(event, "long_running_tool_ids", []) or [])

            # 为每个 chunk 计算“所处状态”
            def _derive_chunk_status() -> str:
                if error_code:
                    return "error"
                if long_run_tools:
                    return "tool_running"
                # 移交/升级具备优先级标记
                if actions and (getattr(actions, "transfer_to_agent", None) or getattr(actions, "escalate", None)):
                    return "handover"
                if is_final:
                    return "final"
                if partial is True:
                    return "delta"
                if partial is False:
                    # 非流式单发 or 未标注 usage 的尾包
                    return "possibly_final"
                return "unknown"

            chunk_status = _derive_chunk_status()

            # ===== 内容增量产出 =====
            content = getattr(event, "content", None)
            if content and getattr(content, "parts", None):
                try:
                    for part in content.parts:
                        text = getattr(part, "text", None)
                        if text is None:
                            # 可能是其他 Part 形态（如 inlineData 等）；这里统一转成字符串
                            text = _safe_text(getattr(part, "to_dict", lambda: str(part))())
                        text = _safe_text(text)
                        if not text:
                            continue

                        # 累积总内容（用于最终入库）
                        accumulated_content += text
                        xml_ongoing_buffer += text  # 给 XML 工具解析留口子

                        # 产出“assistant”流式片段，并把 ADK 状态打在 metadata 上
                        msg_obj = {
                            "sequence": __sequence,
                            "message_id": None,
                            "thread_id": thread_id,
                            "type": "assistant",
                            "is_llm_message": True,
                            "content": json.dumps({"role": "assistant", "content": text}, ensure_ascii=False),
                            "metadata": json.dumps({
                                "thread_run_id": thread_run_id,
                                "stream_status": chunk_status,      # <=== 关键：chunk 所处状态
                                "adk": {
                                    "event_id": getattr(event, "id", None),
                                    "invocation_id": getattr(event, "invocation_id", None),
                                    "author": getattr(event, "author", None),
                                    "timestamp": getattr(event, "timestamp", None),
                                    "partial": partial,
                                    "turn_complete": turn_complete,
                                    "is_final_response": is_final,
                                    "error_code": error_code,
                                    "error_message": error_msg,
                                    "long_running_tool_ids": long_run_tools,
                                    "actions": {
                                        "transfer_to_agent": getattr(actions, "transfer_to_agent", None) if actions else None,
                                        "escalate": getattr(actions, "escalate", None) if actions else None,
                                        "state_delta": getattr(actions, "state_delta", None) if actions else None,
                                        "artifact_delta": getattr(actions, "artifact_delta", None) if actions else None,
                                        "requested_auth_configs": getattr(actions, "requested_auth_configs", None) if actions else None,
                                    },
                                }
                            }, ensure_ascii=False),
                        }
                        yield msg_obj
                        __sequence += 1

                except Exception as ie:
                    # 内容处理异常也要继续进行（并且让上层看到错误状态）
                    err_msg = await self.add_message(
                        thread_id=thread_id,
                        type="status",
                        content={"status_type": "error", "message": f"Error handling content parts: {ie}"},
                        is_llm_message=False,
                        metadata={"thread_run_id": thread_run_id},
                    )
                    if err_msg:
                        yield format_for_yield(err_msg)

            # =====（可选）XML 工具调用解析（保留占位，沿用你已有解析器）=====
            if config.xml_tool_calling and xml_ongoing_buffer:
                # TODO: 这里接你现有的 XML 解析逻辑，把解析出的 tool_calls 以“工具调用片段/完成”形式产出
                # xml_chunks = parse_xml_from_buffer(xml_ongoing_buffer)  # 伪代码
                # for tool_call in xml_chunks: yield {... "stream_status": "tool_call_chunk" ...}
                pass

            # ===== 事件层面的状态/工件增量，也可以单独落一条状态消息，便于前端 UI 标注 =====
            if actions and (getattr(actions, "state_delta", None) or getattr(actions, "artifact_delta", None)):
                state_msg = await self.add_message(
                    thread_id=thread_id,
                    type="status",
                    content={
                        "status_type": "agent_state_delta",
                        "state_delta": getattr(actions, "state_delta", None),
                        "artifact_delta": getattr(actions, "artifact_delta", None),
                    },
                    is_llm_message=False,
                    metadata={"thread_run_id": thread_run_id},
                )
                if state_msg:
                    yield format_for_yield(state_msg)

            # ===== 记录“最终事件”时的 finish_reason（仅用于存档/可视化，不用于流控制）=====
            # ADK 不保证一定提供 finish_reason；此处只做记录
            fr = getattr(event, "finish_reason", None)
            if fr:
                finish_reason = str(fr)

        # ===== 流结束：保存完整助手消息 & 结束事件 =====
        # 根据 ADK 设计，最终性应以 is_final_response 判定；finish_reason 可缺省
        if saw_error:
            resolved_finish = "error"
        elif saw_length_limit:
            resolved_finish = "length"
        elif finish_reason:
            resolved_finish = finish_reason
        else:
            resolved_finish = "stop"  # 默认

        if accumulated_content.strip():
            assistant_message_obj = await self.add_message(
                thread_id=thread_id,
                type="assistant",
                content=accumulated_content,
                is_llm_message=True,
                metadata={
                    "thread_run_id": thread_run_id,
                    "model": llm_model,
                    "finish_reason": resolved_finish,
                    "usage": usage_snapshot,
                },
            )
            if assistant_message_obj:
                yield format_for_yield(assistant_message_obj)

        # 结束状态（可选，与原逻辑保持一致）
        assist_end = await self.add_message(
            thread_id=thread_id,
            type="status",
            content={"status_type": "assistant_response_end"},
            is_llm_message=False,
            metadata={"thread_run_id": thread_run_id},
        )
        if assist_end:
            yield format_for_yield(assist_end)

        thread_end = await self.add_message(
            thread_id=thread_id,
            type="status",
            content={"status_type": "thread_run_end", "thread_run_id": thread_run_id},
            is_llm_message=False,
            metadata={"thread_run_id": thread_run_id},
        )
        if thread_end:
            yield format_for_yield(thread_end)

        print("✅ [ADK PROCESSOR DEBUG] ADK 流式响应处理完成")

    except Exception as e:
        traceback.print_exc()
        err = await self.add_message(
            thread_id=thread_id,
            type="status",
            content={"status_type": "error", "message": f"Error during ADK response streaming: {e}"},
            is_llm_message=False,
            metadata={"thread_run_id": thread_run_id},
        )
        if err:
            yield format_for_yield(err)
        raise
