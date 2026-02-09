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
    
    # Configuration: Hardcoded defaults for zero-config UI
    mapping_method = "Email-based (extract from email)"
    default_owner = ""
    default_name = ""
    manual_branch = ""
    manual_author = ""
    
    st.info("💡 System will automatically detect Repository, Branch, and Commit Author.")

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
                
                if not assignee_email:
                    # Don't warn yet, wait to see if we can fall back to auth user
                    pass

                # Try to get authenticated user from session state if available
                auth_user = st.session_state.get("github_auth_user")
                if not auth_user and st.session_state.github:
                    auth_user = run_async(st.session_state.github.call("get_authenticated_user", {}))
                    if auth_user and not auth_user.get("error"):
                        st.session_state.github_auth_user = auth_user
                
                # Determine GitHub identifier
                github_identifier = assignee_email
                
                if mapping_method == "Email-based (extract from email)":
                    if assignee_email:
                        github_identifier = assignee_email.split("@")[0].replace(".", "-").lower()
                else:
                    mapping_str = os.getenv("GITHUB_USER_MAPPING", "{}")
                    try:
                        user_mapping = json.loads(mapping_str)
                        if assignee_email in user_mapping:
                             github_identifier = user_mapping.get(assignee_email, "")
                    except:
                        pass
                
                # Fallback to authenticated user if mapping failed or returned empty
                # BUT ONLY if we don't have a valid identifier yet
                if not github_identifier and auth_user:
                    github_identifier = auth_user.get("username", "")
                    if not assignee_email:
                         st.info(f"ℹ️ Jira email missing. Tracking by GitHub user: **{github_identifier}**")
                
                # FINAL FALLBACK: Use Assignee Name (e.g. "Ravinder")
                if not github_identifier and assignee_name:
                    github_identifier = assignee_name
                    st.info(f"ℹ️ Tracking by Assignee Name: **{github_identifier}**")
                
                if not github_identifier and not manual_author:
                     st.error("❌ Could not determine any Commit Author (No Email, No GitHub User, No Name).")
                     st.stop()
                
                # Step 3: Detect Repository (SMART discovery)
                repo_owner = default_owner if default_owner else (github_identifier if github_identifier else "unknown")
                
                # Determine Final Author to Track
                # Priority: Manual Override > Jira Assignee > Auth User
                final_author = manual_author if manual_author else github_identifier
                
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
                guessed_owner = ""
                final_branch = "main" # Default
                
                # STRATEGY 1: Smart Scan - Check recently active repos for a branch matching the Story Key
                # Sort repos by updated_at (newest first)
                if repos:
                    sorted_repos = sorted(repos, key=lambda x: x.get("updated_at", ""), reverse=True)
                    
                    st.write("🕵️ Scanning recently active repositories for branch match...")
                    
                    # Check top 5 most recent repos
                    for r in sorted_repos[:5]:
                        r_name = r["name"]
                        r_owner = r["full_name"].split("/")[0] if "full_name" in r else default_owner
                        
                        try:
                            # Check branches for this repo
                            branches_payload = run_async(st.session_state.github.call("list_branches", {"owner": r_owner, "repo": r_name}))
                            if branches_payload and not branches_payload.get("error"):
                                branches = branches_payload.get("branches", [])
                                
                                # Check if story key is in any branch name
                                for b in branches:
                                    if story_key.lower() in b.lower():
                                        guessed_repo = r_name
                                        guessed_owner = r_owner
                                        final_branch = b # Found the branch too!
                                        st.success(f"✅ Auto-detected work in **{r_name}** on branch **{b}**")
                                        break
                        except:
                            continue
                        
                        if guessed_repo:
                            break
                
                # STRATEGY 2: Name Matching (Fallback)
                if not guessed_repo:
                    # 1. Look for exact match or 'project-app'
                    for r in repos:
                        r_name = r["name"].lower()
                        if r_name == project_key or r_name == f"{project_key}-app" or r_name == f"jira-{project_key}":
                            guessed_repo = r["name"]
                            if "full_name" in r:
                                 guessed_owner = r["full_name"].split("/")[0]
                            break
                    
                    # 2. Look for any repo containing the project key
                    if not guessed_repo:
                        for r in repos:
                            if project_key in r["name"].lower():
                                guessed_repo = r["name"]
                                if "full_name" in r:
                                     guessed_owner = r["full_name"].split("/")[0]
                                break
                
                repo_name = default_name if default_name else (guessed_repo if guessed_repo else f"{project_key}-app")
                
                # If we guessed a repo, prioritize its owner over our 'github_identifier' guess
                # This is critical if github_identifier is a Display Name (e.g. "John Doe")
                if not default_owner and guessed_owner:
                     repo_owner = guessed_owner
                else:
                     repo_owner = default_owner if default_owner else (github_identifier if github_identifier else "unknown")
                
                # Step 3.5: Auto-detect Branch (If not already found by Smart Scan)
                if final_branch == "main" and not manual_branch and st.session_state.github:
                     # Only run if we didn't find it in Strategy 1
                     pass # Logic continues below to refine branch if needed
                
                if manual_branch:
                    final_branch = manual_branch
                    st.info(f"Using manual branch: **{final_branch}**")
                elif st.session_state.github:
                    st.write("🌿 Auto-detecting branch...")
                    try:
                        branches_payload = run_async(st.session_state.github.call("list_branches", {"owner": repo_owner, "repo": repo_name}))
                        if branches_payload and not branches_payload.get("error"):
                            all_branches = branches_payload.get("branches", [])
                            
                            # Find best match
                            detected = None
                            # 1. Exact match (case insensitive)
                            for b in all_branches:
                                if b.lower() == story_key.lower():
                                    detected = b
                                    break
                            
                            # 2. Contains match
                            if not detected:
                                for b in all_branches:
                                    if story_key.lower() in b.lower():
                                        detected = b
                                        break
                            
                            if detected:
                                final_branch = detected
                                st.success(f"✅ Found feature branch: **{final_branch}**")
                            else:
                                st.info(f"ℹ️ No specific branch found for {story_key}. Tracking **main**.")
                    except Exception as e:
                        st.warning(f"Branch detection failed: {e}. Defaulting to main.")

                st.success(f"✅ Ready! Tracking **{repo_owner}/{repo_name}** on branch **{final_branch}** for **{final_author}**")
                
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
                        branch=final_branch,
                        author_identifier=final_author
                    )
                )
                
                # Store result in session state to persist across reruns (e.g. Post to Jira click)
                st.session_state.analysis_result = analysis

            except Exception as e:
                st.error(f"An error occurred during analysis: {e}")

    # Step 5: Display results (Outside button block)
    if st.session_state.get("analysis_result"):
        analysis = st.session_state.analysis_result
        story_key = analysis.get("story_key") # Retrieve key from analysis
        
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
            assignee_display = analysis.get('assignee', {}).get('name', 'Unknown')
            m4.metric("Assignee", assignee_display)
            
            # Debug Information (Visible if 0 commits or just for transparency)
            with st.expander("🕵️ Debug Info & Commit Log"):
                    st.write(f"**Target Author:** `{analysis.get('target_identity', 'Unknown')}`")
                    st.write(f"**Created Date:** `{analysis.get('created_date', 'Unknown')}`")
                    
                    debug_logs = analysis.get("debug_log", [])
                    if debug_logs:
                        st.code("\n".join(debug_logs), language="text")
                    else:
                        st.info("No debug logs available.")
            
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
                
                # --- POST TO JIRA BUTTON ---
                if st.button("📝 Post Validation to Jira"):
                    with st.spinner("Posting comment to Jira..."):
                        # Format the comment body
                        # Using standard text formatting instead of Jira Wiki Markup specific headers
                        comment_body = (
                            f"*AI Work Validation Report*\n\n"
                            f"*Status:* {val.get('matching', 'Unknown')}\n"
                            f"*Confidence:* {val.get('confidence', 'N/A')}\n\n"
                            f"*Summary:*\n{val.get('work_summary', 'N/A')}\n\n"
                        )
                        if val.get('notes'):
                            comment_body += f"*Notes:*\n{val['notes']}\n"
                        
                        comment_body += "\n_(Posted automatically by Jira MCP)_"

                        try:
                            # Call the add_comment tool
                            import jira_ui3
                            from jira_ui3 import run_async
                            
                            res = run_async(st.session_state.jira.call("add_comment", {
                                "issue_key": story_key, 
                                "body": comment_body
                            }))
                            
                            # DEBUG: Show raw response
                            # st.write(f"🔍 DEBUG: API Response: {res}")
                            
                            if isinstance(res, dict) and res.get("isError"):
                                    st.error(f"Failed to post comment: {res.get('error')}")
                            else:
                                    st.success("✅ Validation Report posted to Jira successfully!")
                                    st.balloons() # Added balloons for better feedback
                        except Exception as e:
                            st.error(f"Error posting to Jira: {e}")
                # ---------------------------
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


