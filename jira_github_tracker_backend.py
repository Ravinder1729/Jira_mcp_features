"""
Backend Module: Jira-GitHub Story Tracker
Add this to your existing codebase to track user stories with GitHub commits
"""
import re
import os
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import google.generativeai as genai

# ==================================================
# Story Tracker Backend Class
# ==================================================
class JiraGitHubTracker:
    """
    Tracks Jira user stories and correlates them with GitHub commits
    from assigned developers
    """
    
    def __init__(self, jira_client, github_client, gemini_model=None):
        """
        Initialize tracker with existing MCP clients
        
        Args:
            jira_client: Your existing JiraClient instance
            github_client: Your existing GitHub MCP client (or REST API client)
            gemini_model: Optional GenerativeModel for validation
        """
        self.jira = jira_client
        self.github = github_client
        self.model = gemini_model
        
        # If no model provided, try to initialize from environment
        if not self.model and os.getenv("GEMINI_API_KEY"):
            try:
                genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
                self.model = genai.GenerativeModel("gemini-2.0-flash")
            except:
                pass
    
    # ==================================================
    # Core Tracking Functions
    # ==================================================
    
    async def get_user_stories_by_project(
        self, 
        project_key: str, 
        status: Optional[str] = None,
        assignee: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetch user stories from Jira project
        
        Args:
            project_key: Jira project key (e.g., "PROJ")
            status: Optional status filter (e.g., "In Progress")
            assignee: Optional assignee email filter
            
        Returns:
            List of Jira issues (user stories)
        """
        # Build JQL query
        jql = f'project = "{project_key}" AND type = Story'
        
        if status:
            jql += f' AND status = "{status}"'
        
        if assignee:
            jql += f' AND assignee = "{assignee}"'
        
        jql += ' ORDER BY created DESC'
        
        # Call Jira MCP server
        result = await self.jira.call("search_issues", {
            "jql": jql,
            "max_results": 100,
            "fields": ["summary", "status", "assignee", "created", "updated", "description"]
        })
        
        if isinstance(result, dict) and result.get("issues"):
            return result["issues"]
        return []
    
    async def get_issue_by_key(self, issue_key: str) -> Optional[Dict[str, Any]]:
        """
        Get single Jira issue by key
        
        Args:
            issue_key: Jira issue key (e.g., "PROJ-123")
            
        Returns:
            Issue details or None
        """
        result = await self.jira.call("get_issue", {"issue_key": issue_key})
        
        if isinstance(result, dict) and not result.get("error"):
            return result
        return None
    
    async def get_comments(self, issue_key: str) -> Dict[str, Any]:
        """
        Fetch comments for a Jira issue
        """
        try:
            result = await self.jira.call("get_issue_comments", {"issue_key": issue_key})
            print(f"DEBUG: get_issue_comments raw result type: {type(result)}")
            print(f"DEBUG: get_issue_comments raw result: {repr(result)[:500]}")
            
            # Handle list-wrapped error (from previous buggy version) OR direct error dict
            err_msg = None
            raw_comments = []
            
            if result is None:
                err_msg = "Tool returned None"
            elif isinstance(result, dict):
                if result.get("isError"):
                    err_msg = result.get("error")
                else:
                    # New version returns {"comments": [...]}
                    raw_comments = result.get("comments", [])
                    # If it didn't find "comments" key, maybe it's an old-style result-as-dict?
                    if not raw_comments and not result.get("issue_key"):
                        # If it's a dict that isn't our new wrapper, maybe it's empty or an error
                        pass
            elif isinstance(result, list):
                # Old version returned list
                raw_comments = result
            elif isinstance(result, str):
                try:
                    parsed = json.loads(result)
                    if isinstance(parsed, dict):
                        raw_comments = parsed.get("comments", [])
                    elif isinstance(parsed, list):
                        raw_comments = parsed
                except:
                    err_msg = f"Unexpected string result: {result[:100]}"
            
            print(f"DEBUG: Found {len(raw_comments)} raw comments from tool")
            if raw_comments:
                print(f"DEBUG: First raw comment keys: {list(raw_comments[0].keys())}")
            
            # If we have an error or no comments, try fallback by fetching the issue directly
            if err_msg or (not raw_comments and not isinstance(result, list)):
                print(f"DEBUG: get_issue_comments failed (err: {err_msg}) or empty. Trying fallback via get_issue...")
                issue = await self.get_issue_by_key(issue_key)
                if issue:
                    fields = issue.get("fields", {})
                    fallback_comments = fields.get("comment", {}).get("comments", [])
                    print(f"DEBUG: Fallback found {len(fallback_comments)} comments in issue fields")
                    if fallback_comments and not raw_comments:
                         # Normalize fallback comments
                         raw_comments = [{
                             "id": c.get("id"),
                             "author": (c.get("author") or {}).get("displayName"),
                             "body": c.get("body"),
                             "created": c.get("created")
                         } for c in fallback_comments]
                         err_msg = None # Clear error if we found comments via fallback

            # Process comments to ensure body is text (handling ADF)
            processed_comments = []
            for i, c in enumerate(raw_comments):
                if not isinstance(c, dict) or c.get("isError"):
                    print(f"DEBUG: Comment {i} is invalid or error: {c}")
                    continue
                    
                body = c.get("body", "")
                print(f"DEBUG: Processing comment {i}, body type: {type(body)}")
                if isinstance(body, dict):
                    # Extract text from ADF (more robust version)
                    try:
                        def extract_adf_text(node):
                            if not isinstance(node, dict): return ""
                            text = ""
                            if node.get("type") == "text":
                                text += node.get("text", "")
                            
                            # Recurse into content
                            for child in node.get("content", []):
                                text += extract_adf_text(child)
                            
                            # Add newlines for block types
                            if node.get("type") in ["paragraph", "blockquote", "codeBlock"]:
                                text += "\n"
                            elif node.get("type") == "listItem":
                                text = "• " + text
                            return text

                        body = extract_adf_text(body).strip()
                    except:
                        body = str(body)
                
                comment_info = c.copy()
                comment_info["body"] = body
                processed_comments.append(comment_info)
            
            return {"comments": processed_comments, "error": err_msg}
        except Exception as e:
            return {"error": str(e), "comments": []}
    
    async def get_commits_by_author(
        self,
        repo_owner: str,
        repo_name: str,
        author_identifier: str,
        since: datetime,
        branch: str = "main"
    ) -> List[Dict[str, Any]]:
        """
        Get GitHub commits by author email or username since a date
        
        Args:
            repo_owner: GitHub repository owner
            repo_name: Repository name
            author_identifier: Author's email address OR GitHub username
            since: Start date for commits
            branch: Branch name (default: main)
            
        Returns:
            List of commits by the author
        """
        # Format date for GitHub API
        since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # Get commit history from GitHub
        result = await self.github.call("get_commit_history", {
            "owner": repo_owner,
            "repo": repo_name,
            "sha": branch,
            "since": since_str,
            "per_page": 100
        })
        
        if isinstance(result, dict) and result.get("commits"):
            commits = result["commits"]
            
            # Filter by author email or username
            # author_identifier can be "john.doe@example.com" or "johndoe"
            target = author_identifier.lower()
            
            # Capture debug info to return to UI
            debug_log = []
            debug_log.append(f"🔍 Searching for commits by **'{target}'** in {len(commits)} candidate commits...")
            
            author_commits = []
            for commit in commits:
                c_email = commit.get("commit", {}).get("author", {}).get("email", "").lower()
                c_name = commit.get("commit", {}).get("author", {}).get("name", "").lower()
                
                # GitHub API sometimes returns author structure with login at top level
                c_login_obj = commit.get("author", {}) or {}
                c_login = c_login_obj.get("login", "").lower() if c_login_obj else ""
                
                match = target in c_email or target == c_login or target in c_name
                
                if match:
                    author_commits.append(commit)
                    debug_log.append(f"✅ MATCH: {c_login} | {c_email}")
                else:
                    debug_log.append(f"❌ SKIP: {c_login} | {c_email} | {c_name} (Did not match '{target}')")
            
            return author_commits, debug_log
        
        return [], ["No commits found in GitHub response"]
        
        return []
    
    def extract_jira_keys_from_message(
        self, 
        commit_message: str, 
        project_key: str
    ) -> List[str]:
        """
        Extract Jira ticket keys from commit message
        
        Examples:
            "PROJ-123: Fix bug" -> ["PROJ-123"]
            "[PROJ-456] Add feature" -> ["PROJ-456"]
            "Fix PROJ-123 and PROJ-456" -> ["PROJ-123", "PROJ-456"]
        
        Args:
            commit_message: Git commit message
            project_key: Jira project key to search for
            
        Returns:
            List of found Jira keys
        """
        # Pattern: PROJECT-NUMBER (case insensitive)
        pattern = rf'\b({project_key}-\d+)\b'
        matches = re.findall(pattern, commit_message, re.IGNORECASE)
        
        # Convert to uppercase and remove duplicates
        return list(set([m.upper() for m in matches]))
    
    async def validate_work(self, story_summary: str, story_description: str, commits: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Use Gemini to validate commits against story requirements"""
        if not self.model or not commits:
            return {"status": "Skipped", "reason": "No model or no commits"}
            
        print(f"DEBUG: Validating {len(commits)} commits against story...")
        
        commit_summaries = "\n".join([
            f"- {c.get('message', 'No message')}" 
            for c in commits[:10]  # Limit to 10 most recent for speed
        ])
        
        prompt = f"""You are a Technical Lead reviewing developer work.
        
        Jira Story: {story_summary}
        Description: {story_description}
        
        Developer's Commits:
        {commit_summaries}
        
        Task:
        1. Compare the developer's actions (commits) with the user story description.
        2. Validate if they are matching or if there's a discrepancy.
        3. Provide a brief summary of work done.
        4. State clearly if the requirements are being met.
        
        Output format:
        Matching: [Yes/No/Partial]
        Work Summary: [1-2 sentences]
        Confidence: [Percentage]
        Notes: [Any missing items or extra work]
        """
        
        try:
            response = self.model.generate_content(prompt)
            text = response.text.strip()
            
            # Simple parsing of structured output
            validation = {}
            for line in text.split("\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    validation[key.strip().lower().replace(" ", "_")] = val.strip()
            
            return validation
        except Exception as e:
            print(f"DEBUG: Validation error: {e}")
            return {"error": str(e), "status": "Failed"}

    async def track_story_commits(
        self,
        story_key: str,
        repo_owner: str,
        repo_name: str,
        branch: str = "main",
        author_identifier: str = None
    ) -> Dict[str, Any]:
        """
        Track commits related to a specific user story with validation
        """
        print(f"DEBUG: Tracking {story_key} in {repo_owner}/{repo_name}...")
        
        # Get story from Jira
        story = await self.get_issue_by_key(story_key)
        
        if not story:
            return {
                "error": f"Story {story_key} not found",
                "story_key": story_key
            }
        
        # Extract story details
        fields = story.get("fields", {})
        assignee = fields.get("assignee")
        status = fields.get("status", {}).get("name", "Unknown")
        created = fields.get("created", "")
        summary = fields.get("summary", "")
        description = fields.get("description", "")
        
        # If description is ADF (dict), try to extract text
        if isinstance(description, dict):
            # Very simple text extraction from ADF
            try:
                content = description.get("content", [])
                text_parts = []
                for node in content:
                    if node.get("type") == "paragraph":
                        for c in node.get("content", []):
                            if c.get("type") == "text":
                                text_parts.append(c.get("text", ""))
                description_text = "\n".join(text_parts)
            except:
                description_text = str(description)
        else:
            description_text = str(description)
        
        # Parse created date
        if created:
            created_date = datetime.fromisoformat(created.replace("Z", "+00:00")) - timedelta(days=365) # Huge buffer (1 year)
        else:
            created_date = datetime.now() - timedelta(days=365)
        
        analysis = {
            "story_key": story_key,
            "summary": summary,
            "status": status,
            "created_date": created_date.strftime("%Y-%m-%d %H:%M:%S"),
            "assignee": None,
            "commits": [],
            "comments": [],
            "comments_error": None,
            "commit_count": 0,
            "has_activity": False,
            "last_commit_date": None,
            "work_status": "Not Started",
            "validation": None
        }
        
        assignee_name = assignee.get("displayName", "Unknown") if assignee else "Unknown"
        assignee_email = assignee.get("emailAddress") if assignee else None
        
        analysis["assignee"] = {
            "name": assignee_name,
            "email": assignee_email,
            "identifier_used": author_identifier or assignee_email
        }
        
        # Step 5: Fetch Comments (DO THIS EARLY so early returns don't block it)
        print(f"DEBUG: Fetching comments for {story_key}...")
        comm_res = await self.get_comments(story_key)
        analysis["comments"] = comm_res.get("comments", [])
        analysis["comments_error"] = comm_res.get("error")

        target_identity = author_identifier or assignee_email

        if not target_identity:
            print(f"DEBUG: {story_key} assignee has no email/id, skipping commit tracking.")
            analysis["note"] = "Assignee has no email or identifier - cannot track commits"
            analysis["work_status"] = "No commits (email missing)"
            return analysis
        
        # Get commits by assignee since story creation
        print(f"DEBUG: Fetching commits by {target_identity} since {created_date}...")
        commits, debug_log = await self.get_commits_by_author(
            repo_owner,
            repo_name,
            author_identifier=target_identity,  # Explicitly pass as kwarg if needed, or positional
            since=created_date,
            branch=branch
        )
        
        analysis["debug_log"] = debug_log
        analysis["target_identity"] = target_identity
        
        # Filter commits that reference this story
        project_key = story_key.split("-")[0]
        related_commits = []
        
        # Logic:
        # 1. If the branch name contains the story key (e.g. "feature/KAN-185"), 
        #    we assume ALL commits on this branch by this author are relevant.
        # 2. If the branch is generic (e.g. "main", "dev"), 
        #    we ONLY accept commits that explicitly mention the story key in the message.
        
        branch_matches_story = story_key.lower() in branch.lower()
        
        for commit in commits:
            commit_msg = commit.get("commit", {}).get("message", "")
            referenced_keys = self.extract_jira_keys_from_message(commit_msg, project_key)
            
            # Check if this story is referenced explicitly OR if branch implies it
            is_explicit_match = story_key.upper() in [k.upper() for k in referenced_keys]
            
            if is_explicit_match or branch_matches_story:
                commit_info = {
                    "sha": commit.get("sha", "")[:7],
                    "full_sha": commit.get("sha", ""),
                    "message": commit_msg.split("\n")[0],  # First line only
                    "full_message": commit_msg,
                    "author": commit.get("commit", {}).get("author", {}).get("name", ""),
                    "date": commit.get("commit", {}).get("author", {}).get("date", ""),
                    "url": commit.get("html_url", ""),
                    "stats": commit.get("stats", {})
                }
                related_commits.append(commit_info)
        
        # Sort by date (newest first)
        related_commits.sort(
            key=lambda c: c.get("date", ""), 
            reverse=True
        )
        
        analysis["commits"] = related_commits
        analysis["commit_count"] = len(related_commits)
        analysis["has_activity"] = len(related_commits) > 0
        
        # Determine work status
        if len(related_commits) > 0:
            latest_commit = related_commits[0]
            analysis["last_commit_date"] = latest_commit["date"][:10]
            
            # Check how recent the last commit was
            if latest_commit.get("date"):
                last_commit_time = datetime.fromisoformat(
                    latest_commit["date"].replace("Z", "+00:00")
                )
                days_ago = (datetime.now(last_commit_time.tzinfo) - last_commit_time).days
                
                if days_ago <= 1:
                    analysis["work_status"] = "Active (worked today)"
                elif days_ago <= 3:
                    analysis["work_status"] = f"Active ({days_ago} days ago)"
                else:
                    analysis["work_status"] = f"Stale (last commit {days_ago} days ago)"
            
            # Perform AI validation
            print(f"DEBUG: Running AI validation for {story_key}...")
            analysis["validation"] = await self.validate_work(
                summary,
                description_text,
                related_commits
            )
        else:
            analysis["work_status"] = "Not Started (no commits)"
        
        print(f"DEBUG: Finished tracking {story_key}. Work status: {analysis['work_status']}")
        return analysis
    
    async def track_assignee_work(
        self,
        assignee_email: str,
        project_key: str,
        repo_owner: str,
        repo_name: str,
        days_back: int = 30
    ) -> Dict[str, Any]:
        """
        Track all work by a specific assignee
        
        Args:
            assignee_email: Developer's email
            project_key: Jira project key
            repo_owner: GitHub repo owner
            repo_name: GitHub repo name
            days_back: Number of days to look back
            
        Returns:
            Summary of assignee's work across all stories
        """
        # Get stories assigned to this person
        stories = await self.get_user_stories_by_project(
            project_key,
            assignee=assignee_email
        )
        
        # Get all commits by this person
        since = datetime.now() - timedelta(days=days_back)
        commits = await self.get_commits_by_author(
            repo_owner,
            repo_name,
            assignee_email,
            since
        )
        
        # Analyze each story
        story_analyses = []
        for story in stories:
            story_key = story.get("key")
            analysis = await self.track_story_commits(
                story_key,
                repo_owner,
                repo_name
            )
            story_analyses.append(analysis)
        
        # Calculate statistics
        total_stories = len(stories)
        stories_with_commits = sum(1 for a in story_analyses if a["has_activity"])
        stories_without_commits = total_stories - stories_with_commits
        total_commits = sum(a["commit_count"] for a in story_analyses)
        
        return {
            "assignee_email": assignee_email,
            "project_key": project_key,
            "period_days": days_back,
            "summary": {
                "total_stories_assigned": total_stories,
                "stories_with_activity": stories_with_commits,
                "stories_without_activity": stories_without_commits,
                "total_commits": total_commits,
                "activity_rate": (stories_with_commits / total_stories * 100) if total_stories > 0 else 0
            },
            "stories": story_analyses,
            "all_commits": len(commits)
        }
    
    async def track_project_progress(
        self,
        project_key: str,
        repo_owner: str,
        repo_name: str
    ) -> Dict[str, Any]:
        """
        Track progress of entire project by analyzing all stories
        
        Args:
            project_key: Jira project key
            repo_owner: GitHub repo owner
            repo_name: GitHub repo name
            
        Returns:
            Complete project tracking data
        """
        # Get all stories
        stories = await self.get_user_stories_by_project(project_key)
        
        # Analyze each story
        analyses = []
        for story in stories:
            story_key = story.get("key")
            analysis = await self.track_story_commits(
                story_key,
                repo_owner,
                repo_name
            )
            analyses.append(analysis)
        
        # Group by status
        by_status = {}
        for analysis in analyses:
            status = analysis["status"]
            if status not in by_status:
                by_status[status] = {
                    "count": 0,
                    "with_commits": 0,
                    "without_commits": 0
                }
            
            by_status[status]["count"] += 1
            if analysis["has_activity"]:
                by_status[status]["with_commits"] += 1
            else:
                by_status[status]["without_commits"] += 1
        
        # Group by assignee
        by_assignee = {}
        for analysis in analyses:
            assignee_data = analysis.get("assignee")
            if assignee_data:
                email = assignee_data.get("email", "Unassigned")
                if email not in by_assignee:
                    by_assignee[email] = {
                        "name": assignee_data.get("name", "Unknown"),
                        "stories": 0,
                        "commits": 0,
                        "active_stories": 0
                    }
                
                by_assignee[email]["stories"] += 1
                by_assignee[email]["commits"] += analysis["commit_count"]
                if analysis["has_activity"]:
                    by_assignee[email]["active_stories"] += 1
        
        # Calculate totals
        total_stories = len(analyses)
        total_commits = sum(a["commit_count"] for a in analyses)
        stories_with_activity = sum(1 for a in analyses if a["has_activity"])
        
        return {
            "project_key": project_key,
            "repository": f"{repo_owner}/{repo_name}",
            "summary": {
                "total_stories": total_stories,
                "stories_with_commits": stories_with_activity,
                "stories_without_commits": total_stories - stories_with_activity,
                "total_commits": total_commits,
                "activity_rate": (stories_with_activity / total_stories * 100) if total_stories > 0 else 0
            },
            "by_status": by_status,
            "by_assignee": by_assignee,
            "story_details": analyses
        }


# ==================================================
# Helper Functions for Integration
# ==================================================

def format_story_status(analysis: Dict[str, Any]) -> str:
    """
    Format story analysis into readable status string
    
    Args:
        analysis: Story analysis from track_story_commits()
        
    Returns:
        Formatted status string
    """
    lines = []
    lines.append(f"📋 {analysis['story_key']}: {analysis['summary']}")
    lines.append(f"   Status: {analysis['status']}")
    lines.append(f"   Assignee: {analysis.get('assignee', {}).get('name', 'Unassigned')}")
    lines.append(f"   Commits: {analysis['commit_count']}")
    lines.append(f"   Work Status: {analysis['work_status']}")
    
    if analysis['commits']:
        lines.append(f"   Last Commit: {analysis['last_commit_date']}")
        lines.append(f"   Latest: {analysis['commits'][0]['message']}")
    
    return "\n".join(lines)


def get_status_emoji(work_status: str) -> str:
    """Get emoji for work status"""
    if "Active" in work_status:
        return "🟢"
    elif "Stale" in work_status:
        return "🟡"
    else:
        return "🔴"


# ==================================================
# Usage Example
# ==================================================

'''async def example_usage(jira_client, github_client):
    """
    Example of how to use the tracker
    """
    # Initialize tracker
    tracker = JiraGitHubTracker(jira_client, github_client)
    
    # Track a single story
    story_analysis = await tracker.track_story_commits(
        story_key="PROJ-123",
        repo_owner="your-org",
        repo_name="your-repo"
    )
    print(format_story_status(story_analysis))
    
    # Track assignee's work
    assignee_work = await tracker.track_assignee_work(
        assignee_email="developer@example.com",
        project_key="PROJ",
        repo_owner="your-org",
        repo_name="your-repo",
        days_back=30
    )
    print(f"\nAssignee has {assignee_work['summary']['total_commits']} commits")
    print(f"Activity rate: {assignee_work['summary']['activity_rate']:.1f}%")
    
    # Track entire project
    project_data = await tracker.track_project_progress(
        project_key="PROJ",
        repo_owner="your-org",
        repo_name="your-repo"
    )
    print(f"\nProject: {project_data['summary']['total_stories']} stories")
    print(f"Activity: {project_data['summary']['stories_with_commits']} stories with commits")'''