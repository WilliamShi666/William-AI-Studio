"""
Header-Recursive Markdown 切分器

标题层级感知的递归切分：
1. 解析并追踪 Markdown 标题层级（# ## ### 等）
2. 每个 chunk 包含完整的标题路径信息
3. 可选：避免跨越重要标题边界（一级/二级标题）
4. 支持跨页处理和桥接片段
5. 表格和句子边界检测

输出的每个 chunk 包含：
- text: 文本内容
- pages: 页码列表
- headers: 标题层级路径（如: ["# 第一章", "## 1.1 概述"]）
- text_length: 字符长度
- is_table_like: 是否包含表格
- cross_page_bridge: 是否为跨页桥接chunk
- continued: 是否由多个小块合并而成

"""

import re, json
from typing import List, Dict, Tuple, Optional
from langchain_text_splitters import MarkdownTextSplitter  # type: ignore

# ========== 1) 基础：按 {{第N页}} 分页 ==========
def split_pages(md_text: str) -> Tuple[str, List[Tuple[int, str]]]:
    pattern = re.compile(r"\{\{第(\d+)页\}\}")
    parts = pattern.split(md_text)
    page_blocks: List[Tuple[int, str]] = []
    # 处理页标前的前缀（若有）
    prefix = parts[0]
    if prefix.strip():
        page_blocks.append((1, prefix))
    # 解析 (page_no, content) 对
    for i in range(1, len(parts), 2):
        page_no = int(parts[i])
        content = parts[i + 1]
        page_blocks.append((page_no, content))
    # 去掉页标后的"全文"
    full_text_clean = "".join(block for _, block in page_blocks)
    return full_text_clean, page_blocks

# ========== 1.5) 标题解析和追踪 ==========
HEADING_PATTERN = re.compile(r'^(#{1,6})\s+(.+?)$', re.MULTILINE)

def parse_headers_in_text(text: str) -> List[Dict]:
    """
    解析文本中的所有标题
    返回: [{"level": 1, "title": "第一章", "position": 0}, ...]
    """
    headers = []
    for match in HEADING_PATTERN.finditer(text):
        level = len(match.group(1))
        title = match.group(2).strip()
        position = match.start()
        headers.append({
            "level": level,
            "title": title,
            "position": position
        })
    return headers

def get_header_context(text: str, chunk_text: str, full_text: str) -> List[str]:
    """
    获取 chunk 所在的标题层级路径
    
    例如: ["# 第一章", "## 1.1 介绍", "### 1.1.1 背景"]
    """
    # 在完整文本中找到这个chunk的大致位置
    chunk_start = full_text.find(chunk_text[:100] if len(chunk_text) > 100 else chunk_text)
    if chunk_start == -1:
        return []
    
    # 找出这个位置之前的所有标题
    headers = parse_headers_in_text(full_text)
    
    # 构建标题栈（只保留chunk之前的标题）
    relevant_headers = [h for h in headers if h["position"] < chunk_start]
    
    if not relevant_headers:
        return []
    
    # 构建层级路径（保持标题层级递增）
    header_path = []
    current_levels = {}
    
    for h in relevant_headers:
        level = h["level"]
        title = h["title"]
        
        # 清除更深层级的标题
        keys_to_remove = [k for k in current_levels.keys() if k >= level]
        for k in keys_to_remove:
            del current_levels[k]
        
        # 添加当前层级
        current_levels[level] = f"{'#' * level} {title}"
    
    # 返回有序的标题路径
    header_path = [current_levels[level] for level in sorted(current_levels.keys())]
    return header_path

# ========== 2) 主流程：带标题追踪的 MarkdownTextSplitter 切分 ==========
def chunk_header_recursive_with_cross_page(
    md_text: str,
    chunk_size: int = 600,
    chunk_overlap: int = 80,
    merge_tolerance: float = 0.2,
    max_page_span: int = 3,
    bridge_span: int = 150,
    respect_headers: bool = True  # 新增：是否尊重标题边界
) -> Dict:
    """
    带标题层级追踪的 Markdown 切分（真正的 header-recursive）
    
    参数:
        md_text: 输入的markdown文本
        chunk_size: 目标chunk大小
        chunk_overlap: 切分时的重叠长度
        merge_tolerance: 合并时允许超出chunk_size的比例（默认20%）
        max_page_span: 单个chunk允许跨越的最大页数（默认3页，0表示不限制）
        bridge_span: 跨页桥接片段长度
        respect_headers: 是否避免跨越一级标题边界
        
    输出：
    {
      "full_text": <去页标全文>,
      "chunks": [
        {
          "page_start": int, "page_end": int, "pages": [..],
          "text": str,
          "text_length": int,
          "continued": bool,
          "cross_page_bridge": bool,
          "is_table_like": bool,
          "headers": [...]  # 新增：标题层级路径
        }, ...
      ]
    }
    """
    full_text_clean, page_blocks = split_pages(md_text)

    splitter = MarkdownTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    # 第一步：逐页切分 - 这些是"最小不可分单元"
    raw_chunks: List[Dict] = []
    for page_no, page_text in page_blocks:
        sub_texts = splitter.split_text(page_text)
        for sub in sub_texts:
            t = sub.strip()
            # 获取这个chunk的标题层级
            headers = get_header_context(page_text, t, full_text_clean)
            
            raw_chunks.append({
                "page_start": page_no,
                "page_end": page_no,
                "pages": [page_no],
                "text": t,
                "text_length": len(t),
                "continued": False,
                "cross_page_bridge": False,
                # 简易表格判断：以竖线开头或包含表格行分隔
                "is_table_like": t.startswith("|") or "\n|" in t,
                "headers": headers  # 添加标题信息
            })

    # 第二步：合并策略 - 以chunk_size为目标，考虑标题边界
    stitched = stitch_chunks_with_headers(
        raw_chunks, 
        chunk_size, 
        merge_tolerance, 
        max_page_span,
        respect_headers
    )

    # 第三步：可选——添加跨页桥接片段
    with_bridges = add_cross_page_bridges(stitched, bridge_span=bridge_span)

    return {"full_text": full_text_clean, "chunks": with_bridges}

