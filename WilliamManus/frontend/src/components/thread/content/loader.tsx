import React, { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { AnimatedShinyText } from '@/components/ui/animated-shiny-text';

const items = [
    { id: 1, content: "初始化推理链路..." },
    { id: 2, content: "分析问题复杂度..." },
    { id: 3, content: "构建思维框架..." },
    { id: 4, content: "编排思考流程..." },
    { id: 5, content: "整合上下文理解..." },
    { id: 6, content: "校准响应参数..." },
    { id: 7, content: "启动推理算法..." },
    { id: 8, content: "处理语义结构..." },
    { id: 9, content: "制定策略方案..." },
    { id: 10, content: "优化解题路径..." },
    { id: 11, content: "协调数据流..." },
    { id: 12, content: "构思智能回复..." },
    { id: 13, content: "微调认知模型..." },
    { id: 14, content: "编织叙事脉络..." },
    { id: 15, content: "凝练关键洞察..." },
    { id: 16, content: "准备全面分析..." }
  ];

export const AgentLoader = () => {
  const [index, setIndex] = useState(0);
  useEffect(() => {
    const id = setInterval(() => {
      setIndex((state) => {
        if (state >= items.length - 1) return 0;
        return state + 1;
      });
    }, 1500);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="flex py-2 items-center w-full">
      <div>✨</div>
      <AnimatePresence>
      <motion.div
          key={items[index].id}
          initial={{ y: 20, opacity: 0, filter: "blur(8px)" }}
          animate={{ y: 0, opacity: 1, filter: "blur(0px)" }}
          exit={{ y: -20, opacity: 0, filter: "blur(8px)" }}
          transition={{ ease: "easeInOut" }}
          style={{ position: "absolute" }}
          className='ml-7'
      >
          <AnimatedShinyText className='text-xs'>{items[index].content}</AnimatedShinyText>
      </motion.div>
      </AnimatePresence>
    </div>
  );
};
