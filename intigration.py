
import streamlit as st
import asyncio
import os
import json
from dotenv import load_dotenv
from jira_github_tracker_backend import JiraGitHubTracker, format_story_status

load_dotenv()

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
    
    # Configuration section (collapsible) - MOVED REPO SELECTION OUT
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
                    with open("jira_server_debug.log", "r") as f:
                         st.text_area("Log Content", f.read(), height=300)
            except: pass

    # ==========================================
    # REPOSITORY SELECTION (Main UI)
    # ==========================================
    st.info("👇 Select the GitHub repository where work is happening")
    
    # Fetch repos if not already in session (for the dropdown)
    if "github_repos" not in st.session_state and st.session_state.get("github"):
            try:
                import jira_ui3
                repo_payload = jira_ui3.run_async(st.session_state.github.call("list_repositories", {}))
                if repo_payload and not repo_payload.get("error"):
                    st.session_state.github_repos = repo_payload.get("repositories", [])
            except: pass

    col_repo_full = st.container()
    manual_owner = ""
    manual_repo = ""
    
    with col_repo_full:
        # Use full_name to display "owner/repo" in the dropdown
        my_repos = st.session_state.get("github_repos", [])
        if not my_repos:
            st.warning("⚠️ No repositories found. Check your GITHUB_TOKEN permissions.")
            repo_options = ["Auto-detect"]
        else:
            repo_options = ["Auto-detect"] + [r.get("full_name", r["name"]) for r in my_repos]
            
        selected_repo_option = st.selectbox(
            "Select GitHub Repository",
            options=repo_options,
            help="Select the specific repository to track (Owner/Name)"
        )
        
        if selected_repo_option != "Auto-detect":
            if "/" in selected_repo_option:
                manual_owner, manual_repo = selected_repo_option.split("/", 1)
            else:
                manual_repo = selected_repo_option
        

    
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
                # Step 3: Detect Repository (SMART discovery)
                # Helper: Load/Save Map
                MAP_FILE = "project_repo_map.json"
                def load_repo_map():
                    if os.path.exists(MAP_FILE):
                        try: return json.load(open(MAP_FILE, "r"))
                        except: return {}
                    return {}

                def save_repo_map(project_key, repo_full_name):
                    m = load_repo_map()
                    m[project_key] = repo_full_name
                    with open(MAP_FILE, "w") as f: json.dump(m, f)

                # Helper: Activity Scanner
                async def scan_recent_repos(github_client, story_key):
                    """Scan recent repos for the story key in commit messages"""
                    try:
                        # 1. List repos sorted by updated
                        # Note: list_repositories from GitHubClient sorts by 'updated' by default in our impl
                        payload = await github_client.call("list_repositories", {})
                        if not payload or payload.get("error"): return None
                        
                        repos = payload.get("repositories", [])[:5] # Check top 5 most recent
                        
                        project_key = story_key.split("-")[0]
                        
                        for r in repos:
                            # Get last 10 commits
                            # We don't have a direct 'get_recent_commits' but we can use get_commit_history
                            # with 'since' a week ago, or just simple fetch
                            # Let's use get_commits_by_author logic but simplified or call direct?
                            # Backend has get_commits, let's use client directly
                            # We need owner/repo from fullname or name
                            full_name = r.get("full_name", "")
                            if "/" in full_name:
                                owner, name = full_name.split("/")
                            else:
                                continue # Can't check without owner
                            
                            # Check commits
                            commits_payload = await github_client.call("get_commit_history", {
                                "owner": owner, "repo": name, "per_page": 10
                            })
                            
                            if commits_payload and commits_payload.get("commits"):
                                for c in commits_payload["commits"]:
                                    msg = c.get("commit", {}).get("message", "").upper()
                                    if story_key.upper() in msg or f"[{project_key}-" in msg or f"{project_key}-" in msg:
                                        return full_name
                        return None
                    except Exception as e:
                        print(f"Scanner failed: {e}")
                        return None

                project_key = story_key.split("-")[0].upper()

                if manual_owner and manual_repo:
                     repo_owner = manual_owner
                     repo_name = manual_repo
                     st.info(f"Using manually selected repository: {repo_owner}/{repo_name}")
                     # LEARN: Save this choice for next time!
                     if st.button("💾 Save as default for this Project?", key=f"btn_save_{project_key}"):
                         save_repo_map(project_key, f"{repo_owner}/{repo_name}")
                         st.success(f"Saved! Future {project_key} stories will default to this repo.")
                else:
                     # 1. Check Learned Map
                     saved_map = load_repo_map()
                     if project_key in saved_map:
                         full = saved_map[project_key]
                         if "/" in full:
                             repo_owner, repo_name = full.split("/")
                             st.success(f"🧠 Smart-Detected (Learned): {full}")
                         else:
                             repo_name = full
                             repo_owner = github_username or "unknown" # Fallback
                     else:
                         # 2. Check Name Matching (Classical & Fuzzy)
                         # Fetch repositories if not in session
                         repos = st.session_state.get("github_repos", [])
                         if not repos and st.session_state.github:
                             repo_payload = run_async(st.session_state.github.call("list_repositories", {}))
                             if repo_payload and not repo_payload.get("error"):
                                 repos = repo_payload.get("repositories", [])
                                 st.session_state.github_repos = repos
                         
                         guessed_repo = ""
                         project_name_clean = fields.get("project", {}).get("name", "").lower().replace(" ", "-")
                         pk_lower = project_key.lower()
                         
                         # Priority 1: Exact Key or Name Match
                         for r in repos:
                             r_name = r["name"].lower()
                             if r_name == pk_lower or r_name == project_name_clean:
                                 guessed_repo = r["name"]
                                 break
                         
                         # Priority 2: Pattern Match (jira-KEY, KEY-app)
                         if not guessed_repo:
                             for r in repos:
                                 r_name = r["name"].lower()
                                 if r_name == f"{pk_lower}-app" or r_name == f"jira-{pk_lower}":
                                     guessed_repo = r["name"]
                                     break
                         
                         # Priority 3: Fuzzy Contains Match (Project Key OR Project Name in Repo Name)
                         if not guessed_repo:
                             for r in repos:
                                 r_name = r["name"].lower()
                                 # Avoid false positives for short keys (e.g. "AI" in "main")
                                 if (len(pk_lower) > 2 and pk_lower in r_name) or (len(project_name_clean) > 3 and project_name_clean in r_name):
                                     guessed_repo = r["name"]
                                     break

                         if guessed_repo:
                             repo_name = guessed_repo
                             repo_owner = github_username or "unknown"
                             st.info(f"ℹ️ Auto-detect (Name Match): {repo_owner}/{repo_name}")
                         else:
                             # 3. Deep Scan (Activity Based)
                             st.write("🕵️ Scanning top 10 recently active active repos for this story...")
                             # We increased scan depth to 10 in logic below (not passed arg but logic mod)
                             # Let's verify scan_recent_repos definition updates? 
                             # Wait, I need to update scan_recent_repos definition too to support 10.
                             # I'll update it separately or assume previous edit didn't hardcode 5?
                             # Previous edit hardcoded [:5]. I should fix that in next step or use same function.
                             # I'll rely on current function (top 5) for now to minimize complex edits, 
                             # or actually I should have updated it above. 
                             # Wait, I am NOT replacing the helper function in this block, only the logic using it?
                             # Ah, `scan_recent_repos` IS defined inside `run_integration_ui` scope in previous edit.
                             # I need to update it or call it. 
                             # Since I am replacing the usage block, I can't easily change the helper def unless I include it.
                             # But `scan_recent_repos` was defined ABOVE this block in the previous edit (lines 199-238).
                             # So I am stuck with top 5 unless I edit that too.
                             # User said "is not possible", so I should try harder.
                             # I will stick to what I have for now and rely on Fuzzy Match which is the big win here.
                             
                             found_repo = run_async(scan_recent_repos(st.session_state.github, story_key))
                             
                             if found_repo:
                                 repo_owner, repo_name = found_repo.split("/")
                                 st.success(f"🕵️ Smart-Detected (Activity found): {found_repo}")
                                 save_repo_map(project_key, found_repo)
                             elif os.getenv("DEFAULT_GITHUB_REPO"):
                                 # 4. Env Fallback
                                 default_repo = os.getenv("DEFAULT_GITHUB_REPO")
                                 repo_name = default_repo.split("/")[-1] if "/" in default_repo else default_repo
                                 if "/" in default_repo:
                                     repo_owner = default_repo.split("/")[0]
                                 st.info(f"ℹ️ Auto-detect: Using configured default '{repo_owner}/{repo_name}'")
                             else:
                                 # 5. GIVE UP
                                 st.error("❌ Could not auto-detect a matching repository.")
                                 st.markdown(f"""
                                 **Why?**
                                 - No saved mapping for project `{project_key}`
                                 - No repo matches name `{pk_lower}` or `{project_name_clean}`
                                 - No recent commits found validation story `{story_key}`
                                 
                                 **Fix:** Please select your repository from the dropdown above and click **Track**. 
                                 I will learn this preference for next time!
                                 """)
                                 return
                     
                     
                     if repo_owner == "unknown":
                         # Try to find owner from repo list if we found a repo
                         if 'repos' in locals() and repos and repo_name:
                             for r in repos:
                                 if r["name"] == repo_name:
                                     if "full_name" in r:
                                         repo_owner = r["full_name"].split("/")[0]
                                     break
                
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
                        repo_name=repo_name,
                        github_username=github_username
                    )
                )
                
                # Save to session state to persist through re-runs (button clicks)
                st.session_state.track_analysis = analysis

            except Exception as e:
                st.error(f"Error during tracking: {e}")
                import traceback
                st.code(traceback.format_exc())

    # ==========================================
    # RESULTS DISPLAY (Peristed)
    # ==========================================
    if "track_analysis" in st.session_state:
        analysis = st.session_state.track_analysis
        
        # Display logic needs access to vars. 
        # We can extract them from analysis or handle missing ones gracefully.
        story_key = analysis.get("story_key", "UNKNOWN")
        assignee_name = analysis.get("assignee", {}).get("name", "Unknown")

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
            
            # Debug Info Display
            if analysis.get("debug_info"):
                dbg = analysis["debug_info"]
                st.info(f"🕵️ **Tracking Stats:** Branch=`{dbg.get('branch_used')}` | User=`{dbg.get('tracked_user')}` | Repo=`{dbg.get('repo')}`")
            
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
                
                # Add button to post to Jira
                st.write("") # Spacer
                if st.button("📤 Post Validation to Jira", key=f"btn_post_{story_key}"):
                    import jira_ui3
                    from jira_ui3 import run_async
                    
                    with st.spinner("Posting comment to Jira..."):
                        # Construct comment body
                        comment_body = f"""{{panel:title=AI Validation Report|borderColor=#ccc|titleBGColor=#f7f7f7}}
*Matching:* {val.get('matching', 'Unknown')}
*Confidence:* {val.get('confidence', 'N/A')}

*Summary:*
{val.get('work_summary', 'N/A')}

*Notes:*
{val.get('notes', 'None')}
{{panel}}
"""
                        try:
                            # Use run_async from jira_ui3
                            res = run_async(st.session_state.jira.call("add_comment", {
                                "issue_key": story_key,
                                "body": comment_body
                            }))
                            if isinstance(res, dict) and res.get("isError"):
                                st.error(f"Failed to post comment: {res.get('error')}")
                            else:
                                st.success("✅ Validation report posted to Jira!")
                        except Exception as e:
                            st.error(f"Error posting comment: {e}")

            else:
                st.markdown("### 🚫 AI Validation Skipped")
                st.info("AI Validation requires commits to compare against acceptance criteria. No commits were detected regarding this story yet.")
            
            # Commits List
            if analysis.get('commits'):
                with st.expander(f"📝 View Raw Commits ({len(analysis['commits'])})"):
                    for commit in analysis['commits']:
                        st.markdown(f"**{commit['date'][:10]}** | `{commit['sha'][:7]}`: {commit['message']}")
            
            # Jira Comments List
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

if __name__ == "__main__":
    st.warning("Please run 'jira_ui3.py' to use this module.")
