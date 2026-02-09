import os
import json
import re
import asyncio
import threading
import streamlit as st
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from pypdf import PdfReader
import docx
import google.generativeai as genai
from typing import Any, List, Dict, Optional
import intigration  # Import the integration module
import requests  # For GitHub API calls

# ==================================================
# Setup
# ==================================================
load_dotenv()
st.set_page_config(page_title="PRD → Jira User Stories", page_icon="🧠", layout="wide")

st.markdown("""
<style>
.header { font-size:2.4rem; font-weight:700; }
.story-box { border:1px solid #ddd; padding:1rem; border-radius:0.6rem; margin-bottom:1rem; }
</style>
""", unsafe_allow_html=True)

# ==================================================
# Async Event Loop for Streamlit
# ==================================================
def get_loop():
    if "loop" not in st.session_state:
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        st.session_state.loop = loop
    return st.session_state.loop

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, get_loop()).result()

def _is_error_resp(resp: Any) -> bool:
    return isinstance(resp, dict) and resp.get("isError", False)

# ==================================================
# Jira MCP Client
# ==================================================
class JiraClient:
    def __init__(self, server="jira_mcp_server.py"):
        self.server = server
        self.session = None
        self._exit_event = None
        self._session_ready = None

    async def _run_session(self):
        """Maintains the session context in a background task."""
        try:
            params = StdioServerParameters(command="python", args=[self.server])
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    self.session = session
                    await session.initialize()
                    if self._session_ready:
                        self._session_ready.set()
                    
                    # Keep session alive until exit signal
                    if self._exit_event:
                        await self._exit_event.wait()
        except Exception as e:
            print(f"Session background task failed: {e}")
        finally:
            self.session = None

    async def connect(self):
        # If already connected, skip
        if self.session:
            return True
            
        self._exit_event = asyncio.Event()
        self._session_ready = asyncio.Event()
        
        # Start the background task on the current loop (which is the persistent one)
        asyncio.create_task(self._run_session())
        
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=15)
            return True
        except asyncio.TimeoutError:
            print("Connection timed out")
            return False
        except Exception as e:
            print(f"Connection failed: {e}")
            return False

    async def call(self, tool, args):
        if not self.session:
            success = await self.connect()
            if not success:
                return {"isError": True, "error": "Failed to connect to Jira server"}
        
        try:
            res = await self.session.call_tool(tool, arguments=args)
        except Exception as e:
            print(f"Session call failed: {e}. Reconnecting...")
            self.session = None # Force reset
            if await self.connect():
                 res = await self.session.call_tool(tool, arguments=args)
            else:
                 raise e

        # Handle various response formats
        if hasattr(res, "content") and res.content:
            try:
                return json.loads(res.content[0].text)
            except:
                return res.content[0].text
        
        # New handling for empty content (implies successful void return or empty list?)
        if hasattr(res, "content") and not res.content:
             print(f"DEBUG: Tool {tool} returned empty content. Res: {res}")
             return {} # Return empty dict as safe fallback

        return res

async def connect_jira():
    jc = JiraClient()
    await jc.connect()
    return jc

# ==================================================
# GitHub REST API Client
# ==================================================
class GitHubClient:
    """Simple GitHub REST API client for fetching commits"""
    
    def __init__(self, token=None):
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {self.token}" if self.token else ""
        }
    
    async def call(self, tool: str, args: dict):
        """MCP-like call interface for compatibility"""
        if tool == "get_commit_history":
            commits = await self.get_commits(
                owner=args.get("owner"),
                repo=args.get("repo"),
                since=args.get("since"),
                branch=args.get("sha") or args.get("branch") # Accept sha or branch
            )
            return {"commits": commits}
        elif tool == "get_authenticated_user":
            return await self.get_authenticated_user()
        elif tool == "list_repositories":
            return await self.list_repositories()
        elif tool == "list_branches":
            return await self.list_branches(
                owner=args.get("owner"),
                repo=args.get("repo")
            )
        return {"error": f"Unknown tool: {tool}"}

    async def get_authenticated_user(self):
        """Get the current authenticated GitHub user"""
        url = "https://api.github.com/user"
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            return {"username": data.get("login"), "name": data.get("name")}
        except Exception as e:
            return {"error": str(e)}

    async def list_repositories(self):
        """List repositories for the authenticated user"""
        url = "https://api.github.com/user/repos"
        params = {"sort": "updated", "per_page": 100}
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=10)
            response.raise_for_status()
            repos = response.json()
            return {"repositories": [{"name": r["name"], "full_name": r["full_name"], "updated_at": r["updated_at"]} for r in repos]}
        except Exception as e:
            return {"error": str(e)}

    async def list_branches(self, owner: str, repo: str):
        """List branches for a repository"""
        if not owner or not repo:
            return {"branches": []}
            
        url = f"https://api.github.com/repos/{owner}/{repo}/branches"
        params = {"per_page": 100}
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=10)
            response.raise_for_status()
            branches = response.json()
            return {"branches": [b["name"] for b in branches]}
        except Exception as e:
            return {"error": str(e)}

    async def get_commits(self, owner: str, repo: str, since: str = None, branch: str = None):
        """Fetch commits from a GitHub repository"""
        if not owner or not repo:
            return []
            
        url = f"https://api.github.com/repos/{owner}/{repo}/commits"
        params = {"per_page": 100}  # Limit to 100 most recent commits
        if since:
            params["since"] = since
        if branch:
            params["sha"] = branch # GitHub API uses 'sha' for branch/tag/commit SHA
        
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"GitHub API Error: {e}")
            return []

# ==================================================
# PRD Text Extraction
# ==================================================
def extract_input_data(file):
    name = file.name.lower()
    if name.endswith(".pdf"):
        reader = PdfReader(file)
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    elif name.endswith(".docx"):
        d = docx.Document(file)
        return "\n".join(p.text for p in d.paragraphs)
    elif name.endswith((".png", ".jpg", ".jpeg")):
        return {"type": "image", "mime_type": f"image/{name.split('.')[-1]}", "data": file.read()}
    elif name.endswith((".srt", ".vtt")):
        content = file.read().decode("utf-8")
        # Remove timestamps/indices for cleaner prompt
        content = re.sub(r'\d+\n\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}', '', content)
        content = re.sub(r'(\d{2}:)?\d{2}:\d{2}.\d{3} --> (\d{2}:)?\d{2}:\d{2}.\d{3}', '', content)
        content = re.sub(r'\n\s*\n', '\n', content)
        return f"[Video Transcript]\n{content.strip()}"
    return file.read().decode("utf-8")

