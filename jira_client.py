"""
fully_dynamic_jira_client.py - Universal LLM-Based Jira Assistant

Capabilities:
✅ Handles ANY query type (casual chat, Jira tasks, complex analysis)
✅ LLM decides if Jira tools are needed or just conversation
✅ Natural conversations + powerful Jira operations
✅ Fully dynamic - no hardcoded logic
✅ Memory-based context across sessions

Requirements:
    pip install mcp python-dotenv google-generativeai

Environment:
    GEMINI_API_KEY, JIRA_BASE, JIRA_EMAIL, JIRA_API_TOKEN
"""

import os
import json
import asyncio
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
import google.generativeai as genai

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY not set")
genai.configure(api_key=GEMINI_API_KEY)

JIRA_BASE = os.getenv("JIRA_BASE", "https://your-domain.atlassian.net")


class ConversationMemory:
    """Stores conversation history and context."""
    
    def __init__(self, memory_file: str = "jira_memory.json"):
        self.memory_file = memory_file
        self.conversation_history: List[Dict[str, Any]] = []
        self.entity_cache: Dict[str, List[str]] = {
            "issues": [],
            "boards": [],
            "users": [],
            "projects": []
        }
        self.user_preferences: Dict[str, Any] = {
            "name": None,
            "default_project": None,
            "timezone": None
        }
        self.load_memory()
    
    def load_memory(self):
        try:
            if os.path.exists(self.memory_file):
                with open(self.memory_file, 'r') as f:
                    data = json.load(f)
                    self.conversation_history = data.get("history", [])[-30:]
                    self.entity_cache = data.get("entities", self.entity_cache)
                    self.user_preferences = data.get("preferences", self.user_preferences)
                    if self.conversation_history:
                        print(f"📚 Loaded {len(self.conversation_history)} past conversations")
        except Exception as e:
            print(f"⚠️ Memory load failed: {e}")
    
    def save_memory(self):
        try:
            with open(self.memory_file, 'w') as f:
                json.dump({
                    "history": self.conversation_history[-100:],
                    "entities": self.entity_cache,
                    "preferences": self.user_preferences,
                    "timestamp": datetime.now().isoformat()
                }, f, indent=2)
        except Exception as e:
            print(f"⚠️ Memory save failed: {e}")
    
    def add_interaction(self, user_query: str, assistant_response: str, 
                        interaction_type: str, tool_calls: List[str] = None, 
                        entities: Dict[str, List[str]] = None):
        """Record interaction with type classification."""
        self.conversation_history.append({
            "timestamp": datetime.now().isoformat(),
            "type": interaction_type,  # "chat", "jira_query", "jira_action"
            "user": user_query,
            "assistant": assistant_response[:400],
            "tools": tool_calls or [],
            "entities": entities or {}
        })
        
        # Update entity cache
        if entities:
            for key, values in entities.items():
                if values and key in self.entity_cache:
                    combined = values + self.entity_cache[key]
                    self.entity_cache[key] = list(dict.fromkeys(combined))[:15]
        
        self.save_memory()
    
    def get_context_for_llm(self) -> str:
        """Build rich context for LLM with emphasis on recent queries."""
        if not self.conversation_history:
            return "This is a new conversation."
        
        recent = self.conversation_history[-5:]
        lines = ["=== Recent Conversation (READ CAREFULLY for follow-ups!) ==="]
        
        for i, interaction in enumerate(recent, 1):
            itype = interaction.get('type', 'unknown')
            lines.append(f"\n[{itype.upper()}] Turn {i}:")
            lines.append(f"User: {interaction['user']}")
            lines.append(f"Assistant: {interaction['assistant'][:150]}...")
            if interaction.get('tools'):
                lines.append(f"Actions: {', '.join(interaction['tools'])}")
            if interaction.get('entities'):
                ent = interaction['entities']
                if ent.get('issues'):
                    lines.append(f"Issues mentioned: {', '.join(ent['issues'])}")
        
        lines.append("\n=== Recent Entities Cache ===")
        if self.entity_cache["issues"]:
            lines.append(f"Recent issues: {', '.join(self.entity_cache['issues'][:5])}")
        if self.entity_cache["users"]:
            lines.append(f"Recent users: {', '.join(self.entity_cache['users'][:3])}")
        if self.entity_cache["projects"]:
            lines.append(f"Projects: {', '.join(self.entity_cache['projects'][:3])}")
        
        # Add pending context hints
        if recent:
            last_query = recent[-1]['user'].lower()
            last_response = recent[-1]['assistant'].lower()
            
            if 'need' in last_response or 'provide' in last_response or 'tell me' in last_response:
                lines.append("\n⚠️ IMPORTANT: Previous response asked user for more information!")
                lines.append(f"   Question was about: {last_query}")
        
        if self.user_preferences.get("name"):
            lines.append(f"\nUser name: {self.user_preferences['name']}")
        if self.user_preferences.get("default_project"):
            lines.append(f"Default project: {self.user_preferences['default_project']}")
        
        return "\n".join(lines)
    
    def clear(self):
        """Reset memory."""
        self.conversation_history = []
        self.entity_cache = {"issues": [], "boards": [], "users": [], "projects": []}
        self.save_memory()


