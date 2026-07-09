import asyncio
import os
from typing import Dict, Any, List
from mcp import ClientSession
from mcp.client.sse import sse_client

class MultiMcpManager:
    def __init__(self):
        self.emr_url = os.getenv("EMR_MCP_URL")
        self.isms_url = os.getenv("OPENISMS_MCP_URL")
        self.sessions: Dict[str, ClientSession] = {}
        self._exit_stack = None

    async def connect_servers(self):
        """Establishes SSE connections to both MCP servers."""
        # Connect to EMR MCP
        try:
            emr_ctx = sse_client(url=self.emr_url)
            self.emr_read, self.emr_write = await emr_ctx.__aenter__()
            emr_session = ClientSession(self.emr_read, self.emr_write)
            await emr_session.__aenter__()
            await emr_session.initialize()
            self.sessions["emr"] = emr_session
        except Exception as e:
            print(f"Failed to connect to EMR MCP at {self.emr_url}: {e}")

        # Connect to openISMS MCP
        try:
            isms_ctx = sse_client(url=self.isms_url)
            self.isms_read, self.isms_write = await isms_ctx.__aenter__()
            isms_session = ClientSession(self.isms_read, self.isms_write)
            await isms_session.__aenter__()
            await isms_session.initialize()
            self.sessions["isms"] = isms_session
        except Exception as e:
            print(f"Failed to connect to openISMS MCP at {self.isms_url}: {e}")

    async def get_all_tools(self) -> List[Dict[str, Any]]:
        """Collects tools from both servers and formats them for Gemini."""
        gemini_tools = []
        
        for name, session in self.sessions.items():
            tools_result = await session.list_tools()
            for tool in tools_result.tools:
                # Map MCP tool schema to Gemini function declaration schema
                gemini_tools.append({
                    "function_declaration": {
                        "name": f"{name}__{tool.name}",  # Namespace to avoid collisions
                        "description": tool.description,
                        "parameters": tool.inputSchema
                    }
                })
        return gemini_tools

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Routes the execution call to the appropriate MCP server."""
        if "__" not in tool_name:
            return "Error: Unknown tool format."
            
        server_prefix, actual_tool_name = tool_name.split("__", 1)
        session = self.sessions.get(server_prefix)
        
        if not session:
            return f"Error: MCP Server '{server_prefix}' is unavailable."
            
        try:
            result = await session.call_tool(actual_tool_name, arguments)
            # Extracted content usually arrives as a list of text objects
            text_contents = [content.text for content in result.content if hasattr(content, 'text')]
            return "\n".join(text_contents)
        except Exception as e:
            return f"Error executing tool {actual_tool_name}: {str(e)}"

    async def disconnect(self):
        """Gracefully close all connection streams."""
        for session in self.sessions.values():
            await session.__aexit__(None, None, None)