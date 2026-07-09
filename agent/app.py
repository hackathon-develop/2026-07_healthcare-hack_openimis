import streamlit as st
import asyncio
from google import genai
from google.genai import types
from dotenv import load_dotenv
from mcp_client import MultiMcpManager

load_dotenv()

st.set_page_config(page_title="Clinical Compliance Assistant", layout="wide")
st.title("🏥 Clinical & Compliance AI Assistant")

# System instruction matching the defined operational rules
SYSTEM_INSTRUCTION = """
You are a specialized clinical compliance assistant. Your job is to guide the user through a specific data verification workflow using the tools available to you. You must strictly follow these sequential steps. Do not skip steps or make assumptions.

STEP 1: IDENTIFICATION
- When the user starts the chat, greet them professionally and ask for their Patient ID. 
- Do not proceed to Step 2 until the user provides an ID.

STEP 2: EMR RETRIEVAL
- Once the user provides an ID, use the EMR tool to query their records.
- Analyze the retrieved data specifically looking for "recent treatment records" (defined as records dated within the last 12 months from today).

STEP 3: COMPLIANCE EVALUATION & ISMS RETRIEVAL
- Count the recent treatment records.
- IF you find 3 or more recent records: The EMR data is sufficient. Move directly to Step 4.
- IF you find FEWER than 3 recent records (0, 1, or 2): The patient file lacks sufficient recent history. You must immediately call the openISMS tool to query the organizational policy regarding "incomplete patient history," "missing recent records," or "alternative data gathering protocols."

STEP 4: REPORT GENERATION
- Compile all findings into a structured, easy-to-read Markdown report. 
- The report MUST include the following sections:
  ## Patient Summary
  [Brief summary of the patient based on EMR data]
  
  ## Record Status
  [State exactly how many recent treatment records were found in the last 12 months]
  
  ## Compliance & Next Steps
  [If >=3 records: State that the file meets the completeness criteria. If <3 records: Detail the policy/guidelines retrieved from openISMS regarding how to handle the missing records.]

Always maintain a professional, clinical tone. Do not invent medical data or ISMS policies—rely strictly on the data returned by your tools.
"""

# Initialize persistent session states
if "mcp_manager" not in st.session_state:
    manager = MultiMcpManager()
    # Run async connection loop within sync streamlit script
    asyncio.run(manager.connect_servers())
    st.session_state.mcp_manager = manager

if "gemini_client" not in st.session_state:
    st.session_state.gemini_client = genai.Client()

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

if "gemini_chat_session" not in st.session_state:
    # Gather dynamic tools from active MCP endpoints
    mcp_tools = asyncio.run(st.session_state.mcp_manager.get_all_tools())
    
    # Initialize the Gemini Chat session
    st.session_state.gemini_chat_session = st.session_state.gemini_client.chats.create(
        model="gemini-2.5-flash",
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=mcp_tools if mcp_tools else None,
            temperature=0.1
        )
    )

# Display historical messages in the chat UI
for message in st.session_state.chat_history:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Main interface handling loop
if user_input := st.chat_input("Enter your message here..."):
    # Render user message
    with st.chat_message("user"):
        st.markdown(user_input)
    st.session_state.chat_history.append({"role": "user", "content": user_input})

    # Render agent stream response box
    with st.chat_message("assistant"):
        response_placeholder = st.empty()
        full_response = ""
        
        # Dispatch message to Gemini chat session
        response = st.session_state.gemini_chat_session.send_message(user_input)
        
        # Tool execution loop (Function Calling Orchestration)
        while response.function_calls:
            for function_call in response.function_calls:
                tool_name = function_call.name
                tool_args = function_call.args
                
                with st.spinner(f"Querying system tool: {tool_name}..."):
                    # Execute tool call synchronously from the async manager
                    tool_output = asyncio.run(
                        st.session_state.mcp_manager.call_tool(tool_name, tool_args)
                    )
                
                # Send the structural execution output data back to Gemini
                response = st.session_state.gemini_chat_session.send_message(
                    types.Part.from_function_response(
                        name=tool_name,
                        response={"result": tool_output}
                    )
                )
        
        # Capture final textual response text from the model loop
        full_response = response.text
        response_placeholder.markdown(full_response)
        
    st.session_state.chat_history.append({"role": "assistant", "content": full_response})