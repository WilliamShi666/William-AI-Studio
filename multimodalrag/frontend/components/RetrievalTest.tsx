import { Search, Download, Save, GitCompare, Eye, Sparkles } from 'lucide-react';
import { useState } from 'react';
import { motion } from 'motion/react';
import { Slider } from './ui/slider';

export function RetrievalTest() {
  const [query, setQuery] = useState('');
  const [topK, setTopK] = useState([5]);
  const [similarityThreshold, setSimilarityThreshold] = useState(0.7);
  const [enableRerank, setEnableRerank] = useState(true);
  const [searchMode, setSearchMode] = useState('hybrid');

  const results = [
    {
      id: 1,
      rank: 1,
      similarity: 0.94,
      rerank: 0.96,
      source: 'API_Reference_v2.pdf',
      page: 3,
      content: 'RAG systems combine the power of retrieval and generation to provide accurate and contextual responses. The system first retrieves relevant documents...',
    },
    {
      id: 2,
      rank: 2,
      similarity: 0.89,
      rerank: 0.92,
      source: 'product_guide.pdf',
      page: 12,
      content: 'The main applications of RAG include question answering, document summarization, and knowledge base construction. It excels in scenarios...',
    },
    {
      id: 3,
      rank: 3,
      similarity: 0.87,
      rerank: 0.88,
      source: 'technical_docs.pdf',
      page: 7,
      content: 'Retrieval-Augmented Generation enhances accuracy by grounding responses in actual documents. This approach reduces hallucinations...',
    },
  ];

  const highlightKeywords = (text: string, keywords: string[]) => {
    let highlighted = text;
    keywords.forEach((keyword) => {
      const regex = new RegExp(`(${keyword})`, 'gi');
      highlighted = highlighted.replace(regex, '<mark class="bg-[rgba(56,189,248,0.3)] text-[#38BDF8] px-1 rounded">$1</mark>');
    });
    return highlighted;
  };

  return (
    <div className="space-y-6">
      {/* Query Input */}
      <motion.div 
        className="glass gradient-border rounded-2xl p-6"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
      >
        <div className="flex gap-4">
          <div className="flex-1 relative">
            <textarea
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="💬 输入测试问题... 例如: 'RAG系统的主要用途是什么？'"
              className="w-full min-h-[100px] px-5 py-4 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl focus:outline-none focus:ring-2 focus:ring-[#38BDF8] focus:border-[#38BDF8] resize-none text-[#E2E8F0] placeholder-[#94A3B8] transition-all duration-300"
            />
          </div>
          <motion.button 
            className="w-40 h-12 bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A] rounded-xl hover:shadow-[0_0_30px_rgba(56,189,248,0.6)] transition-all flex items-center justify-center gap-2 self-end relative overflow-hidden group"
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
          >
            <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white to-transparent opacity-0 group-hover:opacity-20 shimmer" />
            <Search size={20} className="relative z-10" />
            <span className="relative z-10">检索</span>
          </motion.button>
        </div>
      </motion.div>

      {/* Configuration Panel */}
      <motion.div 
        className="glass gradient-border rounded-2xl p-6"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
      >
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center shadow-lg">
            <Sparkles size={20} className="text-[#0F172A]" />
          </div>
          <h3 className="text-[#E2E8F0]">检索参数</h3>
        </div>

        {/* Parameters */}
        <div className="grid grid-cols-2 gap-6 mb-6">
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <label className="text-[#94A3B8]">Top K</label>
              <span className="text-[#38BDF8] px-3 py-1 rounded-lg bg-[rgba(56,189,248,0.1)] border border-[rgba(56,189,248,0.2)]">{topK[0]}</span>
            </div>
            <Slider
              value={topK}
              onValueChange={setTopK}
              min={1}
              max={20}
              step={1}
              className="w-full"
            />
          </div>

          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <label className="text-[#94A3B8]">相似度阈值</label>
              <input
                type="number"
                value={similarityThreshold}
                onChange={(e) => setSimilarityThreshold(parseFloat(e.target.value))}
                min={0}
                max={1}
                step={0.05}
                className="w-24 px-3 py-1 glass-strong border border-[rgba(56,189,248,0.2)] rounded-lg text-center focus:outline-none focus:ring-2 focus:ring-[#38BDF8] text-[#E2E8F0]"
              />
            </div>
          </div>
        </div>

        {/* Rerank Checkbox */}
        <div className="mb-6">
          <label className="flex items-center gap-3 cursor-pointer group">
            <div className="relative">
              <input
                type="checkbox"
                checked={enableRerank}
                onChange={(e) => setEnableRerank(e.target.checked)}
                className="w-5 h-5 rounded border-[rgba(56,189,248,0.3)] text-[#38BDF8] focus:ring-2 focus:ring-[#38BDF8] bg-[rgba(30,41,59,0.6)] cursor-pointer"
              />
            </div>
            <span className="text-[#E2E8F0] group-hover:text-[#38BDF8] transition-colors">启用Rerank</span>
          </label>
        </div>

        {/* Search Mode */}
        <div className="space-y-3">
          <label className="text-[#94A3B8]">搜索模式</label>
          <div className="grid grid-cols-3 gap-4">
            {[
              { id: 'vector', label: '向量', icon: '🔵', desc: '语义搜索' },
              { id: 'hybrid', label: '混合', icon: '⚫', desc: '最佳效果' },
              { id: 'keyword', label: '关键词', icon: '⚪', desc: '精确匹配' },
            ].map((mode) => (
              <motion.button
                key={mode.id}
                onClick={() => setSearchMode(mode.id)}
                whileHover={{ scale: 1.05, y: -2 }}
                whileTap={{ scale: 0.95 }}
                className={`p-4 rounded-xl border-2 transition-all flex flex-col items-center justify-center gap-2 relative overflow-hidden group ${
                  searchMode === mode.id
                    ? 'border-[#38BDF8] bg-[rgba(56,189,248,0.1)] shadow-[0_0_20px_rgba(56,189,248,0.3)]'
                    : 'border-[rgba(56,189,248,0.2)] glass hover:border-[rgba(56,189,248,0.4)]'
                }`}
              >
                {searchMode === mode.id && (
                  <div className="absolute inset-0 bg-gradient-to-r from-transparent via-[rgba(56,189,248,0.2)] to-transparent shimmer" />
                )}
                <span className="text-2xl relative z-10">{mode.icon}</span>
                <span className={`relative z-10 ${searchMode === mode.id ? 'text-[#38BDF8]' : 'text-[#E2E8F0]'}`}>
                  {mode.label}
                </span>
                <span className="text-xs text-[#94A3B8] relative z-10">{mode.desc}</span>
              </motion.button>
            ))}
          </div>
        </div>
      </motion.div>

      {/* Results Section */}
      <motion.div 
        className="glass gradient-border rounded-2xl p-6"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.2 }}
      >
        <div className="flex items-center justify-between mb-6">
          <h3 className="text-[#E2E8F0] flex items-center gap-2">
            <span className="text-2xl">📊</span>
            检索结果
          </h3>
          <span className="text-[#94A3B8] px-4 py-2 rounded-lg glass-strong border border-[rgba(56,189,248,0.2)]">
            (共{results.length}个，耗时<span className="text-[#38BDF8] mx-1">0.5s</span>)
          </span>
        </div>

        <div className="space-y-4">
          {results.map((result, index) => (
            <motion.div
              key={result.id}
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.3 + index * 0.1 }}
              whileHover={{ y: -2 }}
              className="glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl p-6 hover:border-[rgba(56,189,248,0.4)] hover:shadow-[0_0_20px_rgba(56,189,248,0.2)] transition-all relative overflow-hidden group"
            >
              {/* Hover effect */}
              <div className="absolute inset-0 opacity-0 group-hover:opacity-100 transition-opacity duration-500">
                <div className="absolute inset-0 bg-gradient-to-r from-transparent via-[rgba(56,189,248,0.05)] to-transparent shimmer" />
              </div>

              {/* Rank Badge */}
              <div className="absolute top-4 left-4 w-10 h-10 bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] text-[#0F172A] rounded-xl flex items-center justify-center shadow-lg z-10">
                #{result.rank}
              </div>

              <div className="ml-14 relative z-10">
                {/* Score Visualization */}
                <div className="mb-4 space-y-3">
                  <div className="flex items-center gap-4">
                    <span className="text-sm text-[#94A3B8] w-20">相似度:</span>
                    <div className="flex-1 h-2.5 bg-[rgba(30,41,59,0.8)] rounded-full overflow-hidden border border-[rgba(56,189,248,0.2)]">
                      <motion.div
                        initial={{ width: 0 }}
                        animate={{ width: `${result.similarity * 100}%` }}
                        transition={{ duration: 1, delay: 0.5 + index * 0.1 }}
                        className="h-full bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] relative overflow-hidden"
                      >
                        <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white to-transparent opacity-30 shimmer" />
                      </motion.div>
                    </div>
                    <span className="text-sm text-[#38BDF8] w-14 text-right">{result.similarity}</span>
                  </div>

                  {enableRerank && (
                    <div className="flex items-center gap-4">
                      <span className="text-sm text-[#94A3B8] w-20">重排序:</span>
                      <div className="flex-1 h-2.5 bg-[rgba(30,41,59,0.8)] rounded-full overflow-hidden border border-[rgba(52,211,153,0.2)]">
                        <motion.div
                          initial={{ width: 0 }}
                          animate={{ width: `${result.rerank * 100}%` }}
                          transition={{ duration: 1, delay: 0.6 + index * 0.1 }}
                          className="h-full bg-gradient-to-r from-[#34D399] to-[#10B981] relative overflow-hidden"
                        >
                          <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white to-transparent opacity-30 shimmer" />
                        </motion.div>
                      </div>
                      <span className="text-sm text-[#34D399] w-14 text-right">{result.rerank}</span>
                    </div>
                  )}
                </div>

                {/* Source Info */}
                <div className="mb-3 text-[#E2E8F0] flex items-center gap-2">
                  <span>📄</span>
                  <span>{result.source}</span>
                  <span className="text-[#94A3B8]">- 第{result.page}页</span>
                </div>

                {/* Content Preview */}
                <div className="glass rounded-xl p-4 mb-4 text-sm text-[#94A3B8] max-h-[120px] overflow-auto border border-[rgba(56,189,248,0.1)]">
                  <div dangerouslySetInnerHTML={{ __html: highlightKeywords(result.content, ['RAG', 'retrieval', 'generation']) }} />
                </div>

                {/* Action Buttons */}
                <div className="flex gap-3">
                  <motion.button 
                    className="px-4 py-2 border border-[#38BDF8] text-[#38BDF8] rounded-xl hover:bg-[rgba(56,189,248,0.1)] transition-all flex items-center gap-2"
                    whileHover={{ scale: 1.05 }}
                    whileTap={{ scale: 0.95 }}
                  >
                    <Eye size={16} />
                    查看完整Chunk
                  </motion.button>
                  <motion.button 
                    className="px-4 py-2 border border-[#38BDF8] text-[#38BDF8] rounded-xl hover:bg-[rgba(56,189,248,0.1)] transition-all"
                    whileHover={{ scale: 1.05 }}
                    whileTap={{ scale: 0.95 }}
                  >
                    查看原始页面
                  </motion.button>
                </div>
              </div>
            </motion.div>
          ))}
        </div>
      </motion.div>

      {/* Bottom Action Bar */}
      <motion.div 
        className="flex gap-4"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.6 }}
      >
        <motion.button 
          className="flex-1 px-6 py-4 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl hover:bg-[rgba(56,189,248,0.05)] hover:border-[rgba(56,189,248,0.4)] transition-all flex items-center justify-center gap-2 text-[#E2E8F0] group"
          whileHover={{ scale: 1.02, y: -2 }}
          whileTap={{ scale: 0.98 }}
        >
          <Download size={18} className="group-hover:text-[#38BDF8] transition-colors" />
          <span>导出结果</span>
        </motion.button>
        <motion.button 
          className="flex-1 px-6 py-4 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl hover:bg-[rgba(56,189,248,0.05)] hover:border-[rgba(56,189,248,0.4)] transition-all flex items-center justify-center gap-2 text-[#E2E8F0] group"
          whileHover={{ scale: 1.02, y: -2 }}
          whileTap={{ scale: 0.98 }}
        >
          <Save size={18} className="group-hover:text-[#38BDF8] transition-colors" />
          <span>保存测试案例</span>
        </motion.button>
        <motion.button 
          className="flex-1 px-6 py-4 bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A] rounded-xl hover:shadow-[0_0_30px_rgba(56,189,248,0.6)] transition-all flex items-center justify-center gap-2 relative overflow-hidden group"
          whileHover={{ scale: 1.02, y: -2 }}
          whileTap={{ scale: 0.98 }}
        >
          <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white to-transparent opacity-0 group-hover:opacity-20 shimmer" />
          <GitCompare size={18} className="relative z-10" />
          <span className="relative z-10">对比不同参数</span>
        </motion.button>
      </motion.div>
    </div>
  );
}