# ==================================================
# Gemini AI User Story Generation
# ==================================================
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-2.5-flash")

def generate_user_stories(input_data: Any, jira_priorities: list[str]):
    allowed_priorities = ", ".join(jira_priorities)

    instructions = f"""You are an expert Agile Product Owner and Business Analyst.

Analyze the provided input (Requirement Document, Wireframe Image, or Video Transcript) and generate ALL necessary user stories to fully implement the requirements.

CRITICAL REQUIREMENTS:
1. Priority MUST be one of: [{allowed_priorities}]
2. Each story must follow the format: "As a [role], I want [goal] so that [benefit]"
3. Acceptance criteria must be specific, testable, and complete
4. Break down complex features into multiple manageable stories
5. Output ONLY valid JSON - no markdown, no backticks, no explanations

JSON Schema:
[
  {{
    "title": "Concise, action-oriented title (max 80 chars)",
    "description": "As a <role>, I want <goal> so that <benefit>",
    "priority": "MUST be from [{allowed_priorities}]",
    "acceptance_criteria": [
      "Given [context], when [action], then [outcome]",
      "Specific, testable condition"
    ],
    "labels": ["feature-name", "category"],
    "components": [],
    "assignee_account_id": ""
  }}
]
"""

    prompt_parts = [instructions]
    
    if isinstance(input_data, dict) and input_data.get("type") == "image":
        prompt_parts.append({
            "mime_type": input_data["mime_type"],
            "data": input_data["data"]
        })
        prompt_parts.append("Analyze this wireframe/image and extract all functional requirements to create user stories.")
    else:
        prompt_parts.append(f"Requirement Content:\n{input_data}")

    resp = model.generate_content(prompt_parts)
    raw = (resp.text or "").strip()

    if not raw:
        raise ValueError("Gemini returned empty response")

    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in Gemini response:\n{raw}")

    try:
        stories = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON from Gemini:\n{match.group(0)}") from e

    # 🔐 FINAL SAFETY NET
    for s in stories:
        if s.get("priority") not in jira_priorities:
            s["priority"] = jira_priorities[0]

    return stories

def analyze_prd_completeness(input_data: Any):
    instructions = """You are a Helpful PRD Assistant. Analyze the input for the following fields.
    
    Checklist (with Synonym support):
    1. Problem Statement
    2. Business Objective (Synonyms: Obj, Objective, Goal, Business Goals)
    3. Target Personas (Synonyms: Users, Personas, User Roles, Actors, Target Audience)
    4. User Scenarios (Synonyms: Functional Requirements, Use Cases, User Flows, Journeys)
    5. Business Value
    6. Success Metrics
    7. KPIs
    8. Features
    9. Scope In
    10. Scope Out
    11. Assumptions
    12. Dependencies
    13. Risks
    14. Acceptance Criteria (High level)
    15. Non-Functional Requirements

    INSTRUCTIONS:
    - Mark a field as "present" if the exact term OR any reasonable synonym is found.
    - Specifically checking:
      - "Users", "Roles", "Actors" -> Satisfies "Target Personas"
      - "Obj", "Goal", "Aim" -> Satisfies "Business Objective"
      - "Functional Requirements" -> Satisfies "User Scenarios"
    - If a field is strictly MISSING, include it in 'missing_fields' with a question.
    - This is a loose check. If the content implies the field, mark it complete.
    
    Output JSON ONLY:
    {
      "is_complete": boolean,
      "missing_fields": [
        {
          "field": "Field Name",
          "question": "Specific question to ask?"
        }
      ]
    }
    """
    
    prompt_parts = [instructions]
    if isinstance(input_data, dict) and input_data.get("type") == "image":
        prompt_parts.append({
            "mime_type": input_data["mime_type"],
            "data": input_data["data"]
        })
        prompt_parts.append("Audit this wireframe/image.")
    else:
        prompt_parts.append(f"PRD Content:\n{input_data}")

    resp = model.generate_content(prompt_parts)
    raw = (resp.text or "").strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match: return {"is_complete": False, "missing_fields": []}
    return json.loads(match.group(0))

def generate_epic_proposal(input_data: Any):
    instructions = """You are an expert Agile Product Owner.
    Analyze the provided PRD/Requirement content and generate a suitable Epic Name and Description.
    
    The Epic Name should be concise (max 50 chars) but descriptive (e.g. "User Authentication Module").
    The Epic Description should be detailed and follow this specific format:
    
    **Goal:**
    [One sentence key goal]
    
    **Scope:**
    [Bullet points of what is in scope]
    
    **Key Features:**
    [Bullet points of main features]
    
    Output JSON ONLY:
    {
        "title": "Epic Name",
        "description": "Epic Description (formatted as requested)"
    }
    """
    
    prompt_parts = [instructions]
    if isinstance(input_data, dict) and input_data.get("type") == "image":
        prompt_parts.append({
            "mime_type": input_data["mime_type"],
            "data": input_data["data"]
        })
        prompt_parts.append("Analyze this wireframe/image for the main module/feature name.")
    else:
        prompt_parts.append(f"PRD Content:\n{input_data}")

    resp = model.generate_content(prompt_parts)
    raw = (resp.text or "").strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match: return {"title": "", "description": ""}
    return json.loads(match.group(0))

