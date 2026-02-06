import streamlit as st
import asyncio
import os
import json
from jira_github_tracker_backend import JiraGitHubTracker, format_story_status

# ==================================================
# Integration UI Module
# ==================================================

def run_integration_ui():
    """
    Render the Jira-GitHub Integration UI with auto-assignee detection
    """
    st.header("🔗 Jira-GitHub Story Tracker")
    
    st.markdown("""
    ### How It Works
    1. Enter a Jira story key (e.g., KAN-25)
    2. System automatically fetches the assignee from the story
    3. Finds the assignee's GitHub username and repository
    4. Analyzes commits and validates against story requirements
    """)
    
    # Check for Jira Client
    if "jira" not in st.session_state or not st.session_state.jira:
        st.warning("Please connect to Jira in the 'Create Stories' tab first.")
        return

    # 1. Input: Just the story key
    story_key = st.text_input(
        "Jira Story Key",
        value="KAN-25",
        help="Enter the Jira issue key (e.g., KAN-25, PROJ-123)"
    )
    
    # Configuration section (collapsible)
    with st.expander("⚙️ Advanced Configuration"):
        st.markdown("### GitHub Username Mapping")
        st.info("Configure how to map Jira users to GitHub usernames")
        
        mapping_method = st.radio(
            "Mapping method:",
            ["Email-based (extract from email)", "Manual mapping (use .env file)"],
            help="Email-based: john.doe@company.com → GitHub user 'john-doe'"
        )
        
        if mapping_method == "Manual mapping (use .env file)":
            st.code("""
# Add to your .env file:
GITHUB_USER_MAPPING={"john.doe@company.com": "johndoe", "jane@company.com": "janesmith"}
            """)
        
        st.markdown("### Repository Detection")
        col_owner, col_repo = st.columns(2)
        with col_owner:
            default_owner = st.text_input(
                "GitHub Owner",
                value="",
                help="Override the automatically detected owner/username"
            )
        with col_repo:
            default_name = st.text_input(
                "GitHub Repo Name",
                value="",
                help="Override the automatically detected repository name"
            )
        
        st.divider()
        st.markdown("### 🛠️ Advanced Diagnostics")
        if st.button("🔌 Test Jira Tool: get_issue_comments"):
            import jira_ui3
            from jira_ui3 import run_async
            with st.spinner(f"Testing tool for {story_key}..."):
                try:
                    raw_res = run_async(st.session_state.jira.call("get_issue_comments", {"issue_key": story_key}))
                    st.write("**Raw Server Response:**")
                    st.json(raw_res)
                except Exception as e:
                    st.error(f"Tool call failed: {e}")

        st.markdown("### 📜 Server Logs")
        if st.toggle("View Server Debug Log"):
            try:
                if os.path.exists("jira_server_debug.log"):
                    with open("jira_server_debug.log", "r", encoding="utf-8") as f:
                        log_data = f.read()
                        # Show last 2000 characters
                        st.text_area("Last 2000 chars of jira_server_debug.log", value=log_data[-2000:], height=300)
                        if st.button("🗑️ Clear Log"):
                            with open("jira_server_debug.log", "w") as f:
                                f.write("")
                            st.rerun()
                else:
                    st.info("Log file not found yet. It will be created when the server runs.")
            except Exception as e:
                st.error(f"Error reading log: {e}")
    
    # 2. Track Button
    if st.button("🔍 Track Story & Validate Commits"):
        if not story_key:
            st.error("Please enter a Jira story key")
            return
        
        if not st.session_state.jira:
            st.error("❌ Jira client is not initialized")
            return
        
        if not st.session_state.github:
            st.error("❌ GitHub client is missing. Please configure GITHUB_TOKEN in .env file")
            return

        with st.spinner(f"🔍 Analyzing {story_key}..."):
            import jira_ui3
            from jira_ui3 import run_async
            
            try:
                # Step 1: Pre-fetch story to get assignee and details
                # This makes the UI responsive and allows us to calculate repo params proactively
                st.write("📡 Fetching story details from Jira...")
                story_data = run_async(st.session_state.jira.call("get_issue", {"issue_key": story_key}))
                
                if isinstance(story_data, dict) and story_data.get("isError"):
                    st.error(f"❌ Failed to fetch story: {story_data.get('error')}")
                    return
                
                # Extract assignee
                fields = story_data.get("fields", {})
                assignee = fields.get("assignee")
                if not assignee:
                    st.warning(f"⚠️ Story {story_key} has no assignee. Cannot track commits.")
                    st.info("💡 Assign the story to a developer in Jira first.")
                    return
                
                assignee_email = assignee.get("emailAddress", "")
                st.write(f"Assignee Email: {assignee_email}")
                assignee_name = assignee.get("displayName", "Unknown")
                github_username = ""
                
                # Try to get authenticated user from session state if available
                auth_user = st.session_state.get("github_auth_user")
                if not auth_user and st.session_state.github:
                    auth_user = run_async(st.session_state.github.call("get_authenticated_user", {}))
                    if auth_user and not auth_user.get("error"):
                        st.session_state.github_auth_user = auth_user
                
                if mapping_method == "Email-based (extract from email)":
                    if assignee_email:
                        github_username = assignee_email.split("@")[0].replace(".", "-").lower()
                else:
                    mapping_str = os.getenv("GITHUB_USER_MAPPING", "{}")
                    try:
                        user_mapping = json.loads(mapping_str)
                        github_username = user_mapping.get(assignee_email, "")
                    except:
                        pass
                
                # Fallback to authenticated user if mapping failed or returned empty
                if not github_username and auth_user:
                    github_username = auth_user.get("username", "")
                
                # Step 3: Detect Repository (SMART discovery)
                repo_owner = default_owner if default_owner else (github_username if github_username else "unknown")
                
                # Fetch repositories if not in session
                repos = st.session_state.get("github_repos", [])
                if not repos and st.session_state.github:
                    repo_payload = run_async(st.session_state.github.call("list_repositories", {}))
                    if repo_payload and not repo_payload.get("error"):
                        repos = repo_payload.get("repositories", [])
                        st.session_state.github_repos = repos
                
                # Intelligent Guessing
                project_key = story_key.split("-")[0].lower()
                guessed_repo = ""
                
                # 1. Look for exact match or 'project-app'
                for r in repos:
                    r_name = r["name"].lower()
                    if r_name == project_key or r_name == f"{project_key}-app" or r_name == f"jira-{project_key}":
                        guessed_repo = r["name"]
                        break
                
                # 2. Look for any repo containing the project key
                if not guessed_repo:
                    for r in repos:
                        if project_key in r["name"].lower():
                            guessed_repo = r["name"]
                            break
                
                repo_name = default_name if default_name else (guessed_repo if guessed_repo else f"{project_key}-app")
                
                if repo_owner == "unknown":
                    st.warning("⚠️ Could not detect GitHub owner. Using 'unknown' - please verify your GITHUB_TOKEN.")
                
                st.success(f"✅ Ready! Tracking **{repo_owner}/{repo_name}**")
                
                # Step 4: Perform Analysis (Once)
                gemini_model = getattr(jira_ui3, 'model', None)
                tracker = JiraGitHubTracker(
                    jira_client=st.session_state.jira,
                    github_client=st.session_state.github,
                    gemini_model=gemini_model
                )
                
                st.write("🤖 Running AI Work Validation...")
                analysis = run_async(
                    tracker.track_story_commits(
                        story_key=story_key,
                        repo_owner=repo_owner,
                        repo_name=repo_name
                    )
                )
                
                # Step 5: Display results
                if analysis.get("error"):
                    st.error(f"❌ {analysis['error']}")
                else:
                    st.divider()
                    st.subheader(f"📊 Analysis Result: {story_key}")
                    
                    # Metrics
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Commits", analysis.get('commit_count', 0))
                    m2.metric("Comments", len(analysis.get('comments', [])))
                    m3.metric("Work Status", analysis.get('work_status', 'Unknown'))
                    m4.metric("Assignee", assignee_name)
                    
                    # Validation Results (Gemini)
                    if analysis.get('validation'):
                        st.markdown("### ✅ AI Validation Report")
                        val = analysis['validation']
                        
                        v_col1, v_col2 = st.columns([1, 4])
                        with v_col1:
                             matching = val.get("matching", "Unknown").strip()
                             color = "green" if "Yes" in matching else "orange" if "Partial" in matching else "red"
                             st.markdown(f"<h4 style='color:{color};'>{matching}</h4>", unsafe_allow_html=True)
                             st.caption("Matches Story?")
                        
                        with v_col2:
                             st.write(f"**Summary:** {val.get('work_summary', 'N/A')}")
                             if val.get('confidence'):
                                  st.write(f"**Confidence:** {val['confidence']}")
                        
                        if val.get('notes'):
                             st.warning(f"**Notes:** {val['notes']}")
                    else:
                        st.markdown("### 🚫 AI Validation Skipped")
                        st.info("AI Validation requires commits to compare against acceptance criteria. No commits were detected regarding this story yet.")
                    
                    # Commits List
                    if analysis.get('commits'):
                        with st.expander(f"📝 View Raw Commits ({len(analysis['commits'])})"):
                            for commit in analysis['commits']:
                                st.markdown(f"**{commit['date'][:10]}** | `{commit['sha'][:7]}`: {commit['message']}")
                    
                    # Jira Comments List (Lower section as requested)
                    st.divider()
                    st.subheader("💬 Jira Activity & Comments")
                    if analysis.get('comments'):
                        with st.expander(f"View Jira Comments ({len(analysis['comments'])})", expanded=True):
                            for comment in analysis['comments']:
                                author = comment.get('author', 'Unknown')
                                date = comment.get('created', '').replace('T', ' ')[:16]
                                body = comment.get('body', '')
                                st.markdown(f"**{author}** ({date}):")
                                st.info(body)
                    elif analysis.get('comments_error'):
                        st.error(f"❌ Failed to fetch comments: {analysis['comments_error']}")
                        st.info("💡 **Possible cause:** Your Jira API Token might lack 'Browse Projects' or 'View Comments' permissions for this project.")
                    else:
                        st.info("ℹ️ No comments found in Jira for this story.")
                
            except Exception as e:
                st.error(f"Error during tracking: {e}")
                import traceback
                st.code(traceback.format_exc())

if __name__ == "__main__":
    st.warning("Please run 'jira_ui3.py' to use this module.")


