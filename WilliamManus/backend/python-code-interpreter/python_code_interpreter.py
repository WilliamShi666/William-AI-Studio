from __future__ import annotations

import asyncio
import re
import os
from typing import AsyncIterable, List, Dict, Any, Optional

from openai import AsyncOpenAI
from ppio_sandbox.code_interpreter import Sandbox

# Thinking tags for processing model thinking/reasoning
THINKING_BEGIN_TAG = "<thinking>"
THINKING_END_TAG = "</thinking>"

PPIO_API_KEY = os.getenv("PPIO_API_KEY")
if not PPIO_API_KEY:
    raise ValueError("environment variable PPIO_API_KEY is not set")
LLM_BASE_URL = "https://api.ppinfra.com/openai"
LLM_MODEL = "deepseek/deepseek-v3.2-exp"


class PythonCodeInterpreter:
    def __init__(self):
        self.openai_client = AsyncOpenAI(
            api_key=PPIO_API_KEY,
            base_url=LLM_BASE_URL,
        )
        self.temperature = 0.6
        self.max_tokens = 4096

    def run_code_in_sandbox(self, code: str) -> str:
        """Execute code in PPIO Agent Sandbox and return the output"""
        sandbox = None
        try:
            sandbox = Sandbox.create(timeout=5 * 60)
            result = sandbox.run_code(code)
            if result.error:
                return result.error.name + ":" + result.error.value + "\n" + "Traceback:\n" + result.error.traceback
            output = ""
            if len(result.results) > 0:
                output += "results >\n" + "\n".join(result.results)
            if len(result.logs.stdout) > 0:
                if output != "":
                    output += "\n"
                output += "stdout >\n" + "\n".join(result.logs.stdout)
            if len(result.logs.stderr) > 0:
                if output != "":
                    output += "\n"
                output += "stderr >\n" + "\n".join(result.logs.stderr)
            return output
        except Exception as e:
            return str(e)
        finally:
            if sandbox is not None:
                try:
                    sandbox.kill()
                except Exception as cleanup_error:
                    print(f"Failed to cleanup sandbox: {cleanup_error}")

    class PartialResponse:
        def __init__(self, content: Optional[str] = None, reasoning_content: Optional[str] = None):
            self.content = content
            self.reasoning_content = reasoning_content

    async def stream_chat_completion(
        self, 
        messages: List[Dict[str, Any]]
    ) -> AsyncIterable[str]:
        """Stream chat completion from OpenAI API"""
        response = await self.openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=self.temperature,
            stream=True,
            max_tokens=self.max_tokens,
            extra_body={"enable_thinking": True},
        )

        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta:
                choice_delta = chunk.choices[0].delta
                if choice_delta.content:
                    yield self.PartialResponse(content=choice_delta.content)
                elif hasattr(choice_delta, 'reasoning_content') and choice_delta.reasoning_content:
                    yield self.PartialResponse(reasoning_content=choice_delta.reasoning_content)


    async def generate_summary(
        self, 
        messages: List[Dict[str, Any]]
    ) -> AsyncIterable[str]:
        """Stream the summary of code execution result"""
        reasoning = False
        async for resp in self.stream_chat_completion(messages):
            if resp.reasoning_content:
                if not reasoning:
                    yield "Thinking...\n"
                    reasoning = True
                yield resp.reasoning_content
            elif resp.content:
                if reasoning:
                    yield "\n\n"
                    reasoning = False
                yield resp.content


    async def generate_python_code(
        self, 
        messages: List[Dict[str, Any]], 
    ) -> AsyncIterable[tuple[str, str]]:
        """
        Generate code stream and return display response and collected code snippet
        
        Args:
            messages: messages to generate code
            
        Returns:
            tuple[str, str]: (display text, collected code snippet)
        """
        reasoning = False
        async for resp in self.stream_chat_completion(messages):
            if resp.content:
                if reasoning:
                    yield "\n\n", ""
                    reasoning = False
                yield resp.content, resp.content
            elif resp.reasoning_content:
                if not reasoning:
                    yield "Thinking...\n", ""
                    reasoning = True
                yield resp.reasoning_content, ""


    def remove_markdown_code_fences(self, code_snippet: str) -> str:
        """Remove markdown code fences from code snippet"""
        return re.sub(r"```(python)?+", "", code_snippet).strip()


    def has_error(self, result: str) -> bool:
        """Check if execution result contains error"""
        error_keywords = ["Traceback (most recent call last):", "Error:"]
        return any(keyword in result for keyword in error_keywords)


    async def run(
        self, 
        user_message: str
    ) -> Dict[str, Any]:
        """
        Execute code generation and execution workflow:
        1. Call LLM to generate code based on the user's request.
        2. Pass the returned code to the Python sandbox.
        3. If there's an error, call LLM again with the error message for debugging.
        4. Re-run the updated code on the Python sandbox.
        5. Return the final result (or last error if debugging failed).
        
        Returns:
            Dict with keys: 'code', 'output', 'has_error', 'debug_code' (if applicable)
        """
        if not user_message:
            print("Please provide a prompt describing the code you want generated.")
            return {"error": "Empty user message"}

        result = {
            "user_message": user_message,
            "code": "",
            "output": "",
            "has_error": False,
            "debug_code": None,
            "summary": ""
        }

        # -------------
        # 1) Ask LLM to generate code
        # -------------
        gen_code_prompt = (
            "You are a helpful coding assistant. The user wants some Python code. "
            "Please provide only the Python code (MUST WITH markdown fences) needed to "
            "accomplish the following request:\n\n"
            f"{user_message}\n\n"
            "Do not include any comments or other text in the code. Do not offer to "
            "explain the code."
        )

        messages = [{"role": "user", "content": gen_code_prompt}]
        code_snippet = ""

        print("\n⏳ Generating code...\n")

        async for display_text, collected_code in self.generate_python_code(messages):
            print(display_text, end="", flush=True)
            code_snippet += collected_code

        print()  # newline

        # Clean up code snippet by removing triple backticks
        code_snippet = self.remove_markdown_code_fences(code_snippet)
        result["code"] = code_snippet

        # -------------
        # 2) Run the code in the sandbox
        # -------------
        print("\n⏳ Running code in PPIO Agent Sandbox...\n")

        python_result = self.run_code_in_sandbox(code_snippet)
        result["output"] = python_result

        # Check if Python returned an error
        has_error = self.has_error(python_result)
        result["has_error"] = has_error
        status_emoji = "✅" if not has_error else "❌"
        
        print(f"**{status_emoji} Code execution output:**\n```text\n{python_result}\n```\n")

        # -------------
        # 3) If there's an error, call LLM to help debug
        # -------------
        if has_error:
            print("\n**Got an error while executing the code. Trying to debug it...**\n")

            debug_prompt = (
                "The following Python code produced an error. "
                f"Original code:\n{code_snippet}\n\n"
                f"Error:\n{python_result}\n\n"
                "Please provide only the Python code (MUST WITH markdown fences) needed to "
                "fix the error. "
                "Do not include any comments or other text in the code. "
                "Do not offer to explain the code. "
            )
            
            debug_messages = [{"role": "user", "content": debug_prompt}]
            debug_code_snippet = ""
            
            async for display_text, collected_code in self.generate_python_code(debug_messages):
                print(display_text, end="", flush=True)
                debug_code_snippet += collected_code

            print()  # newline
                
            debug_code_snippet = self.remove_markdown_code_fences(debug_code_snippet)
            result["debug_code"] = debug_code_snippet

            print("\n⏳ Running the fixed code in PPIO Agent Sandbox...\n")

            python_debug_result = self.run_code_in_sandbox(debug_code_snippet)
            has_error = self.has_error(python_debug_result)
            result["has_error"] = has_error
            result["output"] = python_debug_result
            status_emoji = "✅" if not has_error else "❌"
            
            print(f"**{status_emoji} Code execution output:**\n```text\n{python_debug_result}\n```\n")

            # If we still have error, just give up and display it
            if has_error:
                print("---\n## Summary\n")
                print(
                    "**It seems we have another error even after debugging:**\n\n"
                    f"```text\n{python_debug_result}\n```\n\n"
                    "You can try refining your request or debugging further."
                )
                result["summary"] = "Failed to fix the error after debugging."
                return result
            else:
                # Summarize the result after successful debug
                print("---\n## Summary\n")
                
                summary_prompt = (
                    "The original user request was:\n"
                    f"{user_message}\n\n"
                    "The code that was generated and run was:\n"
                    f"{code_snippet}\n\n"
                    "But we got an error. So we debugged it and ran the following code:\n"
                    f"{debug_code_snippet}\n\n"
                    "The output of the code was:\n"
                    f"{python_debug_result}\n\n"
                    "Please summarize the output of the code. "
                )
                
                summary_messages = [{"role": "user", "content": summary_prompt}]
                summary = ""
                
                async for text in self.generate_summary(summary_messages):
                    print(text, end="", flush=True)
                    summary += text

                print()  # newline

                result["summary"] = summary
                return result
        else:
            # -------------
            # 4) If there's no error, summarize the result
            # -------------
            print("---\n## Summary\n")
            
            summary_prompt = (
                "The original user request was:\n"
                f"{user_message}\n\n"
                "The code that was generated and run was:\n"
                f"{code_snippet}\n\n"
                "The output of the code was:\n"
                f"{python_result}\n\n"
                "Please summarize the output of the code. "
            )
            
            summary_messages = [{"role": "user", "content": summary_prompt}]
            summary = ""
            
            async for text in self.generate_summary(summary_messages):
                summary += text
                print(text, end="", flush=True)

            print()  # newline
            
            result["summary"] = summary
            return result


