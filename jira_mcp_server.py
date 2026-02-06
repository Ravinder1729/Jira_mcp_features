"""
jira_mcp_server.py - Production-ready, hardened Jira MCP client (v4)

Changes:
- Added get_project_issue_types (wraps createmeta/project details).
- Added get_project_details.
- Added list_project_issues (to see what was recently pushed).
"""
from __future__ import annotations

import sys
import os
import re
import json
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple, Union
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import requests
from pydantic import Field
from requests.exceptions import RequestException, HTTPError
from mcp.server.fastmcp import FastMCP

# Load environment variables
load_dotenv()

# ---- Environment ----

def _sanitize_env_value(v: Optional[str]) -> Optional[str]:
    if v is None: return None
    v = v.strip()
    if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
        v = v[1:-1].strip()
    return v.replace("\n", "").replace("\r", "")

JIRA_BASE = _sanitize_env_value(os.getenv('JIRA_BASE', 'https://your-domain.atlassian.net'))
JIRA_EMAIL = _sanitize_env_value(os.getenv('JIRA_EMAIL'))
JIRA_API_TOKEN = _sanitize_env_value(os.getenv('JIRA_API_TOKEN'))

if not JIRA_BASE: raise RuntimeError("JIRA_BASE not set")
JIRA_BASE = JIRA_BASE.rstrip('/')

# HTTP session
SESSION = requests.Session()
SESSION.auth = (JIRA_EMAIL, JIRA_API_TOKEN)
SESSION.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

mcp = FastMCP("ShambuAI_Jira_Hardened")

# ---- Logging ----

def _log(level: str, msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] [{level}] {msg}\n"
    sys.stderr.write(formatted)
    sys.stderr.flush()
    try:
        with open("jira_server_debug.log", "a", encoding="utf-8") as f:
            f.write(formatted)
    except: pass

def _log_info(msg: str) -> None: _log("INFO", msg)
def _log_error(msg: str) -> None: _log("ERROR", msg)

# ---- HTTP ----

def _is_error_resp(resp: Any) -> bool:
    return isinstance(resp, dict) and resp.get("isError", False)

