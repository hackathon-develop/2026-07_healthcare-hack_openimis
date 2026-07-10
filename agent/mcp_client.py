import asyncio
import threading
import os
import base64
from typing import Dict, Any, List
from mcp import ClientSession
from mcp.client.sse import sse_client

class MultiMcpManager:
    def __init__(self):
        self.emr_url = os.getenv("EMR_MCP_URL", "https://aql-mcp-server.sandbox.vghip.cloud/mcp")
        self.isms_url = os.getenv("OPENISMS_MCP_URL")
        self.sessions: Dict[str, ClientSession] = {}
        
        self.connection_status = {
            "emr": {"connected": False, "error": None},
            "isms": {"connected": False, "error": None}
        }
        
        self._contexts = []
        
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def connect(self):
        """Synchronous wrapper to connect all servers in the background thread."""
        future = asyncio.run_coroutine_threadsafe(self.connect_servers(), self.loop)
        future.result()

    def disconnect(self):
        """Synchronous wrapper to disconnect all servers."""
        future = asyncio.run_coroutine_threadsafe(self.disconnect_servers(), self.loop)
        future.result()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()

    async def connect_servers(self):
        """Establishes SSE connections to both MCP servers."""
        # Connect to EMR MCP
        try:
            headers = {}
            auth_env = os.getenv("EMR_MCP_AUTH")
            if auth_env:
                auth_env = auth_env.strip()
                if ":" in auth_env:
                    username, password = auth_env.split(":", 1)
                    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")
                    headers["Authorization"] = f"Basic {encoded}"
                else:
                    if not auth_env.lower().startswith("basic "):
                        headers["Authorization"] = f"Basic {auth_env}"
                    else:
                        headers["Authorization"] = auth_env

            emr_ctx = sse_client(url=self.emr_url, headers=headers if headers else None)
            self._contexts.append(emr_ctx)
            emr_read, emr_write = await emr_ctx.__aenter__()
            emr_session = ClientSession(emr_read, emr_write)
            self._contexts.append(emr_session)
            await emr_session.__aenter__()
            await emr_session.initialize()
            self.sessions["emr"] = emr_session
            self.connection_status["emr"] = {"connected": True, "error": None}
        except Exception as e:
            print(f"Failed to connect to EMR MCP at {self.emr_url}: {e}")
            self.connection_status["emr"] = {"connected": False, "error": str(e)}

        # Connect to openISMS MCP
        try:
            isms_ctx = sse_client(url=self.isms_url)
            self._contexts.append(isms_ctx)
            isms_read, isms_write = await isms_ctx.__aenter__()
            isms_session = ClientSession(isms_read, isms_write)
            self._contexts.append(isms_session)
            await isms_session.__aenter__()
            await isms_session.initialize()
            self.sessions["isms"] = isms_session
            self.connection_status["isms"] = {"connected": True, "error": None}
        except Exception as e:
            print(f"Failed to connect to openISMS MCP at {self.isms_url}: {e}")
            self.connection_status["isms"] = {"connected": False, "error": str(e)}

    async def disconnect_servers(self):
        """Gracefully close all connection streams."""
        for ctx in reversed(self._contexts):
            try:
                await ctx.__aexit__(None, None, None)
            except Exception as e:
                print(f"Error exiting context manager: {e}")
        self._contexts.clear()
        self.sessions.clear()

    def get_all_tools(self) -> List[Dict[str, Any]]:
        """Collects tools from both servers and formats them for Gemini."""
        future = asyncio.run_coroutine_threadsafe(self._get_all_tools_async(), self.loop)
        return future.result()

    async def _get_all_tools_async(self) -> List[Dict[str, Any]]:
        gemini_tools = []
        for name, session in self.sessions.items():
            try:
                tools_result = await session.list_tools()
                for tool in tools_result.tools:
                    gemini_tools.append({
                        "function_declaration": {
                            "name": f"{name}__{tool.name}",
                            "description": tool.description,
                            "parameters": tool.inputSchema
                        }
                    })
            except Exception as e:
                print(f"Failed to list tools for {name}: {e}")
        return gemini_tools

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Routes the execution call to the appropriate MCP server."""
        future = asyncio.run_coroutine_threadsafe(self._call_tool_async(tool_name, arguments), self.loop)
        return future.result()

    async def _call_tool_async(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        if "__" not in tool_name:
            return "Error: Unknown tool format."
            
        server_prefix, actual_tool_name = tool_name.split("__", 1)
        session = self.sessions.get(server_prefix)
        
        if not session:
            return f"Error: MCP Server '{server_prefix}' is unavailable."
            
        try:
            result = await session.call_tool(actual_tool_name, arguments)
            text_contents = [content.text for content in result.content if hasattr(content, 'text')]
            return "\n".join(text_contents)
        except Exception as e:
            return f"Error executing tool {actual_tool_name}: {str(e)}"