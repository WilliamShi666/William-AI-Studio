export interface Task {
  id: string
  content: string
  status: "pending" | "completed" | "cancelled"
  section_id: string
}

export interface Section {
  id: string
  title: string
  tasks: Task[]
}

export interface TaskListData {
  sections: Section[]
  total_tasks?: number
  total_sections?: number
  message?: string
}

export function extractTaskListData(
    assistantContent?: string, 
    toolContent?: string
  ): TaskListData | null {
    // Debug logging
    console.log('🔍 [extractTaskListData] Input:', {
      assistantContent: typeof assistantContent,
      assistantContentPreview: typeof assistantContent === 'string' ? assistantContent.slice(0, 200) + '...' : assistantContent,
      toolContent: typeof toolContent,
      toolContentPreview: typeof toolContent === 'string' ? toolContent.slice(0, 200) + '...' : toolContent,
    });
    const parseContent = (content: any): any => {
      if (typeof content === 'string') {
        try {
          return JSON.parse(content);
        } catch (e) {
          return content;
        }
      }
      return content;
    };
  
    const extractFromNewFormat = (content: any): TaskListData | null => {
      const parsedContent = parseContent(content);
      
      if (!parsedContent || typeof parsedContent !== 'object') {
        return null;
      }

      // 🎯 新增：直接检查是否是任务数据格式（用于streaming状态）
      if (parsedContent.sections && Array.isArray(parsedContent.sections)) {
        console.log('✅ [extractTaskListData] Found direct sections format:', {
          sectionsCount: parsedContent.sections.length,
          totalTasks: parsedContent.total_tasks
        });
        
        // 确保每个task都有必要的字段
        const normalizedSections = parsedContent.sections.map((section: any, sectionIndex: number) => ({
          id: section.id || `section-${sectionIndex}`,
          title: section.title || 'Untitled Section',
          tasks: Array.isArray(section.tasks) 
            ? section.tasks.map((task: string | any, taskIndex: number) => {
                if (typeof task === 'string') {
                  return {
                    id: `task-${sectionIndex}-${taskIndex}`,
                    content: task,
                    status: 'pending',
                    section_id: section.id || `section-${sectionIndex}`
                  };
                }
                return {
                  id: task.id || `task-${sectionIndex}-${taskIndex}`,
                  content: task.content || task,
                  status: task.status || 'pending',
                  section_id: task.section_id || section.id || `section-${sectionIndex}`
                };
              })
            : []
        }));
        
        const totalTasks = normalizedSections.reduce((sum: number, section: any) => 
          sum + (section.tasks?.length || 0), 0
        );
        
        return {
          sections: normalizedSections,
          total_tasks: parsedContent.total_tasks || totalTasks,
          total_sections: parsedContent.total_sections || normalizedSections.length
        };
      }

      // Check for tool_call_chunk format with nested JSON in arguments
      if (parsedContent.status_type === 'tool_call_chunk' && parsedContent.tool_call_chunk?.function?.arguments) {
        const argumentsContent = parsedContent.tool_call_chunk.function.arguments;
        
        // If arguments is a string, parse it
        if (typeof argumentsContent === 'string') {
          try {
            const argumentsData = JSON.parse(argumentsContent);
            if (argumentsData.sections && Array.isArray(argumentsData.sections)) {
              // Convert simple task arrays to full task objects for tool_call_chunk
              const convertedSections = argumentsData.sections.map((section: any, sectionIndex: number) => ({
                id: `section-${sectionIndex}`,
                title: section.title,
                tasks: Array.isArray(section.tasks) 
                  ? section.tasks.map((task: string | any, taskIndex: number) => {
                      if (typeof task === 'string') {
                        return {
                          id: `task-${sectionIndex}-${taskIndex}`,
                          content: task,
                          status: 'pending',
                          section_id: `section-${sectionIndex}`
                        };
                      }
                      return task; // Already a complete task object
                    })
                  : []
              }));
              
              const totalTasks = convertedSections.reduce((sum: number, section: any) => 
                sum + (section.tasks?.length || 0), 0
              );
              return { 
                sections: convertedSections,
                total_tasks: totalTasks,
                total_sections: convertedSections.length
              };
            }
          } catch (e) {
            // If parsing fails, continue with other checks
          }
        } else if (argumentsContent?.sections && Array.isArray(argumentsContent.sections)) {
          // If arguments is already an object
          const totalTasks = argumentsContent.sections.reduce((sum: number, section: any) => 
            sum + (section.tasks?.length || 0), 0
          );
          return { 
            sections: argumentsContent.sections,
            total_tasks: totalTasks,
            total_sections: argumentsContent.sections.length
          };
        }
      }
  
      // Check for tool result format (from tool execution)
      // Support both underscore and hyphen formats
      const taskToolNames = [
        'create_tasks', 'create-tasks',
        'update_tasks', 'update-tasks',
        'view_tasks', 'view-tasks',
        'delete_tasks', 'delete-tasks'
      ];
      
      if (parsedContent.result && taskToolNames.includes(parsedContent.tool_name)) {
        console.log('✅ [extractTaskListData] Found task tool result:', {
          tool_name: parsedContent.tool_name,
          hasResult: !!parsedContent.result,
          resultType: typeof parsedContent.result
        });
        
        const resultData = parseContent(parsedContent.result);
        console.log('🔍 [extractTaskListData] Parsed result data:', {
          hasSections: !!resultData?.sections,
          sectionsIsArray: Array.isArray(resultData?.sections),
          sectionsLength: resultData?.sections?.length || 0,
          resultDataKeys: resultData ? Object.keys(resultData) : []
        });
        
        if (resultData?.sections && Array.isArray(resultData.sections)) {
          console.log('✅ [extractTaskListData] Successfully extracted sections:', {
            sectionsCount: resultData.sections.length,
            totalTasks: resultData.total_tasks,
            firstSectionTitle: resultData.sections[0]?.title
          });
          return { 
            sections: resultData.sections, 
            total_tasks: resultData.total_tasks, 
            total_sections: resultData.total_sections 
          };
        }
      }

      // Check for tool_execution format
      if (parsedContent.tool_execution?.result?.output) {
        const output = parsedContent.tool_execution.result.output;
        const outputData = parseContent(output);
        
        // Nested sections format
        if (outputData?.sections && Array.isArray(outputData.sections)) {
          return { sections: outputData.sections, total_tasks: outputData.total_tasks, total_sections: outputData.total_sections };
        }
      }
  
      // Check for direct sections array
      if (parsedContent.sections && Array.isArray(parsedContent.sections)) {
        return { sections: parsedContent.sections };
      }
  
      // Check for nested content
      if (parsedContent.content) {
        return extractFromNewFormat(parsedContent.content);
      }
  
      return null;
    };


  
    // Try tool content first, then assistant content
    const result = extractFromNewFormat(toolContent) || extractFromNewFormat(assistantContent);
    
    console.log('🎯 [extractTaskListData] Result:', {
      hasResult: !!result,
      sectionsCount: result?.sections?.length || 0,
      totalTasks: result?.total_tasks || 0,
      firstSectionPreview: result?.sections?.[0] ? {
        title: result.sections[0].title,
        tasksCount: result.sections[0].tasks?.length || 0
      } : null
    });
    
    return result;
  }