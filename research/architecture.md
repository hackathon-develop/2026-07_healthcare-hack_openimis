~~~
there are components to be implemented. thse components need to work with existing systems.

existing system 1
a running instance of openISMS with a postgres database in the backend.
direct access to the the database is possibble. credential can be provided.
there is a mcp server that provides the access to a agent. this openISMS mcp deployed in google cloud and public accessible 
we call this the openISMS-mcp

existing system 2 
an emr system from vitagroup that offers mcp access.
the mcp offers also patient related information.
wee will call this the emr-mcp



the agent to build has a workflow prompt that has to be writte later.
the agent can call the emr-mcp and the openISMS-mcp.
both mcp urls hav to be configurable by env var.
the agent will be deplooyed on google cloud and be reachable publicly for now, the date used is all fake data for now.  
auth will be implemented later.
use google adk kit for the agent and the standard approach for a chatbot interface to it.

as first step propose a plan to implement this agent, ask for missing information and update the plan. present the final plan when it is ready for implementation.
~~~   



This is a great clarification. Using Python with a quick functional UI like Streamlit is the perfect pairing for this kind of agent architecture. The workflow you outlined gives us a very clear state machine for the agent's behavior.

Here is the updated, highly specific implementation plan based on your choices.

---

## 📋 Updated Implementation Plan (Python & Streamlit)

### Phase 1: Core Setup & MCP Connectivity

* **Dependencies:** Initialize a Python environment with `google-genai` (for the Gemini model), `mcp` (the official Python SDK to handle HTTP/SSE connections to the MCP servers), and `streamlit`.
* **Client Initialization:** Write the Python client code to establish Server-Sent Events (SSE) connections to both the `EMR_MCP_URL` and `OPENISMS_MCP_URL` over HTTP.
* **Tool Registration:** Extract the available tools from both MCP servers and bind them to the Gemini agent so it can call them natively.

### Phase 2: Agent Logic & Workflow Prompting

* **State Management:** Set up a conversation loop where the agent manages the chat history, ensuring it follows your defined sequence.
* **The System Instruction:** Draft the exact workflow prompt. This prompt will enforce the rules:
1. Greet and request patient identification.
2. Query the `emr-mcp` using the provided ID.
3. Evaluate the retrieved EMR data against a specific checklist.
4. If the checklist is incomplete, query the `openISMS-mcp` for the missing compliance/policy context.
5. Format all findings into a structured report.



### Phase 3: The Streamlit User Interface

* **Chat Interface:** Build the frontend using Streamlit's native `st.chat_message` and `st.chat_input` components.
* **Report Rendering:** Configure the UI to distinctly render the final compiled report (e.g., using Markdown tables or expanders) so the patient can review it clearly.

### Phase 4: Google Cloud Deployment

* **Containerization:** Write a `Dockerfile` that packages the Python environment, the MCP connection logic, and the Streamlit app.
* **Cloud Run Deployment:** Deploy the Docker container to Google Cloud Run, exposing the standard Streamlit port (8501).
* **Environment Variables:** Securely inject `EMR_MCP_URL`, `OPENISMS_MCP_URL`, and the Gemini API key into the Cloud Run service.

---

To help me write the exact system prompt that will drive this logic, what specific "missing information" in the EMR should trigger the agent to check openISMS, and what do the "further steps" look like once the patient sees the report?

# - 

> assume that enough is if at leats 3 recent(~ 1y) treatment records are found . further steps will be implemented later 


#### - 


This is the exact level of detail needed. We can now construct a highly deterministic **System Instruction** (the workflow prompt) that acts as the "brain" for the agent.

By explicitly encoding your rule ("at least 3 records from the last year") into the prompt, the Gemini model will autonomously execute the logic, knowing exactly when to bridge the gap between the EMR and the openISMS policy engine.

Here is the finalized workflow prompt and the integration strategy.

### The Agent Workflow Prompt (System Instruction)

You will pass this exact text into the `system_instruction` parameter when initializing the Gemini model in your Python backend.

```text
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

```

---

### Implementation Context for Python (`google-genai`)

When building the backend using the new `google-genai` SDK and the official `mcp` client library, you will bind this prompt to the client configuration.

Here is how you will structure the core logic:

1. **Connect MCPs:** Using `mcp.Client` with SSE transports, you will connect to `EMR_MCP_URL` and `OPENISMS_MCP_URL`.
2. **Extract Tools:** You will call `list_tools()` on both servers and map them into the format expected by Gemini.
3. **Initialize Gemini:**

```python
from google import genai
from google.genai import types

client = genai.Client(api_key="YOUR_API_KEY")

# Create a conversation session with the system prompt
chat = client.chats.create(
    model="gemini-2.5-flash", 
    config=types.GenerateContentConfig(
        system_instruction="[INSERT THE PROMPT FROM ABOVE HERE]",
        tools=[emr_tools, isms_tools], # The tools mapped from your MCP clients
        temperature=0.1 # Keep it low for deterministic, factual outputs
    )
)

```

4. **Streamlit Loop:** Inside your Streamlit app, you will capture the user's chat input, send it to `chat.send_message(user_input)`, and display the model's response. The model will autonomously decide to pause and execute tool calls (which you will handle via a tool execution loop) based on the instructions in the prompt.