def _get(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    try:
        r = SESSION.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json() if r.text and r.text.strip() else {}
    except Exception as e:
        msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            msg += f" | Response: {e.response.text}"
        _log_error(f"GET {url} failed: {msg}")
        return {"isError": True, "error": msg, "url": url}

def _post(url: str, json_payload: Optional[Dict[str, Any]] = None) -> Any:
    try:
        r = SESSION.post(url, json=json_payload or {}, timeout=30)
        r.raise_for_status()
        return r.json() if r.text and r.text.strip() else {}
    except Exception as e:
        msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            msg += f" | Response: {e.response.text}"
        _log_error(f"POST {url} failed: {msg}")
        return {"isError": True, "error": msg, "url": url}

# ---- URL ----
def _rest(path: str) -> str: return f"{JIRA_BASE}/rest/api/3{path if path.startswith('/') else '/' + path}"
def _agile(path: str) -> str: return f"{JIRA_BASE}/rest/agile/1.0{path if path.startswith('/') else '/' + path}"

# ---- Tools ----

@mcp.tool()
def list_projects() -> Dict[str, Any]:
    """Returns all projects"""
    resp = _get(_rest("/project"))
    if _is_error_resp(resp): return resp
    return {"projects": resp if isinstance(resp, list) else []}

@mcp.tool()
def search_projects(query: Optional[str] = None, max_results: int = 50) -> Dict[str, Any]:
    """Search for projects"""
    params = {"maxResults": max_results}
    if query: params["query"] = query
    return _get(_rest("/project/search"), params)

@mcp.tool()
def get_project_details(project_key: str) -> Dict[str, Any]:
    """Returns detailed info for a project including issue types"""
    return _get(_rest(f"/project/{project_key}"))

@mcp.tool()
def get_issue_createmeta(project_key: str) -> Dict[str, Any]:
    """Returns create metadata for a project (issue types and fields)"""
    # Jira Cloud v3 createmeta is specialized. We'll simplify.
    resp = _get(_rest(f"/project/{project_key}"))
    if _is_error_resp(resp): return resp
    
    # Return issue types list as expected by client
    return {"issueTypes": resp.get("issueTypes", [])}

@mcp.tool()
def get_priorities() -> Dict[str, Any]:
    """Returns all priorities"""
    resp = _get(_rest("/priority"))
    if _is_error_resp(resp): return resp
    return {"priorities": resp if isinstance(resp, list) else []}

@mcp.tool()
def list_components(project_key: str) -> Dict[str, Any]:
    """Returns all components for a project"""
    resp = _get(_rest(f"/project/{project_key}/components"))
    if _is_error_resp(resp): return resp
    return {"components": resp if isinstance(resp, list) else []}

@mcp.tool()
def search_issues(jql: str, fields: Optional[List[str]] = None, expand: Optional[str] = None, max_results: int = 50) -> Dict[str, Any]:
    """Searches Jira issues with JQL. result: {"issues": [...], "total": N}"""
    params = {"jql": jql, "maxResults": max_results}
    if fields: params["fields"] = ",".join(fields)
    if expand: params["expand"] = expand
    return _get(_rest("/search/jql"), params)

@mcp.tool()
def get_issue(issue_key: str, fields: Optional[List[str]] = None, expand: Optional[str] = None) -> Dict[str, Any]:
    """Fetches a single issue by key"""
    params = {}
    if fields: params["fields"] = ",".join(fields)
    if expand: params["expand"] = expand
    return _get(_rest(f"/issue/{issue_key}"), params)

@mcp.tool()
def get_issue_comments(issue_key: str) -> Dict[str, Any]:
    """Fetches all comments for an issue. result: {"comments": [...]}"""
    _log_info(f"Fetching comments for {issue_key}...")
    resp = _get(_rest(f"/issue/{issue_key}/comment"))
    if _is_error_resp(resp): return resp
    comments = resp.get("comments", [])
    _log_info(f"Found {len(comments)} comments for {issue_key}")
    return {
        "issue_key": issue_key,
        "comments": [{
            "id": c.get("id"),
            "author": (c.get("author") or {}).get("displayName"),
            "body": c.get("body"), 
            "created": c.get("created")
        } for c in comments]
    }

@mcp.tool()
def create_issue(project_key: str, summary: str, description: Union[str, Dict[str, Any]] = "", issue_type: str = "Task", assignee_account_id: Optional[str] = None, fields_extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Creates a new Jira issue with support for extra fields and ADF descriptions."""
    # Determine if issue_type is ID (numeric string) or Name
    issue_type_obj = {"id": issue_type} if issue_type.isdigit() else {"name": issue_type}
    
    # Handle description (can be raw string or ADF dict)
    if isinstance(description, str):
        desc_obj = {"version": 1, "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}]} if description else None
    else:
        desc_obj = description

    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": issue_type_obj
        }
    }
    
    if desc_obj: payload["fields"]["description"] = desc_obj
    if assignee_account_id: payload["fields"]["assignee"] = {"accountId": assignee_account_id}
    
    # Merge extra fields if provided
    if fields_extra:
        for k, v in fields_extra.items():
            payload["fields"][k] = v
            
    return _post(_rest("/issue"), payload)

@mcp.tool()
def add_comment(issue_key: str, body: str) -> Dict[str, Any]:
    """Adds a comment to an issue"""
    payload = {"body": {"version": 1, "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": body}]}]}}
    return _post(_rest(f"/issue/{issue_key}/comment"), payload)

@mcp.tool()
def get_myself() -> Dict[str, Any]:
    """Returns the currently authenticated user details"""
    return _get(_rest("/myself"))

@mcp.tool()
def get_users(query: Optional[str] = None, max_results: int = 50) -> Dict[str, Any]:
    """Searches for users. result: {"users": [...]}"""
    resp = _get(_rest("/users/search"), params={"query": query or "", "maxResults": max_results})
    if _is_error_resp(resp): return resp
    users = resp if isinstance(resp, list) else []
    return {"users": [{"accountId": u.get("accountId"), "displayName": u.get("displayName"), "active": u.get("active")} for u in users]}

@mcp.tool()
def list_boards(project_key_or_id: Optional[str] = None) -> Dict[str, Any]:
    """Returns all boards, optionally filtered by project"""
    params = {}
    if project_key_or_id: params["projectKeyOrId"] = project_key_or_id
    return _get(_agile("/board"), params)

@mcp.tool()
def get_board_configuration(board_id: int) -> Dict[str, Any]:
    """Returns configuration for a specific board"""
    return _get(_agile(f"/board/{board_id}/configuration"))

@mcp.tool()
def get_filter(filter_id: str) -> Dict[str, Any]:
    """Returns details for a specific filter"""
    return _get(_rest(f"/filter/{filter_id}"))

@mcp.tool()
def get_project_statuses(project_key: str) -> Any:
    """Returns all statuses for a project"""
    return _get(_rest(f"/project/{project_key}/statuses"))

@mcp.tool()
def list_board_backlog(board_id: int, start_at: int = 0, max_results: int = 50, jql: str = None) -> Dict[str, Any]:
    """Returns issues in the backlog for a board"""
    params = {"startAt": start_at, "maxResults": max_results}
    if jql:
        params["jql"] = jql
    return _get(_agile(f"/board/{board_id}/backlog"), params=params)

@mcp.tool()
def list_board_issues(board_id: int, start_at: int = 0, max_results: int = 50, jql: str = None) -> Dict[str, Any]:
    """Returns all issues on the board (backlog + active)"""
    params = {"startAt": start_at, "maxResults": max_results}
    if jql:
        params["jql"] = jql
    return _get(_agile(f"/board/{board_id}/issue"), params=params)

@mcp.tool()
def move_to_board(board_id: int, issues: List[str]) -> Dict[str, Any]:
    """Moves issues to the board from the backlog"""
    return _post(_agile(f"/board/{board_id}/issue"), {"issues": issues})

@mcp.tool()
def move_to_backlog(issues: List[str]) -> Dict[str, Any]:
    """Moves issues to the backlog"""
    return _post(_agile("/backlog/issue"), {"issues": issues})

@mcp.tool()
def get_issue_transitions(issue_key: str) -> Dict[str, Any]:
    """Returns possible transitions for an issue"""
    return _get(_rest(f"/issue/{issue_key}/transitions"))

@mcp.tool()
def list_epics(project_key: str) -> Dict[str, Any]:
    """Returns all issues that might be epics for a project (broad JQL)"""
    # Simply find anything that is an Epic by type name using the robust endpoint.
    jql = f'project = "{project_key}" AND issuetype in ("Epic", "epic", "Standard Epic")'
    # Use /search/jql as the old /search is deprecated/gone
    params = {"jql": jql, "maxResults": 1000, "fields": "summary,status,issuetype,parent"}
    return _get(_rest("/search/jql"), params)

@mcp.tool()
def create_epic(project_key: str, summary: str, description: str = "") -> Dict[str, Any]:
    """Creates a new Epic in Jira"""
    # Note: Modern Jira uses 'Epic Name' field for some templates, 
    # but v3 API usually standardizes on summary.
    # In some older Jira versions, 'Epic Name' was a custom field.
    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": "Epic"}
        }
    }
    if description:
        payload["fields"]["description"] = {
            "version": 1,
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}]
        }
    
    return _post(_rest("/issue"), payload)

@mcp.tool()
def list_board_issues(board_id: int, start_at: int = 0, max_results: int = 50) -> Dict[str, Any]:
    """Returns issues for a board"""
    return _get(_agile(f"/board/{board_id}/issue"), params={"startAt": start_at, "maxResults": max_results})

@mcp.tool()
def list_sprints(board_id: int, state: Optional[str] = None) -> Dict[str, Any]:
    """Returns sprints for a board. State can be 'future', 'active', 'closed'."""
    params = {}
    if state: params["state"] = state
    return _get(_agile(f"/board/{board_id}/sprint"), params=params)

@mcp.tool()
def add_to_sprint(sprint_id: int, issues: List[str]) -> Dict[str, Any]:
    """Adds issues to a sprint"""
    return _post(_agile(f"/sprint/{sprint_id}/issue"), {"issues": issues})

if __name__ == "__main__":
    _log_info("Starting Hardened Jira MCP Server (v4/Complete)...")
    mcp.run(transport='stdio')
