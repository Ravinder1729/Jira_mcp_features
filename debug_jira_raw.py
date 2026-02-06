import os
import sys
from dotenv import load_dotenv

# Ensure the current directory is in python path to find jira_mcp_server
sys.path.append(os.getcwd())

try:
    from jira_mcp_server import list_projects, JIRA_BASE, JIRA_EMAIL, JIRA_API_TOKEN
except ImportError as e:
    print(f"Import Error: {e}")
    sys.exit(1)

print(f"Checking connection to: {JIRA_BASE}")
print(f"User: {JIRA_EMAIL}")
# obscure token
masked = JIRA_API_TOKEN[:4] + "*" * 5 if JIRA_API_TOKEN else "None"
print(f"Token: {masked}")

load_dotenv()

try:
    print("Attempting to list projects...")
    # list_projects in the server file is synchronous and returns a dict/list
    result = list_projects()
    print("RAW RESULT:", result)
except Exception as e:
    print("EXECUTION ERROR:", e)
