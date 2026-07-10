import streamlit as st
from google import genai
from google.genai import types
from dotenv import load_dotenv
from mcp_client import MultiMcpManager

load_dotenv()

st.set_page_config(page_title="Health Help Agent", layout="wide")
st.title("🏥 Health Help Agent")

# System instruction matching the defined operational rules
SYSTEM_INSTRUCTION = """
You are a specialized clinical compliance assistant. Your job is to guide the user through a specific data verification workflow using the tools available to you. You must strictly follow these sequential steps. Do not skip steps or make assumptions.

STEP 1: IDENTIFICATION
- When the user starts the chat, greet them professionally and ask for their Patient ID. 
- Do not proceed to Step 2 until the user provides an ID.

STEP 2: ISMS RETRIEVAL
- Once the user provides an ID, do NOT query the EMR. Go directly to querying the openISMS tool.
- Query the organizational policy regarding "incomplete patient history," "missing recent records," or "alternative data gathering protocols".

STEP 3: REPORT GENERATION
- Compile all findings into a structured, easy-to-read Markdown report. 
- The report MUST include the following sections:
  ## Patient ID
  [The Patient ID provided by the user]
  
  ## Compliance & Next Steps
  [Detail the policy/guidelines retrieved from openISMS regarding how to handle patient records and alternative data gathering protocols.]

Always maintain a professional, clinical tone. Do not invent medical data or ISMS policies—rely strictly on the data returned by your tools.
"""

# ----------------- Debug Logging -----------------
import os
import datetime
show_debug = os.getenv("SHOW_DEBUG_LOG", "true").lower() in ("true", "1", "yes")

if "debug_logs" not in st.session_state:
    st.session_state.debug_logs = []

def log_debug(message):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    st.session_state.debug_logs.append(f"[{timestamp}] {message}")

# ----------------- Initialization -----------------
if "mcp_manager" not in st.session_state:
    log_debug("Initializing MCP Manager...")
    manager = MultiMcpManager()
    manager.connect()
    st.session_state.mcp_manager = manager
    log_debug("MCP Manager connected.")

status = st.session_state.mcp_manager.connection_status
for server, info in status.items():
    if not info["connected"]:
        st.warning(f"⚠️ Could not connect to {server.upper()} MCP server: {info['error']}")

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

try:
    if "gemini_client" not in st.session_state:
        log_debug("Initializing Gemini Client...")
        st.session_state.gemini_client = genai.Client()

    if "gemini_chat_session" not in st.session_state:
        mcp_tools = st.session_state.mcp_manager.get_all_tools()
        log_debug(f"Gathered {len(mcp_tools)} tools from MCP endpoints.")
        
        st.session_state.gemini_chat_session = st.session_state.gemini_client.chats.create(
            model="gemini-2.5-flash",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                tools=mcp_tools if mcp_tools else None,
                temperature=0.1
            )
        )
        log_debug("Gemini Chat Session created.")
except Exception as e:
    err_msg = f"Failed to initialize Gemini Client. Error: {e}"
    st.error(f"❌ {err_msg}")
    log_debug(f"ERROR: {err_msg}")

# ----------------- UI Layout -----------------
if show_debug:
    main_col, debug_col = st.columns([3, 1])
else:
    main_col = st.container()
    debug_col = None

with main_col:
    # Display historical messages in the chat UI
    for message in st.session_state.chat_history:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

# Main interface handling loop MUST be at root level to avoid StreamlitAPIException
if user_input := st.chat_input("Enter your message here..."):
    log_debug(f"User input: {user_input}")
    st.session_state.chat_history.append({"role": "user", "content": user_input})

    with main_col:
        with st.chat_message("user"):
            st.markdown(user_input)

        with st.chat_message("assistant"):
            response_placeholder = st.empty()
            full_response = ""
            
            if "gemini_chat_session" not in st.session_state:
                st.error("Cannot process message: Gemini Client failed to initialize. Please check API Key and server connections.")
                st.stop()
                
            log_debug("Sending message to Gemini...")
            response = st.session_state.gemini_chat_session.send_message(user_input)
            
            # Tool execution loop (Function Calling Orchestration)
            while response.function_calls:
                function_responses = []
                for function_call in response.function_calls:
                    tool_name = function_call.name
                    tool_args = function_call.args
                    
                    log_debug(f"Executing tool: {tool_name} | Args: {tool_args}")
                    with st.spinner(f"Querying system tool: {tool_name}..."):
                        tool_output = st.session_state.mcp_manager.call_tool(tool_name, tool_args)
                    out_str = str(tool_output)
                    log_debug(f"Tool {tool_name} completed. Output length: {len(out_str)} chars. Prefix: {out_str[:80]}...")
                    
                    function_responses.append(
                        types.Part.from_function_response(
                            name=tool_name,
                            response={"result": tool_output}
                        )
                    )
                
                log_debug("Sending tool responses back to Gemini...")
                response = st.session_state.gemini_chat_session.send_message(function_responses)
            
            full_response = response.text
            response_placeholder.markdown(full_response)
            log_debug("Received final text response from Gemini.")
            
        st.session_state.chat_history.append({"role": "assistant", "content": full_response})

if debug_col:
    with debug_col:
        st.subheader("🛠️ Debug Logs")
        try:
            debug_container = st.container(height=600)
            with debug_container:
                for log_msg in st.session_state.debug_logs:
                    st.text(log_msg)
        except TypeError:
            # Fallback for older Streamlit versions that don't support height in st.container
            for log_msg in st.session_state.debug_logs:
                st.text(log_msg)
                
        if st.button("Clear Logs"):
            st.session_state.debug_logs = []
            st.rerun()