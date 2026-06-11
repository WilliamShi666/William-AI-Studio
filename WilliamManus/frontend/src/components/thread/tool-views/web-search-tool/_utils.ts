import { extractToolData, extractSearchQuery, extractSearchResults } from '../utils';

export interface WebSearchData {
  query: string | null;
  results: Array<{ title: string; url: string; snippet?: string }>;
  answer: string | null;
  images: string[];
  success?: boolean;
  timestamp?: string;
}

const parseContent = (content: any): any => {
  if (typeof content === 'string') {
    try {
      const parsed = JSON.parse(content);
      // Check if the parsed result is also a JSON string (double-encoded)
      if (typeof parsed === 'string') {
        try {
          return JSON.parse(parsed);
        } catch (e) {
          return parsed;
        }
      }
      return parsed;
    } catch (e) {
      return content; 
    }
  }
  return content;
};

const extractFromNewFormat = (content: any): WebSearchData => {
  const parsedContent = parseContent(content);
  
  if (!parsedContent || typeof parsedContent !== 'object') {
    return { query: null, results: [], answer: null, images: [], success: undefined, timestamp: undefined };
  }

  // Handle format with tool_name and result (your backend format)
  if ('tool_name' in parsedContent && 'result' in parsedContent) {
    console.log('🔍 [Format] tool_name + result format detected');
    let result = parsedContent.result;
    
    // Handle case where result is a JSON string (double-encoded)
    if (typeof result === 'string') {
      try {
        result = JSON.parse(result);
        console.log('🔍 [Format] Parsed double-encoded result:', result);
      } catch (e) {
        console.log('🔍 [Format] Failed to parse result string:', e);
        return { query: null, results: [], answer: null, images: [], success: undefined, timestamp: undefined };
      }
    }
    
    if (result && typeof result === 'object') {
      const extractedData = {
        query: result.query || null,
        results: result.results?.map((resultItem: any) => ({
          title: resultItem.title || '',
          url: resultItem.url || '',
          snippet: resultItem.content || resultItem.snippet || ''
        })) || [],
        answer: result.answer || null,
        images: result.images || [],
        success: true,
        timestamp: undefined
      };
      console.log('🔍 [Format] Extracted from tool_name+result:', {
        query: extractedData.query,
        resultsCount: extractedData.results.length,
        firstResult: extractedData.results[0]
      });
      return extractedData;
    }
  }

  // Handle OpenAI tool call format in toolContent
  if ('result' in parsedContent && typeof parsedContent.result === 'object') {
    console.log('🔍 [Format] result object format detected');
    const result = parsedContent.result;
    const extractedData = {
      query: result.query || null,
      results: result.results?.map((result: any) => ({
        title: result.title || '',
        url: result.url || '',
        snippet: result.content || result.snippet || ''
      })) || [],
      answer: result.answer || null,
      images: result.images || [],
      success: true,
      timestamp: undefined
    };
    return extractedData;
  }

  // Handle format with name and content fields (like MCP tools)
  if ('name' in parsedContent && 'content' in parsedContent) {
    console.log('🔍 [Format] name + content format detected, recursing');
    return extractFromNewFormat(parsedContent.content);
  }

  // Handle OpenAI tool call format in assistantContent
  if ('tool_calls' in parsedContent && Array.isArray(parsedContent.tool_calls)) {
    console.log('🔍 [Format] tool_calls format detected');
    const toolCall = parsedContent.tool_calls[0];
    if (toolCall && toolCall.function) {
      const args = toolCall.function.arguments || {};
      return {
        query: args.query || null,
        results: [],
        answer: null,
        images: [],
        success: undefined,
        timestamp: undefined
      };
    }
  }

  if ('tool_execution' in parsedContent && typeof parsedContent.tool_execution === 'object') {
    console.log('🔍 [Format] tool_execution format detected');
    const toolExecution = parsedContent.tool_execution;
    const args = toolExecution.arguments || {};
    
    let parsedOutput = toolExecution.result?.output;
    if (typeof parsedOutput === 'string') {
      try {
        parsedOutput = JSON.parse(parsedOutput);
      } catch (e) {
        // ignore
      }
    }
    parsedOutput = parsedOutput || {};

    const extractedData = {
      query: args.query || parsedOutput?.query || null,
      results: parsedOutput?.results?.map((result: any) => ({
        title: result.title || '',
        url: result.url || '',
        snippet: result.content || result.snippet || ''
      })) || [],
      answer: parsedOutput?.answer || null,
      images: parsedOutput?.images || [],
      success: toolExecution.result?.success,
      timestamp: toolExecution.execution_details?.timestamp
    };
    return extractedData;
  }

  if ('role' in parsedContent && 'content' in parsedContent) {
    console.log('🔍 [Format] role + content format detected, recursing');
    return extractFromNewFormat(parsedContent.content);
  }

  console.log('🔍 [Format] No matching format found, available keys:', Object.keys(parsedContent));
  return { query: null, results: [], answer: null, images: [], success: undefined, timestamp: undefined };
};


const extractFromLegacyFormat = (content: any): Omit<WebSearchData, 'success' | 'timestamp'> => {
  const toolData = extractToolData(content);
  
  if (toolData.toolResult) {
    const args = toolData.arguments || {};
    return {
      query: toolData.query || args.query || null,
      results: [], 
      answer: null,
      images: []
    };
  }

  const legacyQuery = extractSearchQuery(content);
  
  return {
    query: legacyQuery,
    results: [],
    answer: null,
    images: []
  };
};