# ==================================================
# Create Jira Story with Bullet Point Formatting
# ==================================================
async def create_story(jira, project_key, story, jira_priorities, valid_components, valid_users):
    summary = story.get("title") or story.get("summary") or "Untitled Story"
    description_text = story.get("description") or ""
    acceptance = story.get("acceptance_criteria") or []
    
    # Build ADF (Atlassian Document Format) with bullet points
    adf_content = []
    
    # Add description as paragraph
    if description_text:
        adf_content.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": description_text}]
        })
    
    # Add acceptance criteria as bullet list
    if acceptance:
        adf_content.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": "Acceptance Criteria:", "marks": [{"type": "strong"}]}]
        })
        
        bullet_items = []
        for criterion in acceptance:
            bullet_items.append({
                "type": "listItem",
                "content": [{
                    "type": "paragraph",
                    "content": [{"type": "text", "text": str(criterion)}]
                }]
            })
        
        adf_content.append({
            "type": "bulletList",
            "content": bullet_items
        })
    
    # Create final ADF document
    adf_description = {
        "version": 1,
        "type": "doc",
        "content": adf_content
    }

    fields = {
        "project_key": project_key,
        "summary": summary,
        "description": "",  # Empty string - we'll override with ADF in fields_extra
        "issue_type": story.get("issue_type", "Story"),
        "fields_extra": {
            "description": adf_description  # Pass ADF directly in fields_extra
        }
    }

    # Handle Parent (Epic) linking
    if story.get("parent_key"):
        fields["fields_extra"]["parent"] = {"key": story["parent_key"]}

    # Handle Issue Type ID override
    if story.get("issue_type_is_id"):
        fields["fields_extra"]["issuetype"] = {"id": story.get("issue_type")}

    # Dynamic Priority
    priority = story.get("priority", "Medium")
    if priority not in jira_priorities:
        priority = jira_priorities[0] if jira_priorities else "Medium"
    fields["fields_extra"]["priority"] = {"name": priority}

    # Labels
    if story.get("labels"):
        fields["fields_extra"]["labels"] = [str(l).strip() for l in story["labels"]]

    # Components
    if story.get("components"):
        fields["fields_extra"]["components"] = [{"name": c} for c in story["components"] if c in valid_components]

    # Assignee
    assignee = story.get("assignee_account_id")
    if assignee and assignee in [u["accountId"] for u in valid_users]:
        fields["fields_extra"]["assignee"] = {"id": assignee}

    try:
        result = await jira.call("create_issue", fields)

        if isinstance(result, dict) and result.get("key"):
            return {"success": True, "key": result["key"], "summary": summary}
        return {"success": False, "summary": summary, "error": str(result)}
    except Exception as e:
        import traceback
        print("Failed payload:", fields)
        print("Error repr:", repr(e))
        traceback.print_exc()
        return {"success": False, "summary": summary, "error": f"{e} (See console for traceback)"}

# ==================================================
# Initialize Session State
# ==================================================
def init_session():
    # Added "github" and "boards" and "epics" and "sprints" to the list of keys
    for k in ["jira", "github", "stories", "selected", "jira_priorities", "components", "users", "projects", "issue_types", "boards", "epics", "sprints"]:
        if k not in st.session_state:
            st.session_state[k] = None if k in ["jira", "github"] else []
    
    if "epics_processed" not in st.session_state:
        st.session_state.epics_processed = False
    
    if "epics_fetched" not in st.session_state:
        st.session_state.epics_fetched = False
        
    if "prd_verified" not in st.session_state:
        st.session_state.prd_verified = False
        
    if "prd_analysis" not in st.session_state:
        st.session_state.prd_analysis = None
        
    if "requirement_text_extra" not in st.session_state:
        st.session_state.requirement_text_extra = ""
        
    if "current_prd_content" not in st.session_state:
        st.session_state.current_prd_content = None
        
    if "auto_epic_generated" not in st.session_state:
        st.session_state.auto_epic_generated = False

# ==================================================
# Callback to reset verification state
# ==================================================
def reset_verification():
    st.session_state.prd_verified = False
    st.session_state.prd_analysis = None
    st.session_state.manual_verification_success = False
    st.session_state.stories = []
    st.session_state.selected = []

