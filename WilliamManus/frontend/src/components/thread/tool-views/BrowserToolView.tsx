import React, { useMemo, useEffect } from 'react';
import {
  Globe,
  MonitorPlay,
  ExternalLink,
  CheckCircle,
  AlertTriangle,
  CircleDashed,
} from 'lucide-react';
import { ToolViewProps } from './types';
import {
  extractBrowserUrl,
  extractBrowserOperation,
  formatTimestamp,
  getToolTitle,
  extractToolData,
} from './utils';
import { safeJsonParse } from '@/components/thread/utils';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ImageLoader } from './shared/ImageLoader';
import { useQueryClient } from '@tanstack/react-query';
import { threadKeys } from '@/hooks/react-query/threads/keys';

export function BrowserToolView({
  name = 'browser-operation',
  assistantContent,
  toolContent,
  assistantTimestamp,
  toolTimestamp,
  isSuccess = true,
  isStreaming = false,
  project,
  agentStatus = 'idle',
  messages = [],
  currentIndex = 0,
  totalCalls = 1,
}: ToolViewProps) {
  const queryClient = useQueryClient();
  
  // 🚀 主动刷新项目数据：如果没有VNC配置且工具正在执行
  useEffect(() => {
    const shouldRefreshProject = 
      project?.id && // 有项目ID
      !project.sandbox?.vnc_preview && // 没有VNC配置
      (isStreaming || agentStatus === 'running'); // 工具正在执行
    
    if (shouldRefreshProject) {
      console.log('🚀 [BrowserToolView] 检测到工具执行但无VNC配置，主动刷新项目数据');
      queryClient.invalidateQueries({ 
        queryKey: threadKeys.project(project.id) 
      });
    }
  }, [project, isStreaming, agentStatus, queryClient]);

  // 🔄 持续监听机制：在工具执行期间每1秒检查一次VNC配置 (更频繁)
  useEffect(() => {
    let intervalId: NodeJS.Timeout | null = null;
    
    const isToolExecuting = isStreaming || agentStatus === 'running';
    const hasVncConfig = project?.sandbox?.vnc_preview;
    
    if (project?.id && isToolExecuting && !hasVncConfig) {
      console.log('⏰ [BrowserToolView] 启动VNC配置持续监听 (每1秒检查一次)', {
        projectId: project.id,
        isStreaming,
        agentStatus,
        hasVncConfig,
        sandbox: project.sandbox
      });
      
      intervalId = setInterval(() => {
        console.log('🔄 [BrowserToolView] 持续检查VNC配置...', {
          timestamp: new Date().toISOString(),
          projectId: project.id
        });
        queryClient.invalidateQueries({ 
          queryKey: threadKeys.project(project.id) 
        });
      }, 1000); // 改为每1秒检查一次
    }
    
    // 清理定时器+
    return () => {
      if (intervalId) {
        console.log('🛑 [BrowserToolView] 清除VNC配置监听定时器');
        clearInterval(intervalId);
      }
    };
  }, [project?.id, project?.sandbox?.vnc_preview, isStreaming, agentStatus, queryClient]);

  // 📊 VNC配置状态监听
  useEffect(() => {
    if (project?.sandbox?.vnc_preview) {
      console.log('✅ [BrowserToolView] VNC配置已获取:', {
        vncUrl: project.sandbox.vnc_preview,
        isToolExecuting: isStreaming || agentStatus === 'running',
        timestamp: new Date().toISOString()
      });
    } else if (project?.id && (isStreaming || agentStatus === 'running')) {
      console.log('⚠️ [BrowserToolView] 工具执行中但VNC配置未就绪:', {
        projectId: project.id,
        sandbox: project.sandbox,
        isStreaming,
        agentStatus
      });
    }
  }, [project?.sandbox?.vnc_preview, project?.id, isStreaming, agentStatus]);
  
  // Try to extract data using the new parser first
  const assistantToolData = extractToolData(assistantContent);
  const toolToolData = extractToolData(toolContent);

  let url: string | null = null;

  // Use data from the new format if available
  if (assistantToolData.toolResult) {
    url = assistantToolData.url;
  } else if (toolToolData.toolResult) {
    url = toolToolData.url;
  }

  // If not found in new format, fall back to legacy extraction
  if (!url) {
    url = extractBrowserUrl(assistantContent);
  }

  const operation = extractBrowserOperation(name);
  const toolTitle = getToolTitle(name);

  let browserStateMessageId: string | undefined;
  let screenshotUrl: string | null = null;
  let screenshotBase64: string | null = null;
  let baseUrl: string | null = null;

  // Add loading states for images
  const [imageLoading, setImageLoading] = React.useState(true);
  const [imageError, setImageError] = React.useState(false);
  
  // 🎬 视频式切换状态
  const [currentImageSrc, setCurrentImageSrc] = React.useState<string | null>(null);
  const [nextImageSrc, setNextImageSrc] = React.useState<string | null>(null);
  const [isTransitioning, setIsTransitioning] = React.useState(false);
  const [displayMode, setDisplayMode] = React.useState<'contain' | 'cover' | 'fill'>('contain');

  // 🎯 新增：首先检查toolResult.toolOutput中的截图数据
  if (toolToolData.toolResult?.toolOutput) {
    try {
      const toolOutputJson = JSON.parse(toolToolData.toolResult.toolOutput);
      console.log('🎯 [BrowserToolView] 解析toolResult.toolOutput:', toolOutputJson);
      
      // Extract base URL
      if (toolOutputJson.base_url || toolOutputJson.baseUrl) {
        baseUrl = toolOutputJson.base_url || toolOutputJson.baseUrl;
      }
      
      // Extract screenshot URL
      if (toolOutputJson.image_url) {
        screenshotUrl = toolOutputJson.image_url;
      } else if (toolOutputJson.screenshot?.url) {
        screenshotUrl = toolOutputJson.screenshot.url;
      } else if (toolOutputJson.base64_data && toolOutputJson.base64_data.startsWith('http')) {
        screenshotUrl = toolOutputJson.base64_data;
      }
      
      // Handle base64 screenshot
      if (!screenshotUrl && toolOutputJson.screenshot?.base64) {
        screenshotBase64 = toolOutputJson.screenshot.base64;
      }
      
      console.log('✅ [BrowserToolView] 从toolResult提取截图数据:', {
        baseUrl,
        screenshotUrl,
        hasBase64: !!screenshotBase64
      });
    } catch (parseError) {
      console.warn('❌ [BrowserToolView] 解析toolResult.toolOutput失败:', parseError);
    }
  }

  // 🔧 **额外保障机制**: 从messages数组中查找最新的工具结果
  if (!screenshotUrl && !screenshotBase64 && messages && messages.length > 0) {
    console.log('🔧 [BrowserToolView] 保障机制启动，搜索messages数组:', {
      messagesCount: messages.length,
      currentToolName: name,
      toolMessages: messages.filter(msg => msg.type === 'tool').length
    });
    
    // 查找与当前工具名称匹配的最新tool消息
    const relevantToolMessages = messages.filter(msg => {
      if (msg.type !== 'tool' || !msg.metadata) return false;
      
      try {
        const metadata = JSON.parse(msg.metadata);
        const toolName = metadata.tool_name;
        const isMatch = toolName === name;
        
        console.log('🔍 [BrowserToolView] 检查工具消息:', {
          messageId: msg.message_id,
          toolName,
          currentName: name,
          isMatch
        });
        
        return isMatch;
      } catch (e) {
        console.warn('❌ [BrowserToolView] metadata解析失败:', e);
        return false;
      }
    }).sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());

    if (relevantToolMessages.length > 0) {
      const latestToolMessage = relevantToolMessages[0];
      try {
        const messageContent = JSON.parse(latestToolMessage.content);
        if (messageContent.result) {
          const resultJson = JSON.parse(messageContent.result);
          
          console.log('🔧 [BrowserToolView] 从messages数组找到工具结果:', {
            messageId: latestToolMessage.message_id,
            toolName: JSON.parse(latestToolMessage.metadata).tool_name,
            result: resultJson
          });
          
          // Extract screenshot data
          if (resultJson.image_url) {
            screenshotUrl = resultJson.image_url;
          } else if (resultJson.screenshot?.url) {
            screenshotUrl = resultJson.screenshot.url;
          } else if (resultJson.base64_data && resultJson.base64_data.startsWith('http')) {
            screenshotUrl = resultJson.base64_data;
          }
          
          if (!screenshotUrl && resultJson.screenshot?.base64) {
            screenshotBase64 = resultJson.screenshot.base64;
          }
          
          if (resultJson.base_url || resultJson.baseUrl) {
            baseUrl = resultJson.base_url || resultJson.baseUrl;
          }
          
          console.log('✅ [BrowserToolView] 从messages数组提取截图数据:', {
            baseUrl,
            screenshotUrl,
            hasBase64: !!screenshotBase64
          });
        }
      } catch (parseError) {
        console.warn('❌ [BrowserToolView] 解析messages工具结果失败:', parseError);
      }
    } else {
      // 🔧 **终极保障机制**: 如果工具名称匹配失败，寻找最近的包含截图的tool消息
      console.log('🚨 [BrowserToolView] 工具名称匹配失败，使用终极保障机制');
      
      const allToolMessages = messages.filter(msg => msg.type === 'tool')
        .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
      
      for (const toolMessage of allToolMessages.slice(0, 5)) { // 只检查最近5条
        try {
          const messageContent = JSON.parse(toolMessage.content);
          if (messageContent.result) {
            const resultJson = JSON.parse(messageContent.result);
            
            if (resultJson.image_url || resultJson.screenshot?.url || resultJson.base64_data) {
              console.log('🎯 [BrowserToolView] 找到包含截图的工具消息:', {
                messageId: toolMessage.message_id,
                toolName: JSON.parse(toolMessage.metadata || '{}').tool_name,
                hasImageUrl: !!resultJson.image_url,
                hasScreenshotUrl: !!resultJson.screenshot?.url,
                hasBase64: !!resultJson.base64_data
              });
              
              // Extract screenshot data
              if (resultJson.image_url) {
                screenshotUrl = resultJson.image_url;
              } else if (resultJson.screenshot?.url) {
                screenshotUrl = resultJson.screenshot.url;
              } else if (resultJson.base64_data && resultJson.base64_data.startsWith('http')) {
                screenshotUrl = resultJson.base64_data;
              }
              
              if (!screenshotUrl && resultJson.screenshot?.base64) {
                screenshotBase64 = resultJson.screenshot.base64;
              }
              
              if (resultJson.base_url || resultJson.baseUrl) {
                baseUrl = resultJson.base_url || resultJson.baseUrl;
              }
              
              console.log('🎯 [BrowserToolView] 终极保障机制提取截图数据:', {
                baseUrl,
                screenshotUrl,
                hasBase64: !!screenshotBase64
              });
              
              break; // 找到第一个有截图的消息就停止
            }
          }
        } catch (e) {
          console.warn('❌ [BrowserToolView] 终极保障机制解析失败:', e);
        }
      }
    }
  }

  try {
    const topLevelParsed = safeJsonParse<{ content?: any }>(toolContent, {});
    const innerContentString = topLevelParsed?.content || toolContent;
    if (innerContentString && typeof innerContentString === 'string') {
      const toolResultMatch = innerContentString.match(/ToolResult\([^)]*output='([\s\S]*?)'(?:\s*,|\s*\))/);
      if (toolResultMatch) {
        const outputString = toolResultMatch[1];
        try {
          const cleanedOutput = outputString.replace(/\\n/g, '\n').replace(/\\"/g, '"').replace(/\\u([0-9a-fA-F]{4})/g, (_match, grp) => String.fromCharCode(parseInt(grp, 16)));
          const outputJson = JSON.parse(cleanedOutput);

          // Extract base URL if available
          if (outputJson.base_url || outputJson.baseUrl) {
            baseUrl = outputJson.base_url || outputJson.baseUrl;
          }

                  // Extract screenshot URL - support multiple formats including browser-use format
          if (outputJson.image_url) {
            screenshotUrl = outputJson.image_url;
        } else if (outputJson.screenshot?.url) {
          screenshotUrl = outputJson.screenshot.url;
        } else if (outputJson.screenshot?.image_url) {
          screenshotUrl = outputJson.screenshot.image_url;
        } else if (outputJson.base64_data && outputJson.base64_data.startsWith('http')) {
          // Browser-use格式: base64_data作为URL使用
          screenshotUrl = outputJson.base64_data;
        }

          // Handle base64 screenshot if no URL
          if (!screenshotUrl && outputJson.screenshot?.base64) {
            screenshotBase64 = outputJson.screenshot.base64;
          }

          if (outputJson.message_id) {
            browserStateMessageId = outputJson.message_id;
          }

          console.log('🖼️ [BrowserToolView] 解析截图数据:', {
                      hasImageUrl: !!screenshotUrl,
          hasScreenshotUrl: !!outputJson.screenshot?.url,
          hasBase64: !!screenshotBase64,
          hasBase64DataUrl: !!(outputJson.base64_data && outputJson.base64_data.startsWith('http')),
          hasBaseUrl: !!baseUrl,
          screenshotUrl,
          baseUrl,
          base64DataValue: outputJson.base64_data
          });
        } catch (parseError) {
          console.warn('❌ [BrowserToolView] 解析截图JSON失败:', parseError);
        }
      }

      if (!screenshotUrl) {
        // Try multiple URL patterns including browser-use format
        const imageUrlMatch = innerContentString.match(/"image_url":\s*"([^"]+)"/);
        const screenshotUrlMatch = innerContentString.match(/"screenshot":\s*{[^}]*"url":\s*"([^"]+)"/);
        const base64DataUrlMatch = innerContentString.match(/"base64_data":\s*"(https?:\/\/[^"]+)"/);
        const baseUrlMatch = innerContentString.match(/"(?:base_url|baseUrl)":\s*"([^"]+)"/);
        
        if (imageUrlMatch) {
          screenshotUrl = imageUrlMatch[1];
        } else if (screenshotUrlMatch) {
          screenshotUrl = screenshotUrlMatch[1];
        } else if (base64DataUrlMatch) {
          // Browser-use格式的base64_data作为URL
          screenshotUrl = base64DataUrlMatch[1];
        }
        
        if (baseUrlMatch && !baseUrl) {
          baseUrl = baseUrlMatch[1];
        }
        
        console.log('🔍 [BrowserToolView] 正则提取结果:', {
          imageUrlMatch: !!imageUrlMatch,
          screenshotUrlMatch: !!screenshotUrlMatch,
          base64DataUrlMatch: !!base64DataUrlMatch,
          baseUrlMatch: !!baseUrlMatch,
          extractedUrl: screenshotUrl,
          extractedBaseUrl: baseUrl
        });
      }

      if (!browserStateMessageId) {
        const messageIdMatch = innerContentString.match(/"message_id":\s*"([^"]+)"/);
        if (messageIdMatch) {
          browserStateMessageId = messageIdMatch[1];
        }
      }

      if (!browserStateMessageId && !screenshotUrl) {
        const outputMatch = innerContentString.match(/\boutput='(.*?)'(?=\s*\))/);
        const outputString = outputMatch ? outputMatch[1] : null;

        if (outputString) {
          const unescapedOutput = outputString
            .replace(/\\n/g, '\n')
            .replace(/\\"/g, '"');

          const finalParsedOutput = safeJsonParse<{ message_id?: string; image_url?: string }>(
            unescapedOutput,
            {},
          );
          browserStateMessageId = finalParsedOutput?.message_id;
          screenshotUrl = finalParsedOutput?.image_url || null;
        }
      }
    } else if (innerContentString && typeof innerContentString === "object") {
      screenshotUrl = (() => {
        if (!innerContentString) return null;
        if (!("tool_execution" in innerContentString)) return null;
        if (!("result" in innerContentString.tool_execution)) return null;
        if (!("output" in innerContentString.tool_execution.result)) return null;
        if (!("image_url" in innerContentString.tool_execution.result.output)) return null;
        if (typeof innerContentString.tool_execution.result.output.image_url !== "string") return null;
        return innerContentString.tool_execution.result.output.image_url;
      })()
    }

  } catch (error) {
    console.warn('❌ [BrowserToolView] 截图数据解析出错:', error);
  }

  // 🔗 URL拼接逻辑：如果有baseUrl且screenshotUrl是相对路径，进行拼接
  if (baseUrl && screenshotUrl && !screenshotUrl.startsWith('http')) {
    const finalUrl = `${baseUrl.replace(/\/$/, '')}/${screenshotUrl.replace(/^\//, '')}`;
    console.log('🔗 [BrowserToolView] URL拼接:', {
      baseUrl,
      relativePath: screenshotUrl,
      finalUrl
    });
    screenshotUrl = finalUrl;
  }

  if (!screenshotUrl && !screenshotBase64 && browserStateMessageId && messages.length > 0) {
    const browserStateMessage = messages.find(
      (msg) =>
        (msg.type as string) === 'browser_state' &&
        msg.message_id === browserStateMessageId,
    );

    if (browserStateMessage) {
      const browserStateContent = safeJsonParse<{
        screenshot_base64?: string;
        image_url?: string;
      }>(
        browserStateMessage.content,
        {},
      );
      screenshotBase64 = browserStateContent?.screenshot_base64 || null;
      screenshotUrl = browserStateContent?.image_url || null;
    }
  }

  // Build complete VNC URL with reconnect configuration for long-running sessions
  const vncPreviewUrl = project?.sandbox?.vnc_preview && project?.sandbox?.pass
    ? `${project.sandbox.vnc_preview}/vnc_lite.html?password=${encodeURIComponent(project.sandbox.pass)}&autoconnect=true&reconnect=true&reconnect_delay=1000&scale=remote&resize=scale&show_dot=true`
    : undefined;

  const [vncReloadNonce, setVncReloadNonce] = React.useState(0);
  const [lastVncActivityAt, setLastVncActivityAt] = React.useState(() => Date.now());

  console.log('🔧 [BrowserToolView] VNC配置状态:', {
    hasVncPreview: !!project?.sandbox?.vnc_preview,
    hasPass: !!project?.sandbox?.pass,
    vncBase: project?.sandbox?.vnc_preview,
    vncPass: project?.sandbox?.pass ? '***' : 'missing',
    finalVncUrl: vncPreviewUrl ? 'generated' : 'missing'
  });
  
  if (vncPreviewUrl) {
    console.log('✅ [BrowserToolView] VNC URL complete:', vncPreviewUrl);
  } else {
    console.warn('❌ [BrowserToolView] VNC URL missing -需要vnc_preview和pass');
  }

  const isRunning = isStreaming || agentStatus === 'running';
  const isLastToolCall = currentIndex === totalCalls - 1;

  // Keep a lightweight activity clock so we can soft-reconnect if VNC freezes while the agent is running.
  useEffect(() => {
    if (!vncPreviewUrl) return;
    setLastVncActivityAt(Date.now());
  }, [vncPreviewUrl, toolTimestamp, assistantTimestamp, currentIndex, totalCalls, isStreaming, agentStatus]);

  useEffect(() => {
    if (!vncPreviewUrl || !isRunning) return;

    const watchdog = setInterval(() => {
      const idleMs = Date.now() - lastVncActivityAt;
      if (idleMs < 25000) {
        return;
      }

      console.warn('⚠️ [BrowserToolView] VNC idle for too long, forcing iframe reconnect', {
        idleMs,
        currentIndex,
        totalCalls,
      });
      setVncReloadNonce((prev) => prev + 1);
      setLastVncActivityAt(Date.now());
    }, 8000);

    return () => clearInterval(watchdog);
  }, [vncPreviewUrl, isRunning, lastVncActivityAt, currentIndex, totalCalls]);

  const vncIframe = useMemo(() => {
    if (!vncPreviewUrl) return null;

    return (
      <iframe
        key={`vnc-${vncReloadNonce}`}
        src={vncPreviewUrl}
        title="VNC Desktop Preview"
        className="w-full h-full border-0 rounded-lg"
        style={{ 
          width: '100%', 
          height: '100%',
          minHeight: '500px', // 最小高度保证可见性
          maxHeight: '80vh', // 最大高度避免过高
          backgroundColor: '#000' // 黑色背景
        }}
        allow="fullscreen"
        sandbox="allow-scripts allow-same-origin allow-forms allow-pointer-lock allow-top-navigation"
        loading="eager"
        onLoad={() => {
          setLastVncActivityAt(Date.now());
        }}
        onError={() => {
          console.warn('❌ [BrowserToolView] VNC iframe failed to load', {
            vncPreviewUrl,
            nonce: vncReloadNonce,
          });
          if (isRunning) {
            setVncReloadNonce((prev) => prev + 1);
          }
        }}
      />
    );
  }, [vncPreviewUrl, vncReloadNonce, isRunning]);

  // 🚀 Smart render decision: 根据工具类型决定显示策略
  const isDesktopTool = name && ['move_to', 'move-to', 'click', 'scroll', 'typing', 'press', 'wait', 
                                'mouse_down', 'mouse-down', 'mouse_up', 'mouse-up', 
                                'drag_to', 'drag-to', 'screenshot', 'hotkey', 'key', 'type'].includes(name);
  const isBrowserUseTool = name && (name.startsWith('browser_') || name.startsWith('browser-'));
  
  // 🔧 修复：即使在streaming状态下，也要检查是否有截图数据
  const hasScreenshotData = !!(screenshotUrl || screenshotBase64);
  const renderDecision = (isDesktopTool || isBrowserUseTool) && vncIframe ? 'DESKTOP_VNC' :
                        isBrowserUseTool && hasScreenshotData ? 'BROWSER_SCREENSHOT' :
                        hasScreenshotData ? 'SCREENSHOT_FALLBACK' :
                        vncIframe ? 'VNC_FALLBACK' :
                        'NO_PREVIEW';
            
  console.log('🎯 [BrowserToolView] Smart render decision:', {
    decision: renderDecision,
    toolType: {
      isDesktopTool,
      isBrowserUseTool,
      toolName: name
    },
    isRunning,
    isStreaming,
    agentStatus,
    hasVnc: !!vncIframe,
    hasScreenshot: !!(screenshotUrl || screenshotBase64),
    screenshotInfo: {
      hasUrl: !!screenshotUrl,
      hasBase64: !!screenshotBase64,
      url: screenshotUrl,
      baseUrl: baseUrl,
      isRelativePath: screenshotUrl ? !screenshotUrl.startsWith('http') : false
    },
    vncUrl: vncPreviewUrl,
    projectId: project?.id,
    sandboxConfig: project?.sandbox,
    currentIndex,
    totalCalls
  });

  const [progress, setProgress] = React.useState(100);

  React.useEffect(() => {
    if (isRunning) {
      setProgress(0);
      const timer = setInterval(() => {
        setProgress((prevProgress) => {
          if (prevProgress >= 95) {
            clearInterval(timer);
            return prevProgress;
          }
          return prevProgress + 2;
        });
      }, 500);
      return () => clearInterval(timer);
    } else {
      setProgress(100);
    }
  }, [isRunning]);

  // 🎬 视频式切换逻辑 - 平滑切换截图
  React.useEffect(() => {
    const newImageSrc = screenshotUrl || (screenshotBase64 ? `data:image/jpeg;base64,${screenshotBase64}` : null);
    
    if (!newImageSrc) {
      setCurrentImageSrc(null);
      setNextImageSrc(null);
      return;
    }
    
    // 如果是第一张图片，直接设置
    if (!currentImageSrc) {
      console.log('🎬 [BrowserToolView] 设置第一张图片:', newImageSrc.substring(0, 50) + '...');
      setCurrentImageSrc(newImageSrc);
      setImageLoading(true);
      setImageError(false);
      return;
    }
    
    // 如果图片相同，不需要切换
    if (currentImageSrc === newImageSrc) {
      return;
    }
    
    console.log('🎬 [BrowserToolView] 开始图片切换:', {
      from: currentImageSrc?.substring(0, 50) + '...',
      to: newImageSrc.substring(0, 50) + '...'
    });
    
    // 开始平滑切换
    setNextImageSrc(newImageSrc);
    setIsTransitioning(true);
    
    // 预加载新图片
    const preloader = new Image();
    preloader.onload = () => {
      console.log('✅ [BrowserToolView] 新图片预加载完成，开始切换');
      
      // 延迟一帧确保过渡动画生效
      requestAnimationFrame(() => {
        setCurrentImageSrc(newImageSrc);
        setNextImageSrc(null);
        
        // 切换动画持续时间后重置状态
        setTimeout(() => {
          setIsTransitioning(false);
        }, 300); // 300ms 切换动画
      });
    };
    
    preloader.onerror = () => {
      console.warn('❌ [BrowserToolView] 新图片预加载失败');
      setIsTransitioning(false);
      setImageError(true);
    };
    
    preloader.src = newImageSrc;
  }, [screenshotUrl, screenshotBase64, currentImageSrc]);

  const handleImageLoad = () => {
    console.log('✅ [BrowserToolView] 图片加载完成');
    setImageLoading(false);
    setImageError(false);
  };

  const handleImageError = () => {
    console.warn('❌ [BrowserToolView] 图片加载失败');
    setImageLoading(false);
    setImageError(true);
    setIsTransitioning(false); // 加载失败时停止切换动画
  };

  const renderScreenshot = () => {

    if (screenshotUrl) {
      return (
        <div className="flex items-center justify-center w-full h-full min-h-[600px] relative p-4" style={{ minHeight: '600px' }}>
          {imageLoading && (
            <ImageLoader />
          )}
          <Card className={`p-0 overflow-hidden border ${imageLoading ? 'hidden' : 'block'}`}>
            <img
              src={screenshotUrl}
              alt="Browser Screenshot"
              className="max-w-full max-h-full object-contain"
              onLoad={handleImageLoad}
              onError={handleImageError}
            />
          </Card>
          {imageError && !imageLoading && (
            <div className="absolute inset-0 flex items-center justify-center bg-zinc-50 dark:bg-zinc-900">
              <div className="text-center text-zinc-500 dark:text-zinc-400">
                <AlertTriangle className="h-8 w-8 mx-auto mb-2" />
                <p>Failed to load screenshot</p>
              </div>
            </div>
          )}
        </div>
      );
    } else if (screenshotBase64) {
      return (
        <div className="flex items-center justify-center w-full h-full min-h-[600px] relative p-4" style={{ minHeight: '600px' }}>
          {imageLoading && (
            <ImageLoader />
          )}
          <Card className={`overflow-hidden border ${imageLoading ? 'hidden' : 'block'}`}>
            <img
              src={`data:image/jpeg;base64,${screenshotBase64}`}
              alt="Browser Screenshot"
              className="max-w-full max-h-full object-contain"
              onLoad={handleImageLoad}
              onError={handleImageError}
            />
          </Card>
          {imageError && !imageLoading && (
            <div className="absolute inset-0 flex items-center justify-center bg-zinc-50 dark:bg-zinc-900">
              <div className="text-center text-zinc-500 dark:text-zinc-400">
                <AlertTriangle className="h-8 w-8 mx-auto mb-2" />
                <p>Failed to load screenshot</p>
              </div>
            </div>
          )}
        </div>
      );
    }
    return null;
  };

  return (
    <Card className="gap-0 flex border shadow-none border-t border-b-0 border-x-0 p-0 rounded-none flex-col h-full overflow-hidden bg-card">
      <CardHeader className="h-14 bg-zinc-50/80 dark:bg-zinc-900/80 backdrop-blur-sm border-b p-2 px-4 space-y-2">
        <div className="flex flex-row items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="relative p-2 rounded-lg bg-gradient-to-br from-purple-500/20 to-purple-600/10 border border-purple-500/20">
              <MonitorPlay className="w-5 h-5 text-purple-500 dark:text-purple-400" />
            </div>
            <div>
              <CardTitle className="text-base font-medium text-zinc-900 dark:text-zinc-100">
                {toolTitle}
              </CardTitle>
            </div>
          </div>

          {!isRunning && (
            <Badge
              variant="secondary"
              className={
                isSuccess
                  ? "bg-gradient-to-b from-emerald-200 to-emerald-100 text-emerald-700 dark:from-emerald-800/50 dark:to-emerald-900/60 dark:text-emerald-300"
                  : "bg-gradient-to-b from-rose-200 to-rose-100 text-rose-700 dark:from-rose-800/50 dark:to-rose-900/60 dark:text-rose-300"
              }
            >
              {isSuccess ? (
                <CheckCircle className="h-3.5 w-3.5 mr-1" />
              ) : (
                <AlertTriangle className="h-3.5 w-3.5 mr-1" />
              )}
              {isSuccess ? 'Browser action completed' : 'Browser action failed'}
            </Badge>
          )}

          {isRunning && (
            <Badge className="bg-gradient-to-b from-blue-200 to-blue-100 text-blue-700 dark:from-blue-800/50 dark:to-blue-900/60 dark:text-blue-300">
              <CircleDashed className="h-3.5 w-3.5 animate-spin" />
              Executing browser action
            </Badge>
          )}
        </div>
      </CardHeader>

      <CardContent className="p-0 flex-1 overflow-hidden relative">
        <div className="flex-1 flex items-stretch min-h-[600px] tool-panel-surface">
          {/* 🚀 智能渲染逻辑：根据工具类型决定显示内容 */}
          {renderDecision === 'DESKTOP_VNC' ? (
            // Desktop工具 + VNC可用：显示VNC实时桌面流
            <div className="flex flex-col w-full h-full">
              <div className="relative w-full h-full flex-1">
                {vncIframe}
                <div className="absolute top-4 right-4 z-10 flex gap-2">
                  <Badge className={isRunning 
                    ? "bg-blue-500/90 text-white border-none shadow-lg animate-pulse"
                    : "bg-green-500/90 text-white border-none shadow-lg"
                  }>
                    {isRunning ? (
                      <>
                        <CircleDashed className="h-3 w-3 animate-spin" />
                        {operation} executing
                      </>
                    ) : (
                      <>
                        <CheckCircle className="h-3 w-3" />
                        VNC connected
                      </>
                    )}
                  </Badge>
                  <button
                    onClick={() => {
                      const iframe = document.querySelector('iframe[title="VNC Desktop Preview"]');
                      if (iframe) {
                        if (iframe.requestFullscreen) {
                          iframe.requestFullscreen();
                        }
                      }
                    }}
                    className="bg-black/70 hover:bg-black/90 text-white px-2 py-1 rounded text-xs flex items-center gap-1 shadow-lg transition-colors"
                    title="全屏显示VNC"
                  >
                    <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
                    </svg>
                    全屏
                  </button>
                </div>
              </div>
            </div>
          ) : (renderDecision === 'BROWSER_SCREENSHOT' || renderDecision === 'SCREENSHOT_FALLBACK') ? (
            // 🎬 Browser-Use工具：视频式截图切换 + 全屏显示
            <div className="flex flex-col w-full h-full">
              {/* 🎛️ 显示模式控制栏 */}
              <div className="flex justify-between items-center p-2 bg-zinc-50 dark:bg-zinc-900 border-b">
                <div className="flex items-center gap-2">
                  <span className="text-xs text-zinc-600 dark:text-zinc-400">显示模式:</span>
                  <div className="flex rounded overflow-hidden border border-zinc-200 dark:border-zinc-700">
                    {(['contain', 'cover', 'fill'] as const).map((mode) => (
                      <button
                        key={mode}
                        onClick={() => setDisplayMode(mode)}
                        className={`px-2 py-1 text-xs transition-colors ${
                          displayMode === mode
                            ? 'bg-blue-500 text-white'
                            : 'bg-white dark:bg-zinc-800 text-zinc-600 dark:text-zinc-400 hover:bg-zinc-100 dark:hover:bg-zinc-700'
                        }`}
                      >
                        {mode === 'contain' ? '适应' : mode === 'cover' ? '填充' : '拉伸'}
                      </button>
                    ))}
                  </div>
                </div>
                
                <button
                  onClick={() => {
                    const container = document.querySelector('.browser-screenshot-container');
                    if (container && container.requestFullscreen) {
                      container.requestFullscreen();
                    }
                  }}
                  className="flex items-center gap-1 px-2 py-1 text-xs bg-black/70 hover:bg-black/90 text-white rounded transition-colors"
                  title="全屏显示"
                >
                  <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
                  </svg>
                  全屏
                </button>
              </div>

              {/* 🎬 双缓冲图片容器 */}
              <div className="browser-screenshot-container flex-1 relative overflow-hidden bg-black min-h-[500px]">
                {(imageLoading && !currentImageSrc) && <ImageLoader />}
                
                {/* 🔍 调试：显示当前状态 */}
                {!currentImageSrc && !imageLoading && (
                  <div className="absolute inset-0 flex items-center justify-center text-white text-sm">
                    <div className="text-center">
                      <div className="mb-2">🔍 等待截图数据...</div>
                      <div className="text-xs opacity-60">
                        screenshotUrl: {screenshotUrl ? '✅' : '❌'}<br/>
                        screenshotBase64: {screenshotBase64 ? '✅' : '❌'}<br/>
                        renderDecision: {renderDecision}
                      </div>
                    </div>
                  </div>
                )}
                
                {/* 当前显示的图片 */}
                {currentImageSrc && (
                  <img
                    src={currentImageSrc}
                    alt="Browser Screenshot"
                    className={`absolute inset-0 w-full h-full transition-opacity duration-300 ${
                      displayMode === 'contain' ? 'object-contain' :
                      displayMode === 'cover' ? 'object-cover' : 'object-fill'
                    } ${isTransitioning ? 'opacity-0' : 'opacity-100'}`}
                    style={{ maxWidth: '100%', maxHeight: '100%' }}
                    onLoad={handleImageLoad}
                    onError={handleImageError}
                  />
                )}
                
                {/* 下一张图片（用于平滑切换） */}
                {nextImageSrc && (
                  <img
                    src={nextImageSrc}
                    alt="Browser Screenshot (Next)"
                    className={`absolute inset-0 w-full h-full transition-opacity duration-300 ${
                      displayMode === 'contain' ? 'object-contain' :
                      displayMode === 'cover' ? 'object-cover' : 'object-fill'
                    } ${isTransitioning ? 'opacity-100' : 'opacity-0'}`}
                    style={{ maxWidth: '100%', maxHeight: '100%' }}
                  />
                )}
                
                {/* 🎬 切换指示器 */}
                {isTransitioning && (
                  <div className="absolute top-4 right-4 bg-black/70 text-white px-2 py-1 rounded text-xs flex items-center gap-1">
                    <div className="w-2 h-2 bg-blue-400 rounded-full animate-pulse"></div>
                    切换中...
                  </div>
                )}
                
                {/* 错误状态 */}
                {imageError && !currentImageSrc && (
                  <div className="absolute inset-0 flex items-center justify-center bg-zinc-50 dark:bg-zinc-900">
                    <div className="text-center text-zinc-500 dark:text-zinc-400">
                      <AlertTriangle className="h-8 w-8 mx-auto mb-2" />
                      <p>Failed to load screenshot</p>
                    </div>
                  </div>
                )}
              </div>
            </div>
          ) : renderDecision === 'VNC_FALLBACK' ? (
            // VNC Fallback：其他情况下显示VNC（如果可用）
            <div className="flex flex-col w-full h-full">
              <div className="relative w-full h-full flex-1">
                {vncIframe}
                <div className="absolute top-4 right-4 z-10 flex gap-2">
                  <Badge className="bg-orange-500/90 text-white border-none shadow-lg">
                    <MonitorPlay className="h-3 w-3" />
                    VNC Fallback
                  </Badge>
                  <button
                    onClick={() => {
                      const iframe = document.querySelector('iframe[title="VNC Desktop Preview"]');
                      if (iframe) {
                        if (iframe.requestFullscreen) {
                          iframe.requestFullscreen();
                        }
                      }
                    }}
                    className="bg-black/70 hover:bg-black/90 text-white px-2 py-1 rounded text-xs flex items-center gap-1 shadow-lg transition-colors"
                    title="全屏显示VNC"
                  >
                    <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
                    </svg>
                    全屏
                  </button>
                </div>
              </div>
            </div>
          ) : (
            // 场景：没有可显示的内容 → 显示提示信息
            <div className="p-8 flex flex-col items-center justify-center w-full tool-panel-surface text-zinc-700 dark:text-zinc-400">
              <div className="w-20 h-20 rounded-full flex items-center justify-center mb-6 bg-gradient-to-b from-purple-100 to-purple-50 shadow-inner dark:from-purple-800/40 dark:to-purple-900/60">
                <MonitorPlay className="h-10 w-10 text-purple-400 dark:text-purple-600" />
              </div>
                <h3 className="text-xl font-semibold mb-2 text-zinc-900 dark:text-zinc-100">
                {isBrowserUseTool ? 'Browser preview not available' : 'Preview not available'}
              </h3>
              
              {/* 🔍 调试信息显示 */}
              {(isStreaming || agentStatus === 'running') && (
                <div className="mt-4 p-4 bg-blue-50 dark:bg-blue-900/20 rounded-lg border border-blue-200 dark:border-blue-800 text-sm">
                  <div className="flex items-center gap-2 mb-2">
                    <CircleDashed className="h-4 w-4 animate-spin text-blue-500" />
                    <span className="text-blue-700 dark:text-blue-300 font-medium">
                      {isDesktopTool ? '工具执行中，正在获取VNC配置...' : 
                       isBrowserUseTool ? '浏览器工具执行中，优先等待VNC连接...' :
                       '工具执行中...'}
                    </span>
                  </div>
                  <div className="text-xs text-blue-600 dark:text-blue-400 space-y-1">
                    <div>项目ID: {project?.id || 'N/A'}</div>
                    <div>工具状态: isStreaming={String(isStreaming)}, agentStatus={agentStatus}</div>
                    <div>VNC基础URL: {project?.sandbox?.vnc_preview || '等待中...'}</div>
                    <div>VNC密码: {project?.sandbox?.pass ? '已配置' : '缺失'}</div>
                    <div>完整VNC URL: {vncPreviewUrl ? '已生成' : '等待配置'}</div>
                    <div>当前时间: {new Date().toLocaleTimeString()}</div>
                  </div>
                </div>
              )}
              
                             {/* 🔍 始终显示调试信息 */}
                                               <div className="mt-4 p-3 bg-blue-50 dark:bg-blue-900/30 rounded border text-xs">
                  <div className="font-medium mb-2 text-blue-700 dark:text-blue-300">🔧 工具输出状态诊断</div>
                  <div className="space-y-1 text-blue-600 dark:text-blue-400">
                    <div>渲染模式: <span className="font-mono font-semibold">{renderDecision}</span></div>
                    <div>VNC配置: <span className="font-mono">
                      {project?.sandbox?.vnc_preview && project?.sandbox?.pass ? '✅ 完整' : '❌ 缺失'}
                    </span></div>
                    <div>截图数据: <span className="font-mono">
                      {screenshotUrl ? '✅ URL可用' : 
                       screenshotBase64 ? '✅ Base64可用' : '❌ 无截图数据'}
                    </span></div>
                    {screenshotUrl && (
                      <div className="text-xs text-gray-600 dark:text-gray-400">
                        📷 截图URL: <span className="font-mono break-all">{screenshotUrl}</span>
                      </div>
                    )}
                    {baseUrl && (
                      <div className="text-xs text-gray-600 dark:text-gray-400">
                        🔗 Base URL: <span className="font-mono">{baseUrl}</span>
                      </div>
                    )}
                    <div>工具执行状态: <span className="font-mono">
                      {isRunning ? '🔄 执行中' : '✅ 完成'}
                    </span></div>
                    <div className="mt-2 pt-2 border-t border-blue-200 dark:border-blue-700">
                      <div className="text-xs text-blue-500 dark:text-blue-400">
                        💡 {isDesktopTool ? '优先级: VNC实时流 > 截图备用' : 
                            isBrowserUseTool ? '优先级: VNC实时流 > 截图URL > Base64截图' :
                            '优先级: 截图URL > VNC备用 > Base64'}
                      </div>
                      <div className="text-xs text-blue-500 dark:text-blue-400">
                        🎯 {isBrowserUseTool ? '浏览器工具优先通过VNC实时显示操作过程' :
                            isDesktopTool ? '桌面工具通过VNC实时显示操作过程' :
                            '工具输出自适应显示'}
                      </div>
                      <div className="text-xs text-blue-500 dark:text-blue-400 mt-1">
                        🔍 {isBrowserUseTool ? '等待VNC配置（或截图回退数据）...' :
                            isDesktopTool ? '等待VNC配置和连接...' :
                            '等待工具输出数据...'}
                      </div>
                    </div>
                  </div>
                </div>
              <p className="text-sm text-zinc-500 dark:text-zinc-400 text-center max-w-sm">
                {isDesktopTool ? 'VNC connection not configured for this sandbox environment' :
                 isBrowserUseTool ? 'Neither VNC nor screenshot data is available from browser operation' :
                 'No preview data available for this tool'}
              </p>
              {url && (
                <div className="mt-4">
                  <Button
                    variant="outline"
                    size="sm"
                    className="bg-white dark:bg-zinc-900 border-zinc-200 dark:border-zinc-700 shadow-sm hover:shadow-md transition-shadow"
                    asChild
                  >
                    <a href={url} target="_blank" rel="noopener noreferrer">
                      <ExternalLink className="h-3.5 w-3.5 mr-2" />
                      Visit URL
                    </a>
                  </Button>
                </div>
              )}
              </div>
            )}
        </div>
      </CardContent>

      <div className="px-4 py-2 h-10 bg-gradient-to-r from-zinc-50/90 to-zinc-100/90 dark:from-zinc-900/90 dark:to-zinc-800/90 backdrop-blur-sm border-t border-zinc-200 dark:border-zinc-800 flex justify-between items-center gap-4">
        <div className="h-full flex items-center gap-2 text-sm text-zinc-500 dark:text-zinc-400">
          {!isRunning && (
            <Badge className="h-6 py-0.5">
              <Globe className="h-3 w-3" />
              {operation}
            </Badge>
          )}
          {url && (
            <span className="text-xs truncate max-w-[200px] hidden sm:inline-block">
              {url}
            </span>
          )}
        </div>

        <div className="text-xs text-zinc-500 dark:text-zinc-400">
          {toolTimestamp && !isRunning
            ? formatTimestamp(toolTimestamp)
            : assistantTimestamp
              ? formatTimestamp(assistantTimestamp)
              : ''}
        </div>
      </div>
    </Card>
  );
}