# ========== 3) 启发式合并规则 ==========
SENT_END = "。！？.!?"
HEADING_PAT = re.compile(r"^\s{0,3}#{1,6}\s")          # # / ## / ### 标题
HR_PAT = re.compile(r"^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$") # --- 或 *** 分隔线
LIST_OR_CODE_START = re.compile(r"^\s{0,3}(\*|\-|\+|\d+\.)\s|^\s{0,3}```")  # 列表项或代码围栏

def looks_like_block_start(s: str) -> bool:
    """判断下块是否像新段/新节的开始（避免误并）"""
    if not s.strip():
        return False
    head = s.strip().splitlines()[0]
    return (
        HEADING_PAT.match(head) is not None or
        HR_PAT.match(head) is not None or
        LIST_OR_CODE_START.match(head) is not None
    )

def ends_with_sentence_break(s: str) -> bool:
    """判断文本是否以句子结束符结尾"""
    s = s.rstrip()
    return len(s) > 0 and s[-1] in SENT_END

def has_major_header_boundary(prev_chunk: Dict, curr_chunk: Dict) -> bool:
    """
    判断两个chunk之间是否有重要的标题边界
    
    如果当前chunk以一级或二级标题开头，则认为是重要边界
    """
    curr_text = curr_chunk["text"].lstrip()
    # 检查是否以 # 或 ## 开头
    if curr_text.startswith("# ") or curr_text.startswith("## "):
        return True
    return False

def stitch_chunks_with_headers(
    chunks: List[Dict], 
    chunk_size: int, 
    tolerance: float = 0.2, 
    max_page_span: int = 3,
    respect_headers: bool = True
) -> List[Dict]:
    """
    带标题感知的合并策略
    
    相比原版，增加了标题边界检测：
    1. 同页内，合并后不超过容忍范围 -> 合并
    2. 相邻页：
       - 合并后 <= chunk_size 且符合语义连续性 -> 合并
       - 合并后在容忍范围内且是表格或短块 -> 合并
    3. 跨页数不超过 max_page_span（0表示不限制）
    4. **新增**: 如果 respect_headers=True，避免跨越一级/二级标题边界
    """
    if not chunks:
        return []
    
    stitched = [chunks[0]]
    max_allowed = int(chunk_size * (1 + tolerance))
    
    for curr in chunks[1:]:
        prev = stitched[-1]
        combined_len = len(prev["text"]) + len(curr["text"])
        
        should_merge = False
        
        # 新增：检查标题边界
        if respect_headers and has_major_header_boundary(prev, curr):
            # 遇到重要标题，不合并
            stitched.append(curr)
            continue
        
        # 检查页数跨度限制
        if max_page_span > 0:
            potential_page_span = curr["page_end"] - prev["page_start"] + 1
            if potential_page_span > max_page_span:
                stitched.append(curr)
                continue
        
        # 条件1: 同一页内
        if prev["page_end"] == curr["page_start"]:
            if combined_len <= max_allowed:
                should_merge = True
        
        # 条件2: 相邻页
        elif curr["pages"][0] - prev["pages"][-1] == 1:
            if combined_len <= chunk_size:
                # 不超过目标大小，检查语义连续性
                if prev.get("is_table_like") and curr.get("is_table_like"):
                    should_merge = True
                elif (not ends_with_sentence_break(prev["text"])) and (not looks_like_block_start(curr["text"])):
                    should_merge = True
                elif len(prev["text"]) < chunk_size * 0.3 and (not looks_like_block_start(curr["text"])):
                    should_merge = True
            elif combined_len <= max_allowed:
                if prev.get("is_table_like") and curr.get("is_table_like"):
                    should_merge = True
                elif len(curr["text"]) < chunk_size * 0.3:
                    should_merge = True
        
        if should_merge:
            # 执行合并
            prev["text"] = prev["text"].rstrip() + "\n" + curr["text"].lstrip()
            prev["text_length"] = len(prev["text"])
            prev["page_end"] = curr["page_end"]
            prev["pages"] = sorted(set(prev["pages"] + curr["pages"]))
            prev["continued"] = True
            prev["is_table_like"] = prev.get("is_table_like") and curr.get("is_table_like")
            # 合并标题路径（取并集）
            if "headers" in curr:
                prev["headers"] = list(set(prev.get("headers", []) + curr["headers"]))
        else:
            stitched.append(curr)
    
    return stitched

