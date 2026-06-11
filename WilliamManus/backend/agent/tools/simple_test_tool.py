"""
Simple test tool for ADK - 测试现有工具注册流程
"""

from agentpress.tool import Tool, ToolResult
from typing import Dict, Any
import time


class SimpleTestTool(Tool):
    """简单的测试工具类，用于验证现有工具注册流程"""
    
    def __init__(self):
        super().__init__()
    
    async def test_calculator(self, operation: str, a: float, b: float) -> Dict[str, Any]:
        """Perform basic mathematical operations.
        
        Args:
            operation: The operation to perform ('add', 'subtract', 'multiply', 'divide')
            a: The first number
            b: The second number
            
        Returns:
            A dictionary containing the result
        """
        time.sleep(3)
        try:
            if operation == "add":
                result = a + b
            elif operation == "subtract":
                result = a - b
            elif operation == "multiply":
                result = a * b
            elif operation == "divide":
                if b == 0:
                    return {"success": False, "error": "Cannot divide by zero"}
                result = a / b
            else:
                return {"success": False, "error": f"Unknown operation: {operation}"}
            
            return {
                "success": True,
                "result": result,
                "operation": operation,
                "message": f"{a} {operation} {b} = {result}"
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def test_echo(self, message: str) -> Dict[str, str]:
        """Echo back a message with some processing.
        
        Args:
            message: The message to echo back
            
        Returns:
            Dictionary containing the echoed message
        """
      
        return {
            "success": True,
            "result": message,
            "operation": "operation",
            "message": f"{message}"
        } 