# ==================================================
# Streamlit UI
# ==================================================
def main():
    init_session()
    
    # 1️⃣ Connect Jira (Global)
    if not st.session_state.jira:
        st.markdown("<div class='header'>🧠 Jira & GitHub Assistant</div>", unsafe_allow_html=True)
        with st.spinner("Connecting to Jira..."):
            st.session_state.jira = run_async(connect_jira())
            # Load priorities
            try:
                p_payload = run_async(st.session_state.jira.call("get_priorities", {}))
                st.session_state.jira_priorities = [p["name"] for p in p_payload.get("priorities", [])]
            except:
                st.session_state.jira_priorities = ["Highest","High","Medium","Low","Lowest"]
            # Load all users (first 50)
            try:
                u_payload = run_async(st.session_state.jira.call("get_users", {"query":"","max_results":50}))
                st.session_state.users = u_payload.get("users", [])
            except:
                st.session_state.users = []
            
            # Story points removed - no longer needed
            
            # Load Projects (Try search_projects first for better results/pagination)
            try:
                # Try the new search_projects tool which is more robust
                projs_payload = run_async(st.session_state.jira.call("search_projects", {"max_results": 100}))
                
                if isinstance(projs_payload, dict):
                    if projs_payload.get("isError"):
                        st.session_state.projects = []
                    elif "values" in projs_payload:
                        projs = projs_payload["values"]
                    elif "projects" in projs_payload:
                        projs = projs_payload["projects"]
                    else:
                        projs = []
                else:
                    projs = []
                
                if projs:
                    # Store as list of dicts with name and key
                    st.session_state.projects = [{"name": p.get("name"), "key": p.get("key")} for p in projs if isinstance(p, dict) and "key" in p]
                    if not st.session_state.projects:
                         st.warning("⚠️ No projects found. Please check your permissions.")
                
                elif isinstance(projs, dict) and "key" in projs:
                    # Single project returned
                    st.session_state.projects = [{"name": projs.get("name"), "key": projs["key"]}]
                
                else:
                    st.session_state.projects = []
            except Exception as e:
                print(f"Project loading error: {e}")
                st.session_state.projects = []
            
            # Initialize GitHub client
            try:
                github_token = os.getenv("GITHUB_TOKEN")
                if github_token and github_token != "your-github-personal-access-token":
                    st.session_state.github = GitHubClient(github_token)
                    st.success("✅ GitHub client initialized")
                else:
                    st.session_state.github = None
                    st.info("ℹ️ GitHub token not configured. Story tracking will be limited.")
            except Exception as e:
                print(f"GitHub init error: {e}")
                st.session_state.github = None
        
        if st.button("🔌 Re-connect to Jira (Reset Session)"):
            st.session_state.jira = None
            st.session_state.issue_types = []
            st.session_state.issue_type_map = {}
            st.rerun()

        st.success("Connected to Jira")
        
        with st.expander("❓ Help: I can't find my Project Key"):
            st.info("Your Project List failed to load automatically. Use this debug tool to see what Jira sees.")
            if st.button("🕵️‍♂️ Debug Connection & List Projects"):
                st.write("### 1. Who am I?")
                try:
                    myself = run_async(st.session_state.jira.call("get_myself", {})) # We need to add get_myself to server or use generic call
                    # Wait, we might not have get_myself tool. Let's try to list projects directly and show RAW.
                    st.warning("Skipping 'myself' check (tool might not exist).")
                except:
                    st.write("Could not check identity.")

                st.write("### 2. Raw Project List Response")
                try:
                    raw_projs = run_async(st.session_state.jira.call("list_projects", {}))
                    st.json(raw_projs)
                except Exception as e:
                    st.error(f"Failed to call list_projects: {e}")
            
            st.markdown("""
            **How to find your Key manually:**
            1. Go to your Jira Board in your browser.
            2. Look at the URL: `https://.../projects/MYKEY/boards/...` or `.../browse/MYKEY-123`.
            3. **MYKEY** is correct Project Key. It might be different from the Project Name.
            """)

    # Tabs for valid workflow
    tab1, tab2 = st.tabs(["📝 Create Stories", "🔍 Track Stories"])

    with tab1:
        st.markdown("<div class='header'>🧠 PRD → Jira User Stories</div>", unsafe_allow_html=True)
        

        
        # User Guide
        with st.expander("📖 How to Use This Tool"):
            st.markdown("""
            ### Quick Start
            1. **Choose Input Method**: Upload a PRD/Requirement file PDF/Word/Text OR enter text manually
            2. **Generate Stories**: AI will analyze Input Method and create user stories
            3. **Select Stories**: Review and choose which stories to create
            4. **Push to Jira**: Stories will be created in Jira
            
            ### Best Practices For PRD/Requirement Document Writing
            - **Be Specific**: Clearly define features, user roles, and goals
            - **Include Context**: Explain why features are needed
            - **Define Success**: Describe what "done" looks like
            - **List Constraints**: Mention technical limitations or dependencies
            
            ### Example Story Format
            The AI will generate stories like:
            > **Title**: User Login with Email
            > 
            > **Description**: As a user, I want to log in with my email so that I can access my account securely
            > 
            > **Acceptance Criteria**:
            > - Given I enter valid credentials, when I click login, then I am redirected to dashboard
            > - Given I enter invalid credentials, when I click login, then I see an error message""")
            
            
        
        # 2️⃣ Input Method Selection
        st.header("1️⃣ Provide Requirements")
        
        input_method = st.radio(
            "Choose input method:",
            ["📄 Upload PRD File", "✍️ Enter Text Manually"],
            horizontal=True,
            on_change=reset_verification
        )
        
        requirement_data = None
        
        if input_method == "📄 Upload PRD File":
            file = st.file_uploader("Upload Requirement (PDF / DOCX / TXT / Image / Transcript)", type=["pdf","docx","txt","png","jpg","jpeg","srt","vtt"], on_change=reset_verification)
            if file:
                requirement_data = extract_input_data(file)
                if isinstance(requirement_data, dict) and requirement_data.get("type") == "image":
                    st.image(requirement_data["data"], caption="Uploaded Wireframe/Image")
                    st.success(f"✅ Loaded image: {file.name}")
                else:
                    st.success(f"✅ Loaded {len(requirement_data)} characters from {file.name}")
        
        else:  # Manual text input
            st.subheader("Use a Template or Write Custom Requirements")
            
            # Predefined templates
            templates = {
                "E-commerce Feature": """Build an e-commerce product catalog feature.

Requirements:
- Users should be able to browse products by category
- Each product should display: name, image, price, description, stock status
- Users should be able to search products by name or category
- Products should be filterable by price range
- Shopping cart functionality to add/remove items
- Checkout process with order summary

User Roles:
- Customer: Browse and purchase products
- Admin: Manage product catalog

Success Criteria:
- Users can find and purchase products easily
- Admin can update inventory in real-time

Assumptions:
- Product images are optimized for web.
- Standard shipping rates apply.

Dependencies:
- Payment Gateway Provider (e.g., Stripe).
- Inventory Service API.

Acceptance Criteria (High level):
- Guest users can search and browse the catalog without login.
- Valid payments result in a confirmed order and inventory deduction.
- Admin receives low-stock alerts.""",
                
                "User Authentication": """Implement secure user authentication system.

Requirements:
- User registration with email and password
- Email verification for new accounts
- Login with email/password
- Password reset functionality
- Session management
- Logout functionality
- Remember me option

Security Requirements:
- Passwords must be hashed
- Email verification required before first login
- Password must meet complexity requirements (8+ chars, uppercase, number, special char)
- Account lockout after 5 failed attempts

User Roles:
- New User: Register and verify account
- Existing User: Login and manage session

Assumptions:
- Users have access to the provided email address.
- Application runs over HTTPS.

Dependencies:
- SMTP Server/Service for sending emails.
- Relational Database for user data.

Acceptance Criteria (High level):
- Registration fails if email already exists.
- Login fails for unverified accounts.
- Password reset link expires after 24 hours.""",
                
                "Data Management": """Create a data management dashboard.

Requirements:
- Display data in tabular format with sorting and filtering
- Export data to CSV/Excel
- Import data from CSV files
- Bulk edit capabilities
- Search functionality across all fields
- Pagination for large datasets
- Data validation on import

User Roles:
- Data Entry Specialist: Add and edit records
- Manager: View reports and export data
- Admin: Manage data structure and permissions

Success Criteria:
- Users can efficiently manage large datasets
- Data integrity is maintained during import/export

Assumptions:
- CSV files follow the strictly defined template.
- Maximum file size for import is 50MB.

Dependencies:
- Cloud Storage for backups/imports.
- Backend API processing queue.

Acceptance Criteria (High level):
- Import rejects files with invalid headers.
- Bulk delete requires an explicit confirmation step.
- Export generation does not block the UI.""",
                
                "API Integration": """Integrate with third-party API service.

Requirements:
- Connect to external API with authentication
- Fetch data from API endpoints
- Transform API response to internal format
- Handle API rate limiting
- Error handling and retry logic
- Cache API responses for performance
- Display API data in UI

Technical Constraints:
- API has rate limit of 100 requests/minute
- Authentication uses OAuth 2.0
- Responses are in JSON format

User Roles:
- System: Automated data sync
- Admin: Configure API credentials and monitor sync status

Assumptions:
- Third-party API uptime is > 99.9%.
- Valid API credentials are provided.

Dependencies:
- Secure connection to external provider.
- Scheduled Job system (cron).

Acceptance Criteria (High level):
- System manages token refreshment automatically.
- Rate limit errors (429) trigger exponential backoff.
- Sync failures are logged and trigger an alert.""",
                
                "Custom": ""
            }
            
            template_choice = st.selectbox(
                "Select a template:",
                list(templates.keys())
            )
            
            default_text = templates[template_choice]
            
            requirement_data = st.text_area(
                "Requirements (edit the template or write your own):",
                value=default_text,
                height=300,
                help="Describe your requirements in detail. The AI will generate user stories from this text.",
                on_change=reset_verification
            )
        
        # Generate button
        # Combine checks
        full_requirements = requirement_data
        if isinstance(requirement_data, str):
            full_requirements = requirement_data + "\n" + st.session_state.requirement_text_extra
        elif isinstance(requirement_data, dict):
             # For images, we can't easily append text to the image data, but we pass the extra text as a separate context if needed, 
             # currently generate_stories handles dict OR str. We might need to handle mixed.
             # For simplicity, if image, we just pass image. (Implementation detail: We could add text context to image prompt)
             pass 
        
        # Save to session state for Epic generation
        st.session_state.current_prd_content = full_requirements 

        # Generate button logic with Verification
        if requirement_data:
            # Step 1: Analyze Button (Always Visible)
            if not st.session_state.prd_verified:
                 if st.button("🔍 Analyze Readiness"):
                    with st.spinner("Checking PRD coverage..."):
                        st.session_state.prd_analysis = analyze_prd_completeness(full_requirements)
            
                 if st.session_state.prd_analysis:
                     res = st.session_state.prd_analysis
                     missing = res.get("missing_fields", [])
                     
                     if not missing:
                         st.success("✅ PRD looks comprehensive!")
                         # Auto-proceed if complete
                         st.session_state.prd_verified = True
                         st.rerun()
                     else:
                         st.warning(f"⚠️ Missing {len(missing)} fields: {', '.join([m['field'] for m in missing])}")
                         st.caption("You can provide missing details below, or proceed if these fields are not needed.")
                         
                         user_context = st.text_area("✍️ Add missing details / instructions:", height=300, key="missing_fields_input")
                         
                         if st.button("✅ Proceed"):
                             if user_context.strip():
                                 st.session_state.requirement_text_extra += "\n\n[User Added Context]\n" + user_context
                                 st.session_state.manual_verification_success = True
                             else:
                                 st.session_state.manual_verification_success = False
                                 
                             st.session_state.prd_verified = True
                             st.rerun()
            
            # Step 2: Generate Button (Only Visible if Verified)
            if st.session_state.get("manual_verification_success"):
                 st.success("✅ Successfully added missing fields and verified!")
            if st.session_state.prd_verified:
                with st.expander("📄 View/Edit Final PRD Content (Verified)"):
                    if isinstance(full_requirements, str):
                        # Allow user to edit the final content
                        edited_prd = st.text_area("Edit Content (Fix spelling, add details)", 
                                                  value=full_requirements, 
                                                  height=300,
                                                  key="final_prd_edit")
                        
                        # Update the session state if changed
                        if edited_prd != full_requirements:
                             # We can't easily update 'requirement_data' directly if it was partial, 
                             # but we can update 'current_prd_content' which is what is used for generation.
                             # However, to make it persist across reruns without complex logic, 
                             # we update the variable 'full_requirements' locally and rely on the user 
                             # hitting 'Generate' which uses 'full_requirements'.
                             # To persist explicitly, we might need a separate state, but for now:
                             full_requirements = edited_prd
                             st.session_state.current_prd_content = edited_prd
                    else:
                        st.info("Content is binary/image based. Editing text is not supported.")

                
                # ==================================================
                # 2️⃣ Configure Project & Epic (Moved Up)
                # ==================================================
                st.divider()
                st.subheader("2️⃣ Configure Project & Epic")
                
                # Use dropdown for project selection
                if st.session_state.projects:
                    # Create display names: "Project Name (KEY)"
                    project_display_map = {f"{p['name']} ({p['key']})": p['key'] for p in st.session_state.projects}
                    proj_options = list(project_display_map.keys())
                    project_selection = st.selectbox("Select Jira Project (Space)", proj_options, key="active_project_selection")
                    project = project_display_map[project_selection]
                else:
                    project = st.text_input("Jira Project Key", value="KAN", key="manual_project_key").upper()
                    st.info("ℹ️ Couldn't load project list automatically. Please enter your project key manually.")
                
                # AUTOMATIC METADATA FETCHING (Issue Types & Epics)
                # We fetch if project changed OR if either list is empty
                needs_types = "last_fetched_types" not in st.session_state or st.session_state.last_fetched_types != project or not st.session_state.issue_types
                needs_epics = "last_fetched_epics" not in st.session_state or st.session_state.last_fetched_epics != project or (not st.session_state.epics and not st.session_state.get('epics_processed', False))

                if needs_types or needs_epics:
                    with st.spinner(f"Updating metadata for {project}..."):
                        try:
                            if needs_types:
                                meta_payload = run_async(st.session_state.jira.call("get_issue_createmeta", {"project_key": project}))
                                found_types = []
                                if isinstance(meta_payload, dict):
                                    if "issueTypes" in meta_payload: found_types = meta_payload["issueTypes"]
                                    elif "name" in meta_payload and "id" in meta_payload: found_types = [meta_payload]
                                elif isinstance(meta_payload, list): found_types = meta_payload
                                
                                if not found_types:
                                    details = run_async(st.session_state.jira.call("get_project_details", {"project_key": project}))
                                    if isinstance(details, dict) and "issueTypes" in details: found_types = details["issueTypes"]
                                
                                if found_types:
                                    st.session_state.issue_types = []
                                    st.session_state.issue_type_map = {}
                                    for m in found_types:
                                        if isinstance(m, dict) and "name" in m:
                                            display_name = m["name"]
                                            if display_name == "Task": display_name = "User Story"
                                            st.session_state.issue_types.append(display_name)
                                            st.session_state.issue_type_map[display_name] = m["id"]
                                    st.session_state.last_fetched_types = project
                            
                            # Reset boards if project changed
                            if "last_fetched_boards_project" in st.session_state and st.session_state.last_fetched_boards_project != project:
                                 st.session_state.boards = []
                                 st.session_state.last_fetched_boards_project = project

                            if needs_epics:
                                with st.spinner("🔍 Fetching all project Epics (KAN-107 to KAN-116)..."):
                                    # Dictionary to store and deduplicate epics by key
                                    all_epics = {e["key"]: e["summary"] for e in (st.session_state.epics or [])}
                                    
                                    # Strategy 1: Dedicated Epic Tool
                                    tool_res = run_async(st.session_state.jira.call("list_epics", {"project_key": project}))
                                    if isinstance(tool_res, dict) and "issues" in tool_res:
                                        for i in tool_res["issues"]:
                                            ikey = i.get("key")
                                            if ikey:
                                                all_epics[ikey] = i.get("fields", {}).get("summary", "No Summary")
                                    
                                    # Strategy 2: ID-based search if found in metadata
                                    epic_ids = []
                                    for it_name, it_id in st.session_state.issue_type_map.items():
                                        if it_name.lower().strip() in ["epic", "standard epic", "feature", "initiative"]:
                                            epic_ids.append(it_id)
                                    
                                    if epic_ids:
                                        it_jql = f'project = "{project}" AND issuetype in ({",".join(epic_ids)})'
                                        # Explicitly ask for fields to ensure we get data
                                        it_res = run_async(st.session_state.jira.call("search_issues", {"jql": it_jql, "max_results": 200, "fields": ["summary", "status", "issuetype"]}))
                                        if isinstance(it_res, dict) and "issues" in it_res:
                                            for i in it_res["issues"]:
                                                ikey = i.get("key")
                                                if ikey:
                                                    all_epics[ikey] = i.get("fields", {}).get("summary", "No Summary from ID Search")
                                    
                                    # Strategy 3: Hierarchy level for Jira Cloud
                                    try:
                                        h_jql = f'project = "{project}" AND hierarchyLevel = 1'
                                        # Explicitly ask for fields here too
                                        h_res = run_async(st.session_state.jira.call("search_issues", {"jql": h_jql, "max_results": 200, "fields": ["summary", "status", "issuetype"]}))
                                        if isinstance(h_res, dict) and "issues" in h_res:
                                            for i in h_res["issues"]:
                                                ikey = i.get("key")
                                                if ikey:
                                                    all_epics[ikey] = i.get("fields", {}).get("summary", "No Summary from Hierarchy")
                                    except: pass

                                    # Convert back to list and sort by numerical key descending
                                    def key_num(k):
                                        try: return int(k.split('-')[1])
                                        except: return 0

                                    sorted_list = [{"key": k, "summary": v} for k, v in all_epics.items()]
                                    sorted_list.sort(key=lambda x: key_num(x["key"]), reverse=True)

                                    st.session_state.epics = sorted_list
                                    st.session_state.epics_processed = True
                                    st.session_state.last_fetched_epics = project
                                    
                                    
                                    if st.session_state.epics:
                                        st.success(f"✅ Loaded {len(st.session_state.epics)} Epics from {project}")
                                    else:
                                        st.warning(f"⚠️ No Epics found for {project}.")
                                        # Detailed Debugging for User
                                        with st.expander("Show Deep Debug Info (Why is it empty?)"):
                                            st.write("Strategy 1 (List Tool):", tool_res)
                                            st.write(f"Strategy 2 (Issue Type IDs: {epic_ids}):", locals().get("it_res", "Not run"))
                                            st.write("Strategy 3 (Hierarchy):", locals().get("h_res", "Not run"))
                                            if st.button("Retry Deep Fetch"):
                                                st.session_state.last_fetched_epics = None
                                                st.session_state.epics_processed = False
                                                st.rerun()
                                
                        except Exception as e:
                            print(f"Metadata auto-fetch failed for {project}: {e}")

                # Issue Type Selection
                issue_type_id = None
                issue_type_name = "Story"
                if st.session_state.issue_types:
                    default_index = 0
                    if "User Story" in st.session_state.issue_types: default_index = st.session_state.issue_types.index("User Story")
                    elif "Story" in st.session_state.issue_types: default_index = st.session_state.issue_types.index("Story")
                    
                    issue_type_name = st.selectbox("Issue Type", st.session_state.issue_types, index=default_index)
                    issue_type_id = st.session_state.issue_type_map.get(issue_type_name)

                # --- Epic Selection (Direct Style) ---
                st.divider()
                st.subheader("Organize by Epic")
                
                epic_choice = st.radio("Choose Epic Option:", 
                                    ["None", "1. Select Existing Epic", "2. Create New Epic"],
                                    horizontal=True,
                                    key="epic_choice_radio")
                
                selected_epic_key = None
                new_epic_name = ""
                new_epic_desc = ""

                if epic_choice == "1. Select Existing Epic":
                    if st.session_state.epics:
                        # Numerical sort (KAN-116, KAN-114...)
                        def key_num(k):
                            try: return int(k.split('-')[1])
                            except: return 0
                        
                        sorted_epics = sorted(st.session_state.epics, key=lambda x: key_num(x['key']), reverse=True)
                        epic_display_map = {f"{e['key']}: {e['summary']}": e['key'] for e in sorted_epics}
                        
                        epic_selection = st.selectbox("Select Epic", list(epic_display_map.keys()))
                        selected_epic_key = epic_display_map.get(epic_selection)
                    else:
                        st.warning("⚠️ No existing epics found for this project.")
                        if st.button("🔄 Force Re-fetch Epics"):
                            st.session_state.last_fetched_epics = None
                            st.session_state.epics_processed = False
                            st.rerun()
                
                elif epic_choice == "2. Create New Epic":
                    # Auto-generate if not done yet and we have content
                    if not st.session_state.auto_epic_generated and st.session_state.current_prd_content:
                        with st.spinner("✨ Generating Epic details from PRD..."):
                            proposal = generate_epic_proposal(st.session_state.current_prd_content)
                            if proposal.get("title"):
                                st.session_state.new_epic_name_input = proposal["title"]
                                st.session_state.new_epic_description_input = proposal.get("description", "")
                        st.session_state.auto_epic_generated = True

                    col_e1, col_e2 = st.columns([0.8, 0.2])
                    with col_e2:
                        if st.button("🔄 Regenerate"):
                            st.session_state.auto_epic_generated = False
                            st.rerun()

                    new_epic_name = st.text_input("Epic Name", 
                                                placeholder="e.g. Login Page Module",
                                                key="new_epic_name_input")
                    
                    new_epic_desc = st.text_area("Epic Description",
                                                placeholder="High level goal...",
                                                key="new_epic_description_input",
                                                height=68)

                    st.info("💡 A new Epic with this name and description will be created.")

                # Board Selection
                selected_board_id = None
                selected_sprint_id = None

                with st.expander("🗺️ Advanced: Target Board / Backlog / Sprints"):
                    if st.button("🔍 Find Boards for this Project"):
                        try:
                            boards_resp = run_async(st.session_state.jira.call("list_boards", {"projectKeyOrId": project}))
                            if isinstance(boards_resp, dict) and "values" in boards_resp:
                                project_boards = [b for b in boards_resp["values"] if b.get("location", {}).get("projectKey") == project]
                                if not project_boards: project_boards = boards_resp["values"]
                                if not project_boards: project_boards = boards_resp["values"]
                                st.session_state.boards = [{"name": b["name"], "id": b["id"]} for b in project_boards]
                                st.session_state.last_fetched_boards_project = project
                                st.success(f"Found {len(st.session_state.boards)} board(s)")
                            else: st.error(f"Failed to load boards: {boards_resp}")
                        except Exception as e: st.error(f"Error: {e}")

                    if st.session_state.boards:
                        board_options = {b["name"]: b["id"] for b in st.session_state.boards}
                        board_name = st.selectbox("Select Board:", ["None"] + list(board_options.keys()))
                        
                        if board_name != "None":
                            selected_board_id = board_options[board_name]
                            
                            # --- FEATURE: Future Sprint Selection ---
                            # Fetch sprints if board selected
                            if "current_board_sprints" not in st.session_state or st.session_state.get("last_sprint_board_id") != selected_board_id:
                                try:
                                    s_res = run_async(st.session_state.jira.call("list_sprints", {"board_id": selected_board_id, "state": "future"}))
                                    if isinstance(s_res, dict) and "values" in s_res:
                                        st.session_state.current_board_sprints = s_res["values"]
                                        st.session_state.last_sprint_board_id = selected_board_id
                                    else:
                                        st.session_state.current_board_sprints = []
                                except Exception as e:
                                    print(f"Sprint fetch failed: {e}")
                                    st.session_state.current_board_sprints = []
                            
                            if st.session_state.current_board_sprints:
                                sprint_map = {f"{s['name']} (ID: {s['id']})": s['id'] for s in st.session_state.current_board_sprints}
                                selected_sprint_name = st.selectbox("Select Future Sprint (Optional):", ["None"] + list(sprint_map.keys()))
                                if selected_sprint_name != "None":
                                    selected_sprint_id = sprint_map[selected_sprint_name]
                                    st.info(f"Stories will be added to sprint: **{selected_sprint_name}**")
                            else:
                                st.caption("No future sprints found for this board.")
                            # ----------------------------------------
                        
                if st.button("🧠 Generate User Stories", type="primary"):
                    with st.spinner("Analyzing requirements and generating user stories..."):
                        # Reset epic generation flag
                        st.session_state.auto_epic_generated = False
                        
                        # Use full requirements including appended answers
                        final_input = full_requirements
                        st.session_state.stories = generate_user_stories(
                            final_input,
                            st.session_state.jira_priorities
                        )
                    st.success(f"✅ Generated {len(st.session_state.stories)} user stories")

        # 3️⃣ Select stories (Updated Position)
        if st.session_state.stories:
            st.header("3️⃣ Generated User Stories")
            selected = []
            for idx, s in enumerate(st.session_state.stories):
                story_id = f"story_{idx}"
                with st.container():
                    col1, col2 = st.columns([0.05,0.95])
                    with col1:
                        checked = st.checkbox("Select", key=story_id, label_visibility="collapsed")
                    with col2:
                        st.markdown(f"""
    <div class='story-box'>
    <b>{s.get('title','Untitled')}</b><br><br>
    {s.get('description','')}<br><br>
    <b>Acceptance Criteria</b>
    <ul>
    {''.join(f"<li>{a}</li>" for a in s.get('acceptance_criteria',[]))}
    </ul>
    <b>Priority:</b> {s.get('priority','Medium')}
    </div>
    """, unsafe_allow_html=True)
                    if checked:
                        selected.append(s)
                        st.session_state.selected = selected




        # 3️⃣ Select stories

        # 4️⃣ Push to Jira (Only button is conditional now)
        if st.session_state.selected:
            st.divider()
            st.header("4️⃣ Finalize & Create")


            if st.button("🚀 Create Selected Stories in Jira"):
                if not st.session_state.projects and not st.session_state.issue_types:
                     st.error("❌ Please select a project and ensure issue types are loaded.")
                     st.stop()
                
                if not st.session_state.issue_types and not issue_type_id:
                     st.error("❌ No Issue Types loaded. Cannot create stories without a valid Issue Type ID.")
                     st.info("Ensure the Project Key is correct and you have permissions.")
                     st.stop()

                # Re-collect selected stories directly from widget states to ensure accuracy
                final_selected = []
                for idx, s in enumerate(st.session_state.stories):
                    if st.session_state.get(f"story_{idx}", False):
                        # Update issue type in the story object
                        if issue_type_id:
                            s["issue_type"] = issue_type_id # Use ID if available
                            s["issue_type_is_id"] = True
                        else:
                            s["issue_type"] = issue_type_name
                            s["issue_type_is_id"] = False
                            
                        final_selected.append(s)
                
                if not final_selected:
                    st.warning("No stories selected. Please select at least one story.")
                else:
                    # Handle Epic creation if needed
                    parent_epic_key = selected_epic_key
                    if new_epic_name: # User chose "➕ Create New Epic..."
                        
                        with st.spinner(f"Creating new Epic '{new_epic_name}'..."):
                            epic_res = run_async(st.session_state.jira.call("create_epic", {
                                "project_key": project,
                                "summary": new_epic_name,
                                "description": new_epic_desc
                            }))
                            if isinstance(epic_res, dict) and epic_res.get("key"):
                                parent_epic_key = epic_res["key"]
                                st.success(f"✅ Created Epic: {parent_epic_key}")
                                # Add to session state so it appears in dropdown immediately
                                if "epics" not in st.session_state or st.session_state.epics is None:
                                    st.session_state.epics = []
                                st.session_state.epics.append({"key": parent_epic_key, "summary": new_epic_name})
                            else:
                                st.error(f"Failed to create Epic: {epic_res}")
                                st.stop()

                    # Apply Epic key to all stories
                    if parent_epic_key:
                        for s in final_selected:
                            s["parent_key"] = parent_epic_key
                    # fetch project components dynamically
                    try:
                        comps = run_async(st.session_state.jira.call("list_components", {"project_key": project}))
                        st.session_state.components = [c["name"] for c in comps]
                    except:
                        st.session_state.components = []
                    
                    results = []
                    progress_bar = st.progress(0)
                    status_text = st.empty()
                    
                    with st.spinner(f"Creating '{issue_type_name}' issues in {project}..."):
                        total = len(final_selected)
                        for i, s in enumerate(final_selected):
                            status_text.text(f"Creating {issue_type_name} {i+1}/{total}: {s.get('title', 'Untitled')}")
                            res = run_async(create_story(
                                st.session_state.jira,
                                project,
                                s,
                                st.session_state.jira_priorities,
                                st.session_state.components,
                                st.session_state.users
                            ))
                            results.append(res)
                            progress_bar.progress((i + 1) / total)
                    
                    status_text.empty()
                    progress_bar.empty()

                    success_count = 0
                    for r in results:
                        if r["success"]:
                            # Create a direct link to the issue
                            issue_url = f"{os.getenv('JIRA_BASE')}/browse/{r['key']}"
                            st.success(f'✅ **[{r["key"]}]({issue_url})**: "{r["summary"]}" created.')
                            success_count += 1
                        else:
                            st.error(f'❌ "{r["summary"]}" failed: {r.get("error")}')
                    
                    if success_count == len(results):
                        # Move to Backlog OR Sprint
                        new_keys = [r["key"] for r in results if r["success"]]
                        
                        if selected_sprint_id and new_keys:
                             with st.spinner(f"Adding {len(new_keys)} stories to Sprint {selected_sprint_id}..."):
                                 sprint_res = run_async(st.session_state.jira.call("add_to_sprint", {"sprint_id": selected_sprint_id, "issues": new_keys}))
                                 if isinstance(sprint_res, dict) and _is_error_resp(sprint_res):
                                     st.error(f"❌ Failed to add to sprint: {sprint_res.get('error')}")
                                 else:
                                     st.success(f"🚀 Success! Stories added to Future Sprint.")
                        
                        elif selected_board_id and new_keys:
                            with st.spinner(f"Moving {len(new_keys)} issues to backlog..."):
                                move_res = run_async(st.session_state.jira.call("move_to_backlog", {"issues": new_keys}))
                                if isinstance(move_res, dict) and _is_error_resp(move_res):
                                    st.warning(f"⚠️ Issues created but could not move to backlog: {move_res.get('error')}")
                                else:
                                    # Verification step
                                    # Verification step
                                    st.info(f"🔍 Verifying issues in Board {selected_board_id} backlog...")
                                    
                                    # Use JQL to filter exactly for our new keys to avoid pagination limits
                                    jql_check = f"key in ({','.join(new_keys)})"
                                    
                                    backlog_check = run_async(st.session_state.jira.call("list_board_backlog", {"board_id": selected_board_id, "jql": jql_check}))
                                    
                                    if isinstance(backlog_check, dict) and "issues" in backlog_check:
                                        backlog_keys = [b["key"] for b in backlog_check["issues"]]
                                        all_found = all(k in backlog_keys for k in new_keys)
                                        if all_found:
                                            st.success("🚀 Success! Issues created and confirmed in Board Backlog.")
                                        else:
                                            # Secondary check: They might be on the Active Board
                                            active_check = run_async(st.session_state.jira.call("list_board_issues", {"board_id": selected_board_id, "jql": jql_check}))
                                            if isinstance(active_check, dict) and "issues" in active_check:
                                                active_keys = [b["key"] for b in active_check["issues"]]
                                                all_active_found = all(k in active_keys for k in new_keys)
                                                if all_active_found:
                                                    st.success("🚀 Success! Issues created and confirmed on Active Board/Backlog.")
                                                else:
                                                    found_keys = [k for k in new_keys if k in active_keys or k in backlog_keys]
                                                    st.warning(f"⚠️ Move complete, but only {len(found_keys)}/{len(new_keys)} issues visible. They are in Jira but filtered from your Board view.")
                                                    st.info("💡 **Why is this happening?** Issues are successfully created, but the Board you selected might not be configured to show them. Click the links above to see them directly in Jira.")
                                            
                                                    col1, col2 = st.columns(2)
                                                    with col1:
                                                        if st.button("🔄 Retry Move to Backlog"):
                                                            with st.spinner("Retrying move to backlog..."):
                                                                run_async(st.session_state.jira.call("move_to_backlog", {"issues": new_keys}))
                                                                st.rerun()
                                                    with col2:
                                                        if st.button("🆘 Rescue: Force Move to Active Board"):
                                                            with st.spinner("Moving issues to board visibility..."):
                                                                # This uses the sprint/board assignment
                                                                rescue_res = run_async(st.session_state.jira.call("move_to_board", {"board_id": selected_board_id, "issues": new_keys}))
                                                                if isinstance(rescue_res, dict) and _is_error_resp(rescue_res):
                                                                    st.error(f"Failed to move: {rescue_res.get('error')}")
                                                                else:
                                                                    st.success("✅ Issues moved to Board! Check your Active Board (Sprint/Kanban).")
                                                                    st.rerun()
                                    else:
                                        st.success("🚀 Move command sent successfully (Verification skipped/failed).")
                        
                        st.balloons()
    
    with tab2:
        # Call the integration UI
        intigration.run_integration_ui()

if __name__ == "__main__":
    main()