async def main():
    """
    Main function to demonstrate the Python Code Interpreter
    """
    # Initialize the interpreter
    # Make sure you have the following environment variable set:
    # - PPIO_API_KEY: Your PPIO API key
    
    interpreter = PythonCodeInterpreter()

    # Example user prompts
    examples = [
        "Calculate the factorial of 10",
        "Implement the Fibonacci sequence in Python, and run an example",
        "Implement the quick-sort algorithm in Python, and run an example",
    ]

    print("=" * 80)
    print("Python Code Interpreter Demo")
    print("=" * 80)
    print("\nThis demo will generate and execute Python code based on natural language prompts.")
    print("\nYou can either:")
    print("1. Try one of the example prompts")
    print("2. Enter your own prompt")
    print("3. Type 'quit' to exit\n")

    while True:
        print("\n" + "-" * 80)
        print("Example prompts:")
        for i, example in enumerate(examples, 1):
            print(f"  {i}. {example}")
        print("\nEnter a number (1-3) to try an example, or type your own prompt:")
        print("(Type 'quit' to exit)")
        
        user_input = input("\n> ").strip()
        
        if user_input.lower() in ['quit', 'exit', 'q']:
            print("\nGoodbye!")
            break
        
        if user_input.isdigit() and 1 <= int(user_input) <= len(examples):
            prompt = examples[int(user_input) - 1]
            print(f"\nUsing example: {prompt}")
        elif user_input:
            prompt = user_input
        else:
            print("Please enter a valid prompt or number.")
            continue

        try:
            # Execute the code generation and execution workflow
            result = await interpreter.run(prompt)
            
            # Print result summary
            print("\n" + "=" * 80)
            print("EXECUTION RESULT SUMMARY:")
            print("=" * 80)
            print(f"User Request: {result['user_message']}")
            print(f"Has Error: {result['has_error']}")
            if result.get('debug_code'):
                print(f"Required Debugging: Yes")
            print("=" * 80)
            
        except KeyboardInterrupt:
            print("\n\nInterrupted by user.")
            break
        except Exception as e:
            print(f"Error during execution: {e}")
            print(f"\n❌ Error: {e}")


if __name__ == "__main__":
    # Run the main function
    asyncio.run(main())