# ========== 4) 跨页桥接片段（带标题信息）==========
def add_cross_page_bridges(chunks: List[Dict], bridge_span: int = 150) -> List[Dict]:
    """
    为跨页边界添加"小桥"chunk，便于检索跨页语义
    桥接chunk会继承前一个chunk的标题信息
    """
    out: List[Dict] = []
    for i, c in enumerate(chunks):
        out.append(c)
        if i + 1 < len(chunks):
            n = chunks[i + 1]
            # 只在页码相邻时添加桥
            if n["pages"][0] - c["pages"][-1] == 1:
                tail = c["text"][-bridge_span:] if len(c["text"]) >= bridge_span else c["text"]
                head = n["text"][0:bridge_span] if len(n["text"]) >= bridge_span else n["text"]
                if tail.strip() and head.strip():
                    bridge_text = tail + "\n" + head
                    # 桥接chunk继承前一个chunk的标题信息
                    bridge_headers = c.get("headers", [])
                    
                    out.append({
                        "page_start": c["page_end"],
                        "page_end": n["page_start"],
                        "pages": [c["page_end"], n["page_start"]],
                        "text": bridge_text,
                        "text_length": len(bridge_text),
                        "continued": True,
                        "cross_page_bridge": True,
                        "is_table_like": c.get("is_table_like") or n.get("is_table_like"),
                        "headers": bridge_headers  # 添加标题信息
                    })
    return out

# ========== 5) DEMO：测试和验证 ==========
if __name__ == "__main__":
    # 示例：使用带页码标记的markdown文件
    md_path = "/home/data/nongwa/workspace/Information-Extraction/unified/output/阿里开发手册-泰山版_1/accurate_text.md"
    
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            md_text = f.read()
    except FileNotFoundError:
        # 如果找不到文件，使用示例文本
        print("使用示例文本进行演示...")
        md_text = """{{第1页}}
# 第一章 编程规约

## 1.1 命名风格

【强制】代码中的命名均不能以下划线或美元符号开始，也不能以下划线或美元符号结束。

反例：`_name / __name / $name / name_ / name$ / name__`

## 1.2 常量定义

【强制】常量命名全部大写，单词间用下划线隔开。

{{第2页}}

### 1.2.1 魔法值

【强制】不允许任何魔法值（即未经定义的常量）直接出现在代码中。

| 规则 | 说明 |
|------|------|
| 数字 | 禁止直接使用数字 |
| 字符串 | 禁止直接使用字符串 |

# 第二章 异常日志

## 2.1 异常处理

【强制】Java 类库中定义的可以通过预检查方式规避的RuntimeException异常不应该通过catch 的方式来处理。
"""
    
    # 执行切分
    data = chunk_header_recursive_with_cross_page(
        md_text,
        chunk_size=1500,
        chunk_overlap=200,
        merge_tolerance=0.2,
        max_page_span=3,
        bridge_span=150,
        respect_headers=True  # 启用标题边界检测
    )
    
    print("=" * 80)
    print(f"✓ Header-Recursive 切分完成")
    print(f"  总chunk数: {len(data['chunks'])}")
    print("=" * 80)
    
    for i, chunk in enumerate(data["chunks"], 1):
        print(f"\n[Chunk {i}]")
        print(f"  页码: {chunk['pages']}")
        print(f"  标题层级: {chunk.get('headers', [])}")  # 修复：使用get方法
        print(f"  长度: {chunk['text_length']} 字符")
        print(f"  是否合并: {chunk['continued']}")
        print(f"  跨页桥接: {chunk['cross_page_bridge']}")
        print(f"  表格: {chunk.get('is_table_like', False)}")
        print(f"  文本预览:\n{chunk['text'][:150]}...")
        print("-" * 80)
    
    # 统计信息
    print("\n" + "=" * 80)
    print("统计信息:")
    print(f"  - 平均chunk长度: {sum(c['text_length'] for c in data['chunks']) / len(data['chunks']):.0f} 字符")
    print(f"  - 最长chunk: {max(c['text_length'] for c in data['chunks'])} 字符")
    print(f"  - 最短chunk: {min(c['text_length'] for c in data['chunks'])} 字符")
    print(f"  - 跨页chunk数: {sum(1 for c in data['chunks'] if len(c['pages']) > 1)}")
    print(f"  - 桥接chunk数: {sum(1 for c in data['chunks'] if c.get('cross_page_bridge', False))}")
    print(f"  - 包含标题的chunk数: {sum(1 for c in data['chunks'] if c.get('headers'))}")
    print("=" * 80)