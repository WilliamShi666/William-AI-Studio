'use client';

import React, { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import { Button } from '@/components/ui/button';
import {
  BarChart3,
  Bot,
  Briefcase,
  Settings,
  Sparkles,
  RefreshCw,
  TrendingUp,
  Users,
  Shield,
  Zap,
  Target,
  Brain,
  Globe,
  Heart,
  PenTool,
  Code,
  Camera,
  Calendar,
  DollarSign,
  Rocket,
} from 'lucide-react';

type PromptExample = {
  title: string;
  query: string;
  icon: React.ReactNode;
};

const allPrompts: PromptExample[] = [
  {
    title: '寻找最佳面包店地图',
    query: '1. 在Google地图上搜索"{{city}}最佳面包店"\n2. 创建包含前{{number}}家面包店的自定义列表\n3. 为每家面包店收集：\n   - 客户评分和热门商品\n   - 营业时间、位置和特色\n   - 价格区间和必试糕点\n4. 生成包含推荐的总结',
    icon: <Globe className="text-blue-700 dark:text-blue-400" size={16} />,
  },
  {
    title: '研究教育数据',
    query: '1. 访问联合国教科文组织数据库获取{{topic}}教育统计数据\n2. 汇编以下数据：\n   - 各地区学生入学率\n   - 全球师生比例\n   - 教育支出占GDP百分比\n3. 创建包含趋势的结构化电子表格\n4. 生成包含关键洞察的执行摘要',
    icon: <BarChart3 className="text-purple-700 dark:text-purple-400" size={16} />,
  },
  {
    title: '计划旅行行程',
    query: '1. 在TripAdvisor上研究{{destination}}的{{duration}}天行程\n2. 寻找顶级景点、餐厅和活动\n3. 根据位置和开放时间优化每日安排\n4. 包含交通、天气和备用计划\n5. 创建按时间块划分的逐日行程',
    icon: <Calendar className="text-rose-700 dark:text-rose-400" size={16} />,
  },
  {
    title: '分析新闻报道',
    query: '1. 搜索{{news_outlet}}关于{{topic}}的过去{{time_period}}文章\n2. 对报道进行分类并识别关键主题\n3. 跟踪专家来源和数据点\n4. 创建重大发展的时间线\n5. 生成包含洞察和报道空白的报告',
    icon: <PenTool className="text-indigo-700 dark:text-indigo-400" size={16} />,
  },
  // {
  //   title: 'Book restaurant reservations',
  //   query: '1. Search OpenTable for restaurants in {{city}} for {{occasion}}\n2. Filter by date, party size, and cuisine preferences\n3. Check reviews and menu highlights\n4. Make reservations at {{number}} restaurants\n5. Create itinerary with confirmation details',
  //   icon: <Users className="text-emerald-700 dark:text-emerald-400" size={16} />,
  // },
  {
    title: '构建财务模型',
    query: '1. 为{{company_type}}企业创建{{model_type}}模型\n2. 收集历史数据和行业基准\n3. 构建收入预测和费用预测\n4. 包括DCF、LTV/CAC或NPV分析\n5. 设计包含情景分析的Excel仪表板',
    icon: <DollarSign className="text-orange-700 dark:text-orange-400" size={16} />,
  },
  {
    title: '制定市场策略',
    query: '1. 为{{product_type}}发布创建进入市场策略\n2. 分析目标市场和竞争格局\n3. 设计市场进入和定价策略\n4. 构建财务预测和时间线\n5. 创建包含建议的演示文稿',
    icon: <Target className="text-cyan-700 dark:text-cyan-400" size={16} />,
  },
  {
    title: '研究公司情报',
    query: '1. 全面研究{{company_name}}\n2. 收集最新新闻、融资和领导层信息\n3. 分析竞争地位和市场份额\n4. 研究关键人员背景\n5. 创建包含可行洞察的详细档案',
    icon: <Briefcase className="text-teal-700 dark:text-teal-400" size={16} />,
  },
  {
    title: '审计日程生产力',
    query: '1. 分析过去{{months}}个月的{{calendar_app}}数据\n2. 评估会议频率和专注时间\n3. 识别优化机会\n4. 分析会议效果模式\n5. 生成建议和实施计划',
    icon: <Calendar className="text-violet-700 dark:text-violet-400" size={16} />,
  },
  {
    title: '研究行业趋势',
    query: '1. 从{{data_sources}}研究{{industry}}趋势\n2. 收集投资活动和技术发展\n3. 分析市场驱动因素和机会\n4. 识别新兴主题和空白\n5. 创建包含建议的综合报告',
    icon: <TrendingUp className="text-pink-700 dark:text-pink-400" size={16} />,
  },
  {
    title: '自动化客服工单',
    query: '1. 监控{{support_platform}}的来来工单\n2. 对问题进行分类并评估紧急程度\n3. 在{{knowledge_base}}中搜索解决方案\n4. 根据置信度自动回复或升级\n5. 跟踪指标并生成每日报告',
    icon: <Shield className="text-yellow-600 dark:text-yellow-300" size={16} />,
  },
  {
    title: '研究法律合规',
    query: '1. 研究{{jurisdictions}}的{{legal_topic}}\n2. 比较州要求和费用\n3. 分析决策因素和影响\n4. 收集实际实施细节\n5. 创建包含建议的比较电子表格',
    icon: <Settings className="text-red-700 dark:text-red-400" size={16} />,
  },
  {
    title: '编制数据分析',
    query: '1. 从{{data_sources}}收集{{data_topic}}数据\n2. 清理和标准化数据集\n3. 分析模式并计算趋势\n4. 创建包含可视化的电子表格\n5. 提供战略建议',
    icon: <BarChart3 className="text-slate-700 dark:text-slate-400" size={16} />,
  },
  {
    title: '计划社交媒体内容',
    query: '1. 为{{brand}}创建{{duration}}社交策略\n2. 研究热门话题和竞争对手内容\n3. 制定每周{{posts_per_week}}篇帖子的内容日历\n4. 创建特定平台内容和调度\n5. 设置分析和月度报告',
    icon: <Camera className="text-stone-700 dark:text-stone-400" size={16} />,
  },
  {
    title: '比较产品',
    query: '1. 全面研究{{product_category}}选项\n2. 收集科学研究和专家意见\n3. 分析优势、劣势和成本\n4. 研究当前专家共识\n5. 创建包含个性化建议的比较报告',
    icon: <Brain className="text-fuchsia-700 dark:text-fuchsia-400" size={16} />,
  },
  {
    title: '分析市场机会',
    query: '1. 研究{{market_topic}}投资机会\n2. 分析市场规模、增长和关键参与者\n3. 识别投资主题和风险\n4. 评估市场挑战和障碍\n5. 创建包含建议的投资演示文稿',
    icon: <Rocket className="text-green-600 dark:text-green-300" size={16} />,
  },
  {
    title: '处理发票和文档',
    query: '1. 扫描{{document_folder}}中的PDF发票\n2. 提取关键数据：编号、日期、金额、供应商\n3. 使用标准化字段组织数据\n4. 构建综合跟踪电子表格\n5. 生成月度财务报告',
    icon: <Heart className="text-amber-700 dark:text-amber-400" size={16} />,
  },
  {
    title: '寻找人才和候选人',
    query: '1. 在{{location}}搜索{{job_title}}候选人\n2. 使用LinkedIn、GitHub和招聘网站\n3. 评估技能、经验和文化契合度\n4. 创建排名候选人管道\n5. 制定个性化接触策略',
    icon: <Users className="text-blue-600 dark:text-blue-300" size={16} />,
  },
  {
    title: '构建专业网站',
    query: '1. 全面在线研究{{person_name}}\n2. 分析专业品牌和成就\n3. 设计网站结构和内容\n4. 创建包含作品集的优化页面\n5. 实施SEO和性能功能',
    icon: <Globe className="text-red-600 dark:text-red-300" size={16} />,
  },
];

// Function to get random prompts
const getRandomPrompts = (count: number = 3): PromptExample[] => {
  const shuffled = [...allPrompts].sort(() => 0.5 - Math.random());
  return shuffled.slice(0, count);
};

export const Examples = ({
  onSelectPrompt,
  count = 3,
}: {
  onSelectPrompt?: (query: string) => void;
  count?: number;
}) => {
  const [displayedPrompts, setDisplayedPrompts] = useState<PromptExample[]>([]);
  const [isRefreshing, setIsRefreshing] = useState(false);

  // Initialize with random prompts on mount
  useEffect(() => {
    setDisplayedPrompts(getRandomPrompts(count));
  }, [count]);

  const handleRefresh = () => {
    setIsRefreshing(true);
    setDisplayedPrompts(getRandomPrompts(count));
    setTimeout(() => setIsRefreshing(false), 300);
  };

  return (
    <div className="w-full max-w-4xl mx-auto px-4">
      <div className="group relative">
        <div className="flex gap-2 justify-center py-2 flex-wrap">
          {displayedPrompts.map((prompt, index) => (
            <motion.div
              key={`${prompt.title}-${index}`}
              initial={{ opacity: 0, scale: 0.8 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{
                duration: 0.3,
                delay: index * 0.03,
                ease: "easeOut"
              }}
            >
              <Button
                variant="outline"
                className="w-fit h-fit px-3 py-2 rounded-full border-neutral-200 dark:border-neutral-800 bg-neutral-50 hover:bg-neutral-100 dark:bg-neutral-900 dark:hover:bg-neutral-800 text-sm font-normal text-muted-foreground hover:text-foreground transition-colors"
                onClick={() => onSelectPrompt && onSelectPrompt(prompt.query)}
              >
                <div className="flex items-center gap-2">
                  <div className="flex-shrink-0">
                    {React.cloneElement(prompt.icon as React.ReactElement<any>, { size: 14 })}
                  </div>
                  <span className="whitespace-nowrap">{prompt.title}</span>
                </div>
              </Button>
            </motion.div>
          ))}
        </div>

        {/* Refresh button that appears on hover */}
        <Button
          variant="ghost"
          size="sm"
          onClick={handleRefresh}
          className="absolute -top-4 right-1 h-5 w-5 p-0 rounded-full opacity-0 group-hover:opacity-100 transition-opacity duration-200 hover:bg-neutral-100 dark:hover:bg-neutral-800"
        >
          <motion.div
            animate={{ rotate: isRefreshing ? 360 : 0 }}
            transition={{ duration: 0.3, ease: "easeInOut" }}
          >
            <RefreshCw size={10} className="text-muted-foreground" />
          </motion.div>
        </Button>
      </div>
    </div>
  );
};