class UniversalJiraAssistant:
    """Fully dynamic Jira assistant - handles ANY query type."""
    
    def __init__(self, server_command: str = "python", server_args: List[str] = None):
        if server_args is None:
            server_args = ["jira_mcp_server.py"]
        
        self.server_params = StdioServerParameters(command=server_command, args=server_args)
        self.session: Optional[ClientSession] = None
        self.available_tools: Dict[str, Any] = {}
        self.memory = ConversationMemory()
        self.model = genai.GenerativeModel(
            'gemini-2.5-flash',
            generation_config=genai.GenerationConfig(
                temperature=0.3,
                top_p=0.95,
                max_output_tokens=2048
            )
        )
    
    async def __aenter__(self):
        self._stdio_client = stdio_client(self.server_params)
        self._read_write = await self._stdio_client.__aenter__()
        self.session = await ClientSession(self._read_write[0], self._read_write[1]).__aenter__()
        await self.session.initialize()
        
        tools_result = await self.session.list_tools()
        self.available_tools = {t.name: t for t in tools_result.tools}
        
        print(f"\n{'='*70}")
        print(f"🤖 Universal Jira Assistant - Fully Dynamic")
        print(f"{'='*70}")
        print(f"📊 Connected to: {JIRA_BASE}")
        print(f"🔧 Available tools: {len(self.available_tools)}")
        print(f"🧠 Memory: {len(self.memory.conversation_history)} conversations loaded")
        print(f"✨ Mode: 100% LLM-driven (handles any query)")
        print(f"{'='*70}\n")
        
        return self
    
    async def __aexit__(self, exc_type, exc, tb):
        if self.session:
            await self.session.__aexit__(exc_type, exc, tb)
        await self._stdio_client.__aexit__(exc_type, exc, tb)
    
    def _get_comprehensive_tools_info(self) -> str:
        """Generate detailed tool documentation."""
        tools_info = {
            "get_issue": "Get full details of a specific issue by key (e.g., CT-3)",
            "search_issues": "Search issues using JQL. CRITICAL: Use ISO dates like 'updated >= \"2025-11-17\"'",
            "get_issue_comments": "Get all comments for a specific issue",
            "add_comment": "Add a new comment to an issue",
            "create_issue": "Create a new Jira issue (requires project, summary, type)",
            "transition_issue": "Change issue status (e.g., move to Done, In Progress)",
            "list_boards": "List all accessible Jira boards/projects",
            "list_sprints": "List sprints for a board (requires board_id)",
            "list_board_issues": "Get issues for a specific board",
            "get_users": "Search for users by name or email",
            "get_recent_comments": "Get recent comments across all issues (specify days)",
            "get_dashboard_data": "Get project metrics, status counts, priority distribution",
            "get_priorities": "List all available priority levels",
            "throughput": "Calculate throughput (issues completed in time period)",
            "issue_cycle_times": "Analyze cycle times from start to completion",
            "extract_comments_by_user": "Get all comments by a specific user (deprecated - use get_comments_by_author)",
            "extract_components_by_user": "Get components grouped by assignee",
            "get_dependencies": "CRITICAL: Get issue dependencies and link relationships (use for dependency graphs, issue relationships)",
            "search_jira": "Enhanced search with comments and links (returns full issue objects with relationships)",
            "get_comments_by_author": "CRITICAL: Get comments by specific author/user. Supports partial names (e.g., 'rav', 'ravi', 'ravinder'). ALWAYS use this for 'comments by [name]' queries"
        }
        
        lines = ["AVAILABLE JIRA TOOLS:"]
        for tool_name, desc in tools_info.items():
            if tool_name in self.available_tools:
                lines.append(f"• {tool_name}: {desc}")
        
        return "\n".join(lines)
    
    async def analyze_and_respond(self, user_query: str) -> Dict[str, Any]:
        """
        Universal LLM analyzer - decides EVERYTHING:
        1. Is this casual chat or Jira work?
        2. What tools (if any) are needed?
        3. How to respond naturally?
        """
        
        # Pre-check: Common Jira patterns that should NEVER be chat
        query_lower = user_query.lower()
        jira_keywords = [
            ('comment', 'by'), ('comment', 'from'), ('comments', 'by'), ('comments', 'from'),
            ('search', 'comment'), ('get', 'comment'), ('find', 'comment'),
            ('search', 'issue'), ('get', 'issue'), ('find', 'issue'),
            ('show', 'issue'), ('list', 'issue'), ('recent', 'issue'),
            ('dependency', 'graph'), ('dependencies',), ('issue', 'link'),
            ('dashboard',), ('metric',), ('velocity',), ('throughput',),
            ('sprint',), ('board',), ('project',)
        ]
        
        forced_jira_query = False
        for keywords in jira_keywords:
            if all(kw in query_lower for kw in keywords):
                forced_jira_query = True
                break
        
        context = self.memory.get_context_for_llm()
        tools_info = self._get_comprehensive_tools_info()
        current_date = datetime.now().date().isoformat()
        seven_days_ago = (datetime.now().date() - timedelta(days=7)).isoformat()
        
        analysis_prompt = f"""You are a versatile AI assistant specializing in Jira project management. You can handle ANYTHING:
- Casual conversation ("Hi", "How are you?", "Thanks!")
- Jira queries ("show me recent issues", "what's the status of CT-3?")
- Jira actions ("add comment to CT-3", "create a bug for login issue")
- Complex analysis ("show me team velocity", "what are recurring problems?")

⚠️ CRITICAL CLASSIFICATION WARNING:
ANY request for Jira data (issues, comments, users, boards, metrics) MUST be classified as "jira_query" 
or "jira_action", NEVER as "chat" - even if phrased formally like "Search for...", "Get...", "Find..."

Examples of jira_query (NOT chat):
✓ "Search for comments by Ravinder" → jira_query (use get_comments_by_author)
✓ "Get comments by John" → jira_query (use get_comments_by_author)
✓ "Find issues in CT" → jira_query (use search_issues)
✓ "Show me users" → jira_query (use get_users)

Examples of chat (no tools needed):
✓ "Hi there!" → chat (greeting)
✓ "Thanks!" → chat (appreciation)
✓ "Goodbye" → chat (farewell)

TODAY'S DATE: {current_date}
SEVEN DAYS AGO: {seven_days_ago}

{tools_info}

CONVERSATION CONTEXT:
{context}

USER QUERY: "{user_query}"

CRITICAL CONTEXT RULES:
1. **Read the conversation history carefully** - if the previous query mentioned something specific, consider it context
2. **Short queries** (like "CT-3" or "that one") are usually follow-ups - link them to previous requests
3. **Dependency/link queries** should ALWAYS use get_dependencies or search_jira tools
4. **Issue keys alone** (CT-3, PROJ-123) are often asking to continue the previous action on that issue
5. If previous query asked for something you couldn't do, and user provides more info, connect them!
6. **CRITICAL: Any query about "comments by [name]" or "search for comments by [name]" MUST use get_comments_by_author tool - NEVER respond as chat!**
7. **CRITICAL: Even if the query says "Search for..." or uses formal language, if it's asking for Jira data, it's a jira_query!**

YOUR TASK:
Analyze the query and decide how to respond. Return JSON:

{{
  "query_type": "chat|jira_query|jira_action|analysis",
  "needs_jira_tools": true/false,
  "understanding": "What does the user want?",
  "response_strategy": "How will you respond?",
  "tool_calls": [
    {{
      "tool_name": "tool_name",
      "tool_args": {{"arg": "value"}},
      "reasoning": "Why this tool"
    }}
  ],
  "direct_response": "If no tools needed, respond directly here",
  "extracted_entities": {{
    "issues": ["CT-3"],
    "users": ["John"],
    "boards": ["Board 1"],
    "projects": ["CT"]
  }}
}}

QUERY TYPE GUIDE:
- "chat": ONLY for greetings ("Hi", "Hello", "Thanks"), farewells ("Bye"), or appreciation → No tools needed
- "jira_query": ANY request for Jira information (even if phrased formally like "Search for...") → Need query tools
- "jira_action": Modifying Jira data (add comment, create issue, transition) → Need action tools
- "analysis": Complex questions needing multiple tools or calculations

⚠️ IMPORTANT CLASSIFICATION RULES:
- "Search for comments by X" → jira_query (use get_comments_by_author)
- "Get comments by X" → jira_query (use get_comments_by_author)  
- "Comments by X" → jira_query (use get_comments_by_author)
- "What did X comment" → jira_query (use get_comments_by_author)
- "Show me X's comments" → jira_query (use get_comments_by_author)
- DO NOT classify Jira data requests as "chat" even if they use words like "search", "find", "get"!

CRITICAL JQL RULES:
- ALWAYS use ISO dates: "updated >= \\"{current_date}\\""
- NEVER use "-7d" or "startOfDay(-7)" 
- For "recent": use {seven_days_ago}
- Fields parameter must be a LIST: ["summary", "status"]

REFERENCE RESOLUTION:
- Use conversation context to resolve "that issue", "those users", etc.
- Check entity cache for recently mentioned items

EXAMPLES:

Example 1 - Casual Chat:
User: "Hi! How are you?"
{{
  "query_type": "chat",
  "needs_jira_tools": false,
  "understanding": "User is greeting me",
  "response_strategy": "Respond warmly and offer help",
  "tool_calls": [],
  "direct_response": "Hi there! 👋 I'm doing great, thanks for asking! I'm your Jira assistant, ready to help you with anything from checking issue status to analyzing team metrics. What can I help you with today?",
  "extracted_entities": {{"issues": [], "users": [], "boards": [], "projects": []}}
}}

Example 2 - Simple Jira Query:
User: "show me recent CT issues"
{{
  "query_type": "jira_query",
  "needs_jira_tools": true,
  "understanding": "User wants recently updated issues from CT project",
  "response_strategy": "Search for CT issues updated in last 7 days",
  "tool_calls": [{{
    "tool_name": "search_issues",
    "tool_args": {{"jql": "project = CT AND updated >= \\"{seven_days_ago}\\" ORDER BY updated DESC", "max_results": 50}},
    "reasoning": "Search CT project for recent activity"
  }}],
  "direct_response": null,
  "extracted_entities": {{"issues": [], "users": [], "boards": [], "projects": ["CT"]}}
}}

Example 3 - Jira Action:
User: "add comment to CT-3: Fixed the bug"
Context: CT-3 exists in cache
{{
  "query_type": "jira_action",
  "needs_jira_tools": true,
  "understanding": "User wants to add a comment to issue CT-3",
  "response_strategy": "Add comment using add_comment tool",
  "tool_calls": [{{
    "tool_name": "add_comment",
    "tool_args": {{"issue_key": "CT-3", "body": "Fixed the bug"}},
    "reasoning": "Add user's comment to the issue"
  }}],
  "direct_response": null,
  "extracted_entities": {{"issues": ["CT-3"], "users": [], "boards": [], "projects": ["CT"]}}
}}

Example 4 - Complex Analysis:
User: "what's our team's velocity this month?"
{{
  "query_type": "analysis",
  "needs_jira_tools": true,
  "understanding": "User wants throughput and completion metrics",
  "response_strategy": "Get dashboard data and throughput metrics",
  "tool_calls": [
    {{"tool_name": "throughput", "tool_args": {{"lookback_days": 30}}, "reasoning": "Calculate completion rate"}},
    {{"tool_name": "get_dashboard_data", "tool_args": {{"lookback_days": 30}}, "reasoning": "Get status distribution"}}
  ],
  "direct_response": null,
  "extracted_entities": {{"issues": [], "users": [], "boards": [], "projects": []}}
}}

Example 5 - Thank You:
User: "Thanks! That's perfect"
{{
  "query_type": "chat",
  "needs_jira_tools": false,
  "understanding": "User is expressing gratitude",
  "response_strategy": "Acknowledge and offer continued help",
  "tool_calls": [],
  "direct_response": "You're very welcome! 😊 Glad I could help! Let me know if you need anything else with your Jira projects.",
  "extracted_entities": {{"issues": [], "users": [], "boards": [], "projects": []}}
}}

Example 6 - Follow-up with Context:
User: "show me comments on that"
Context: Last issue discussed was CT-3
{{
  "query_type": "jira_query",
  "needs_jira_tools": true,
  "understanding": "User wants comments on previously discussed issue CT-3",
  "response_strategy": "Get comments for CT-3 from context",
  "tool_calls": [{{
    "tool_name": "get_issue_comments",
    "tool_args": {{"issue_key": "CT-3"}},
    "reasoning": "Resolved 'that' to CT-3 from conversation context"
  }}],
  "direct_response": null,
  "extracted_entities": {{"issues": ["CT-3"], "users": [], "boards": [], "projects": []}}
}}

Example 7 - Dependency Graph Request:
User: "get dependency graph"
{{
  "query_type": "jira_query",
  "needs_jira_tools": true,
  "understanding": "User wants to see issue dependencies and relationships",
  "response_strategy": "Use get_dependencies tool to analyze issue links",
  "tool_calls": [{{
    "tool_name": "get_dependencies",
    "tool_args": {{"lookback_days": 30, "limit": 100}},
    "reasoning": "Fetch dependency relationships across recent issues"
  }}],
  "direct_response": null,
  "extracted_entities": {{"issues": [], "users": [], "boards": [], "projects": []}}
}}

Example 8 - Context-Based Follow-up:
User: "get dependency graph"
(Previous response: "I need an issue key")
User: "CT-3"
Context: User just asked for dependency graph
{{
  "query_type": "jira_query",
  "needs_jira_tools": true,
  "understanding": "User is providing CT-3 as the issue for the previously requested dependency graph",
  "response_strategy": "Connect to previous request - get dependencies for CT-3 using search_jira with issuelinks",
  "tool_calls": [
    {{
      "tool_name": "get_issue",
      "tool_args": {{"issue_key": "CT-3"}},
      "reasoning": "Get full issue details including links"
    }},
    {{
      "tool_name": "search_jira",
      "tool_args": {{"jql": "issue = CT-3 OR issuekey in linkedIssues(CT-3)", "fields": ["summary", "status", "issuelinks"], "limit": 50}},
      "reasoning": "Get CT-3 and all linked issues to show dependency graph"
    }}
  ],
  "direct_response": null,
  "extracted_entities": {{"issues": ["CT-3"], "users": [], "boards": [], "projects": ["CT"]}}
}}

Example 9 - Issue Key as Follow-up:
User: "who's working on high priority bugs?"
(Response shows: CT-5, CT-8, CT-12)
User: "CT-5"
Context: User just saw list of bugs
{{
  "query_type": "jira_query",
  "needs_jira_tools": true,
  "understanding": "User wants details about CT-5 from the previous list",
  "response_strategy": "Get full details of CT-5",
  "tool_calls": [{{
    "tool_name": "get_issue",
    "tool_args": {{"issue_key": "CT-5"}},
    "reasoning": "User is asking for more info on CT-5 from previous results"
  }}],
  "direct_response": null,
  "extracted_entities": {{"issues": ["CT-5"], "users": [], "boards": [], "projects": ["CT"]}}
}}

Example 10 - Comments by Specific User:
User: "comments by ravinder"
{{
  "query_type": "jira_query",
  "needs_jira_tools": true,
  "understanding": "User wants to see all comments made by Ravinder",
  "response_strategy": "Use get_comments_by_author tool with partial name matching",
  "tool_calls": [{{
    "tool_name": "get_comments_by_author",
    "tool_args": {{"author_query": "ravinder", "days": 30, "limit": 100}},
    "reasoning": "Get all comments where author name contains 'ravinder'"
  }}],
  "direct_response": null,
  "extracted_entities": {{"issues": [], "users": ["ravinder"], "boards": [], "projects": []}}
}}

Example 11 - Comments by User with Partial Name:
User: "what did rivo comment?"
{{
  "query_type": "jira_query",
  "needs_jira_tools": true,
  "understanding": "User wants comments from user 'rivo' (likely partial name)",
  "response_strategy": "Use get_comments_by_author with partial name search",
  "tool_calls": [{{
    "tool_name": "get_comments_by_author",
    "tool_args": {{"author_query": "rivo", "days": 30, "limit": 100}},
    "reasoning": "Search for comments by any author matching 'rivo' in their name"
  }}],
  "direct_response": null,
  "extracted_entities": {{"issues": [], "users": ["rivo"], "boards": [], "projects": []}}
}}

Example 12 - Formal Search Query (MUST be jira_query, NOT chat):
User: "Search for comments by 'Ravinder'"
{{
  "query_type": "jira_query",
  "needs_jira_tools": true,
  "understanding": "User wants to search for comments written by Ravinder. Even though phrased formally with 'Search for', this is a Jira data request",
  "response_strategy": "Use get_comments_by_author tool to find all Ravinder's comments",
  "tool_calls": [{{
    "tool_name": "get_comments_by_author",
    "tool_args": {{"author_query": "Ravinder", "days": 30, "limit": 100}},
    "reasoning": "This is a Jira query despite formal phrasing - must use tool to search comments"
  }}],
  "direct_response": null,
  "extracted_entities": {{"issues": [], "users": ["Ravinder"], "boards": [], "projects": []}}
}}

Example 13 - Get Comments (Another formal variation):
User: "Get comments by John"
{{
  "query_type": "jira_query",
  "needs_jira_tools": true,
  "understanding": "User wants to retrieve comments by John from Jira",
  "response_strategy": "Use get_comments_by_author to fetch John's comments",
  "tool_calls": [{{
    "tool_name": "get_comments_by_author",
    "tool_args": {{"author_query": "John", "days": 30, "limit": 100}},
    "reasoning": "Standard comment search by author request"
  }}],
  "direct_response": null,
  "extracted_entities": {{"issues": [], "users": ["John"], "boards": [], "projects": []}}
}}

RESPOND WITH VALID JSON ONLY."""

        try:
            response = self.model.generate_content(analysis_prompt)
            text = response.text.strip()
            
            # Extract JSON
            if text.startswith("```"):
                start = text.find("{")
                end = text.rfind("}") + 1
                if start != -1 and end > start:
                    text = text[start:end]
            
            analysis = json.loads(text)
            
            # Safety override: If we detected Jira keywords but LLM said "chat", correct it
            if forced_jira_query and analysis.get("query_type") == "chat":
                print("⚠️ LLM misclassified Jira query as chat - correcting...")
                
                # Force proper classification based on keywords
                if any(kw in query_lower for kw in ['comment', 'comments']):
                    # Extract author name
                    import re
                    name_match = re.search(r'(?:by|from)\s+["\']?([a-zA-Z]+)["\']?', user_query, re.IGNORECASE)
                    author = name_match.group(1) if name_match else "user"
                    
                    analysis = {
                        "query_type": "jira_query",
                        "needs_jira_tools": True,
                        "understanding": f"User wants comments by {author} - corrected from misclassification",
                        "response_strategy": "Use get_comments_by_author tool",
                        "tool_calls": [{
                            "tool_name": "get_comments_by_author",
                            "tool_args": {"author_query": author, "days": 30, "limit": 100},
                            "reasoning": "Corrected: This is a Jira comment search"
                        }],
                        "direct_response": None,
                        "extracted_entities": {"issues": [], "users": [author], "boards": [], "projects": []}
                    }
                else:
                    # Generic correction for other Jira queries
                    analysis["query_type"] = "jira_query"
                    analysis["needs_jira_tools"] = True
                    analysis["understanding"] = "Jira data request - corrected from misclassification"
            
            return analysis
            
        except Exception as e:
            print(f"❌ Analysis failed: {e}")
            # Return safe fallback
            return {
                "query_type": "chat",
                "needs_jira_tools": False,
                "understanding": "I had trouble processing that",
                "response_strategy": "Ask for clarification",
                "tool_calls": [],
                "direct_response": "I'm not quite sure how to help with that. Could you rephrase or provide more details?",
                "extracted_entities": {"issues": [], "users": [], "boards": [], "projects": []},
                "error": str(e)
            }
    
    async def execute_tool(self, tool_name: str, tool_args: Dict[str, Any]) -> Any:
        """Execute Jira tool."""
        if tool_name not in self.available_tools:
            return {"isError": True, "error": f"Unknown tool: {tool_name}"}
        
        try:
            result = await self.session.call_tool(tool_name, arguments=tool_args)
            return result
        except Exception as e:
            return {"isError": True, "error": str(e)}
    
    async def format_results_naturally(self, tool_results: List[Dict[str, Any]], 
                                       original_query: str, analysis: Dict[str, Any]) -> str:
        """LLM creates natural, conversational response."""
        
        results_data = []
        for r in tool_results:
            tool_name = r["tool_name"]
            result = r["result"]
            
            # Extract data
            if hasattr(result, "structuredContent"):
                data = result.structuredContent
            else:
                try:
                    texts = [json.loads(c.text) for c in getattr(result, "content", [])]
                    data = texts[0] if len(texts) == 1 else texts
                except:
                    data = str(result)[:800]
            
            results_data.append({
                "tool": tool_name,
                "data": str(data)[:2000]
            })
        
        formatting_prompt = f"""You are responding to a colleague about Jira. Be natural, friendly, and helpful.

USER ASKED: "{original_query}"
QUERY TYPE: {analysis.get('query_type', 'unknown')}
YOUR UNDERSTANDING: {analysis.get('understanding', 'N/A')}

WHAT YOU FOUND:
{json.dumps(results_data, indent=2)}

RESPOND NATURALLY:
• Talk like a helpful colleague, not a robot
• Use "I", "you", conversational phrases
• Add emojis for clarity (📝🔍✅💬👤📋🎯📊⚡🐛🎉)
• Highlight important info
• Offer insights or next steps
• If multiple items, prioritize and summarize
• Ask follow-up questions when helpful

EXAMPLES:

For recent issues:
"🔍 I found 8 issues active in the last week! Here's what stands out:

**Needs Attention:**
• CT-3: Login bug - In progress, looks like John's working on it
• CT-8: Performance issue - Still waiting to be picked up

**Recently Completed:**
• CT-5: Documentation update - Closed yesterday, nice work!

Want me to dive deeper into any of these?"

For adding comment:
"✅ Done! Your comment is now on CT-3. The team will see it in their notifications."

For metrics:
"📊 Here's how things look this month:

**Velocity:** 23 issues completed (up from 18 last month! 🎉)
**Current Load:** 12 in progress, 5 waiting to start
**Focus Areas:** 3 high-priority items need attention

Overall, the team's moving at a healthy pace. The completion rate is solid!"

For users:
"👥 I found 5 active team members:

• John Doe - Main developer, very active
• Jane Smith - Product lead
• Mike Chen - Design

Everyone's account is active. Who do you need to connect with?"

RESPOND NOW (no JSON, just natural text):"""

        try:
            response = self.model.generate_content(
                formatting_prompt,
                generation_config=genai.GenerationConfig(
                    temperature=0.7,  # More creative
                    top_p=0.95,
                    max_output_tokens=1200
                )
            )
            return response.text.strip()
        except Exception as e:
            return f"I found the information, but had trouble formatting it nicely. Here's what I got: {json.dumps(results_data, indent=2)[:500]}"
    
    async def process_query(self, user_query: str):
        """Universal query processor - handles ANYTHING."""
        print(f"\n💬 You: {user_query}")
        
        # Step 1: LLM analyzes query
        analysis = await self.analyze_and_respond(user_query)
        
        query_type = analysis.get("query_type", "unknown")
        needs_tools = analysis.get("needs_jira_tools", False)
        understanding = analysis.get("understanding", "Processing...")
        
        print(f"🧠 Type: {query_type} | Understanding: {understanding}")
        
        # Step 2: Direct response or tool execution
        if not needs_tools:
            # Pure chat - no Jira tools needed
            response = analysis.get("direct_response", "I'm here to help!")
            print(f"\n🤖 {response}\n")
            
            self.memory.add_interaction(
                user_query=user_query,
                assistant_response=response,
                interaction_type=query_type
            )
            return
        
        # Step 3: Execute Jira tools
        tool_calls = analysis.get("tool_calls", [])
        if not tool_calls:
            print("\n🤖 I'm not sure how to help with that. Could you provide more details?\n")
            return
        
        print(f"🔧 Executing {len(tool_calls)} tool(s)...")
        results = []
        for call in tool_calls:
            tool_name = call["tool_name"]
            tool_args = call["tool_args"]
            reasoning = call.get("reasoning", "")
            
            print(f"  📋 {reasoning}")
            result = await self.execute_tool(tool_name, tool_args)
            results.append({"tool_name": tool_name, "result": result})
        
        # Step 4: Natural language formatting
        print("\n🤖 ", end="")
        formatted_response = await self.format_results_naturally(results, user_query, analysis)
        print(f"{formatted_response}\n")
        
        # Step 5: Update memory
        extracted = analysis.get("extracted_entities", {})
        tool_names = [c["tool_name"] for c in tool_calls]
        
        self.memory.add_interaction(
            user_query=user_query,
            assistant_response=formatted_response,
            interaction_type=query_type,
            tool_calls=tool_names,
            entities=extracted
        )
    
    async def chat_loop(self):
        """Interactive conversation loop."""
        print("🤖 Hi! I'm your Jira assistant. I can help with:")
        print("   • Casual chat and questions")
        print("   • Finding and analyzing Jira issues")
        print("   • Creating and updating tickets")
        print("   • Team metrics and insights")
        print("\n💡 Just ask me anything naturally!")
        print("\n📝 Commands: 'memory' (view context) | 'clear' (reset) | 'quit' (exit)")
        print("="*70 + "\n")
        
        while True:
            try:
                query = input("💭 You: ").strip()
                
                if not query:
                    continue
                
                if query.lower() in ["quit", "exit", "q", "bye"]:
                    print("\n👋 Goodbye! Thanks for chatting. Your conversation is saved for next time!\n")
                    break
                
                if query.lower() == "clear":
                    self.memory.clear()
                    print("🧹 Memory cleared! Starting fresh.\n")
                    continue
                
                if query.lower() == "memory":
                    print(f"\n{self.memory.get_context_for_llm()}\n")
                    continue
                
                await self.process_query(query)
                
            except KeyboardInterrupt:
                print("\n\n👋 Goodbye! Conversation saved.\n")
                break
            except Exception as e:
                print(f"\n❌ Oops, something went wrong: {e}")
                print("Let's try that again!\n")


async def main():
    async with UniversalJiraAssistant(
        server_command="python",
        server_args=["jira_mcp_server.py"]
    ) as assistant:
        await assistant.chat_loop()


if __name__ == "__main__":
    asyncio.run(main())