Are you ready to move on to drafting the code for the Streamlit UI and the SSE connection handling, or do you have any adjustments to the core prompt workflow?



> draft the code 



Here is the complete implementation code for your Python agent. It contains the Streamlit chat interface, the async MCP client connections using HTTP/SSE, and the orchestration loop using the official `google-genai` SDK.

### 📁 Project Structure

```text
├── .env
├── requirements.txt
├── mcp_client.py
└── app.py

```

---

### 1. Dependencies (`requirements.txt`)

```text
google-genai
streamlit
mcp
httpx
python-dotenv
asyncio

```

---

### 2. Environment Configuration (`.env`)

Create a `.env` file in your root directory to store your credentials and configuration URLs:

```env
GEMINI_API_KEY=your_gemini_api_key_here
EMR_MCP_URL=http://localhost:8000/sse
OPENISMS_MCP_URL=http://localhost:8001/sse

```

---

### 3. MCP SSE Client Integration (`mcp_client.py`)

This module manages the asynchronous SSE transport layer to connect to both the EMR and openISMS MCP servers, discover their tools, and execute them.

```python
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

```

---

### 4. Streamlit UI and Agent Orchestration (`app.py`)

This script executes the Streamlit frontend and uses the `google-genai` SDK to run the conversational framework with tool execution loops.

```python
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

```

---

### How to Run Locally

1. Install the environment libraries:
```bash
pip install -r requirements.txt

```


2. Start your Streamlit service application:
```bash
streamlit run app.py

```
# - 

> this chat interface has to run in a google could run function or a gcp vm.
>a gcp project is configured.
> decide the runtime environment and provide exact description of deployment an configuration steps. assume powershell for gcloud commands


For this architecture, **Google Cloud Run** is definitively the better choice over a standard GCP VM.

Here is why: You are building a containerized application (Python + Streamlit). Cloud Run is a managed serverless platform that will automatically provision an HTTPS URL, manage the underlying infrastructure, and—crucially for development—scale to zero when you aren't actively chatting with the bot, saving you money. A VM requires manual OS patching, manual HTTPS configuration via reverse proxies, and runs 24/7.

Here is the exact step-by-step plan to deploy your agent to Cloud Run using PowerShell and the Google Cloud CLI (`gcloud`).

---

### Step 1: Prepare the Dockerfile

Cloud Run needs to know how to build and expose your app. Streamlit must be configured to run on port `8080` (Cloud Run's default) and listen on all interfaces (`0.0.0.0`).

Create a file named `Dockerfile` in the same directory as your `app.py` and `requirements.txt`:

```dockerfile
# Use a lightweight Python base image
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install dependencies first (caches this layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the Cloud Run default port
EXPOSE 8080

# Run Streamlit on port 8080 and disable CORS to avoid routing issues
ENTRYPOINT ["streamlit", "run", "app.py", "--server.port=8080", "--server.address=0.0.0.0", "--server.enableCORS=false"]

```

---

### Step 2: Authenticate and Configure GCP (PowerShell)

Open PowerShell. Make sure you have the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) installed.

Log in and set your active project:

```powershell
# Log into your Google account
gcloud auth login

# Set your project ID (replace YOUR_PROJECT_ID with your actual GCP project ID)
gcloud config set project YOUR_PROJECT_ID

# Enable the required APIs for building and running containers
gcloud services enable cloudbuild.googleapis.com run.googleapis.com artifactregistry.googleapis.com

```

---

### Step 3: Build the Container Image

Instead of installing Docker locally on your Windows machine, you can use Cloud Build to build the container remotely and store it in Google Container Registry (GCR).

Ensure you are in the directory containing your `Dockerfile` and run:

```powershell
# Define a variable for your app name
$APP_NAME = "agent"
$PROJECT_ID = "hh-berlin26ber-500"

# Submit the build to Google Cloud Build
gcloud builds submit --tag gcr.io/$PROJECT_ID/$APP_NAME
```

*(Wait for this process to complete. It will output a SUCCESS message when the image is stored).*

---

### Step 4: Deploy to Cloud Run

Now, deploy the image and inject the required environment variables. We will make it publicly accessible since you mentioned auth will be implemented later.

Execute this single command (adjust the URLs and API key to match your real values):

```powershell
gcloud run deploy $APP_NAME `
  --image gcr.io/$PROJECT_ID/$APP_NAME `
  --region europe-west1 `
  --platform managed `
  --allow-unauthenticated `
  --port 8080 `
  --set-env-vars="GEMINI_API_KEY=your_actual_api_key_here,EMR_MCP_URL=http://your_emr_url/sse,OPENISMS_MCP_URL=http://your_isms_url/sse"

```

*(Note: You can change `europe-west1` to your preferred GCP region).*

### Step 5: Access the Agent

Once the deployment finishes, the PowerShell console will output a **Service URL** (e.g., `https://compliance-agent-xxxxx-ew.a.run.app`).

Click that link, and your publicly accessible Streamlit interface will be live, communicating directly with your Gemini model and remote MCP servers.

Do you have the required external URLs for the MCP endpoints ready, or will those also be hosted on GCP in a subsequent step?