export function extractWebSearchData(
  assistantContent: any,
  toolContent: any,
  isSuccess: boolean,
  toolTimestamp?: string,
  assistantTimestamp?: string
): {
  query: string | null;
  searchResults: Array<{ title: string; url: string; snippet?: string }>;
  answer: string | null;
  images: string[];
  actualIsSuccess: boolean;
  actualToolTimestamp?: string;
  actualAssistantTimestamp?: string;
} {
  console.log('🔍 [WebSearch] Input data:', {
    assistantContentType: typeof assistantContent,
    toolContentType: typeof toolContent,
    assistantPreview: assistantContent ? JSON.stringify(assistantContent).slice(0, 200) : null,
    toolPreview: toolContent ? JSON.stringify(toolContent).slice(0, 200) : null
  });

  let query: string | null = null;
  let searchResults: Array<{ title: string; url: string; snippet?: string }> = [];
  let answer: string | null = null;
  let images: string[] = [];
  let actualIsSuccess = isSuccess;
  let actualToolTimestamp = toolTimestamp;
  let actualAssistantTimestamp = assistantTimestamp;
  let usedNewFormat = false; // Track if we used new format parsing

  const assistantNewFormat = extractFromNewFormat(assistantContent);
  const toolNewFormat = extractFromNewFormat(toolContent);

  console.log('🔍 [WebSearch] Extracted formats:', {
    assistantFormat: { query: assistantNewFormat.query, resultsCount: assistantNewFormat.results.length },
    toolFormat: { query: toolNewFormat.query, resultsCount: toolNewFormat.results.length }
  });

  // Prioritize formats that have both query AND results
  if (assistantNewFormat.results.length > 0 && assistantNewFormat.query) {
    query = assistantNewFormat.query;
    searchResults = assistantNewFormat.results;
    answer = assistantNewFormat.answer;
    images = assistantNewFormat.images;
    usedNewFormat = true;
    if (assistantNewFormat.success !== undefined) {
      actualIsSuccess = assistantNewFormat.success;
    }
    if (assistantNewFormat.timestamp) {
      actualAssistantTimestamp = assistantNewFormat.timestamp;
    }
    console.log('🔍 [WebSearch] Using assistant format data');
  } else if (toolNewFormat.results.length > 0 && toolNewFormat.query) {
    query = toolNewFormat.query;
    searchResults = toolNewFormat.results;
    answer = toolNewFormat.answer;
    images = toolNewFormat.images;
    usedNewFormat = true;
    if (toolNewFormat.success !== undefined) {
      actualIsSuccess = toolNewFormat.success;
    }
    if (toolNewFormat.timestamp) {
      actualToolTimestamp = toolNewFormat.timestamp;
    }
    console.log('🔍 [WebSearch] Using tool format data');
  } else if (assistantNewFormat.query) {
    // Fallback to query-only from assistant
    query = assistantNewFormat.query;
    answer = assistantNewFormat.answer;
    images = assistantNewFormat.images;
    console.log('🔍 [WebSearch] Using assistant format data (query only)');
  } else if (toolNewFormat.query) {
    // Fallback to query-only from tool
    query = toolNewFormat.query;
    answer = toolNewFormat.answer;
    images = toolNewFormat.images;
    console.log('🔍 [WebSearch] Using tool format data (query only)');
  } else {
    const assistantLegacy = extractFromLegacyFormat(assistantContent);
    const toolLegacy = extractFromLegacyFormat(toolContent);

    query = assistantLegacy.query || toolLegacy.query;
    
    const legacyResults = extractSearchResults(toolContent);
    searchResults = legacyResults;
    
    if (toolContent) {
      try {
        let parsedContent;
        if (typeof toolContent === 'string') {
          parsedContent = JSON.parse(toolContent);
        } else if (typeof toolContent === 'object' && toolContent !== null) {
          parsedContent = toolContent;
        } else {
          parsedContent = {};
        }

        if (parsedContent.answer && typeof parsedContent.answer === 'string') {
          answer = parsedContent.answer;
        }
        if (parsedContent.images && Array.isArray(parsedContent.images)) {
          images = parsedContent.images;
        }
      } catch (e) {
        // ignore
      }
    }
    console.log('🔍 [WebSearch] Using legacy format data');
  }

  // Only run fallback logic if we didn't use new format parsing
  if (!usedNewFormat) {
  if (!query) {
    query = extractSearchQuery(assistantContent) || extractSearchQuery(toolContent);
      console.log('🔍 [WebSearch] Fallback query extraction:', query);
  }
  
  if (searchResults.length === 0) {
    const fallbackResults = extractSearchResults(toolContent);
      console.log('🔍 [WebSearch] Fallback results extraction:', {
        count: fallbackResults.length,
        firstResult: fallbackResults[0]
      });
    searchResults = fallbackResults;
    }
  } else {
    console.log('🔍 [WebSearch] Skipping fallback logic (new format was successful)');
  }

  console.log('🔍 [WebSearch] Final result:', {
    query,
    resultsCount: searchResults.length,
    hasAnswer: !!answer,
    imagesCount: images.length,
    firstResult: searchResults[0],
    usedNewFormat
  });

  return {
    query,
    searchResults,
    answer,
    images,
    actualIsSuccess,
    actualToolTimestamp,
    actualAssistantTimestamp
  };
} 