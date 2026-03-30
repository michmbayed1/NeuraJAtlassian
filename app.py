from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import requests
from requests.auth import HTTPBasicAuth
import logging
import base64
import json
from datetime import datetime

# Initialize the main web application 'engine' and tell it where to find the website files.
app = Flask(__name__, template_folder='.')
# This allows the app to communicate with different web browsers safely during development.
CORS(app) 

# Set up a 'diary' (logging) to record what the app is doing or if any errors happen.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 1. HOME ROUTE ---
# This defines what happens when you first open the app's address in a browser.
@app.route('/')
def home():
    # Show the main visual interface (index.html) to the user.
    return render_template('index.html')


# --- UTILITY: GET CLOUD ID ---
# A helper function to find the hidden 'ID number' unique to your specific Jira site.
def get_cloud_id(site_url, email, token):
    try:
        # Ask Atlassian's servers for the tenant info using your login details.
        res = requests.get(f"{site_url}/_edge/tenant_info", auth=HTTPBasicAuth(email, token), timeout=10)
        if res.status_code == 200:
            # If successful, extract and return the Cloud ID.
            return res.json().get('cloudId')
        else:
            # If it fails, write the error code in the 'diary'.
            logger.error(f"Tenant Info Failed: {res.status_code}")
            return None
    except Exception as e:
        # If the internet connection fails, record the specific error.
        logger.error(f"CloudID Fetch Error: {e}")
    return None

# --- ROUTE 1: MAIN DASHBOARD (FAST LOAD) ---
# This is the 'brain' of the app that performs the heavy-duty Jira analysis.
@app.route('/api/jira/analyze', methods=['POST'])
def analyze_jira():
    # Grab the URL, Email, and Token that the user typed into the website.
    data = request.json
    site_url = data.get('url', '').rstrip('/')
    domain = site_url.split("//")[-1]
    email = data.get('email')
    token = data.get('token')

    # Ensure no fields were left blank before starting.
    if not all([site_url, email, token]):
        return jsonify({"status": "error", "message": "Missing credentials"}), 400

    # Create a 'session' to keep the connection open and authenticated.
    session = requests.Session()
    session.auth = HTTPBasicAuth(email, token)
    
    try:
        # --- CRITICAL FIX: THE GATEKEEPER CHECK ---
        # Before doing any work, double-check that the password/token actually works.
        verify_res = session.get(f"{site_url}/rest/api/3/myself", timeout=10)
        
        # If the password is wrong, tell the user immediately.
        if verify_res.status_code == 401:
            return jsonify({"status": "error", "message": "Invalid API Token or Email"}), 401
        elif verify_res.status_code != 200:
            return jsonify({"status": "error", "message": "Could not connect to Jira"}), verify_res.status_code
        # ------------------------------------------

        # --- 1. ADD-ONS (PLUGINS) ---
        # Fetch the Cloud ID to look up installed apps/plugins.
        cloud_id = get_cloud_id(site_url, email, token)
        installed_apps = []
        
        if cloud_id:
            # Connect to a special Atlassian database (GraphQL) to see installed tools.
            graphql_url = f"https://{domain}/gateway/api/graphql"
            app_query = {
                "query": """
                query getInstalledApps($cloudId: ID!) {
                  ecosystem {
                    appInstallationsByContext(
                      filter: { appInstallations: { contexts: [$cloudId] } }
                    ) {
                      nodes {
                        app {
                          name
                          vendorName
                          id
                        }
                        license {
                          active
                          type
                        }
                      }
                    }
                  }
                }
                """,
                "variables": {"cloudId": f"ari:cloud:jira::site/{cloud_id}"}
            }
            
            gql_res = session.post(graphql_url, json=app_query, timeout=20)
            
            if gql_res.status_code == 200:
                gql_data = gql_res.json()
                nodes = gql_data.get('data', {}).get('ecosystem', {}).get('appInstallationsByContext', {}).get('nodes', [])
                
                # Loop through every app found and organize its details.
                for node in nodes:
                    app_info = node.get('app', {})
                    vendor = app_info.get('vendorName', '')
                    lic_info = node.get('license') or {}
                    
                    # Determine if it's a paid app (COMMERCIAL) or a built-in one (SYSTEM).
                    version = lic_info.get('type')
                    if not version:
                        version = "COMMERCIAL" if vendor else "SYSTEM"
                    
                    installed_apps.append({
                        "name": app_info.get('name'),
                        "key": app_info.get('id'),
                        "vendor": vendor if vendor else "Atlassian",
                        "status": "Active" if lic_info.get('active') else "Installed",
                        "version": version
                    })

        # --- 2. WORKFLOWS ---
        # Ask Jira for all the processes (workflows) currently configured.
        session.headers.update({"Accept": "application/json"})
        wf_res = session.get(f"{site_url}/rest/api/3/workflow/search", params={"expand": "statuses,transitions", "maxResults": 1000})
        workflow_data = []
        if wf_res.status_code == 200:
            for wf in wf_res.json().get('values', []):
                steps = len(wf.get('statuses', []))
                trans = len(wf.get('transitions', []))
                # If a workflow has too many steps/moves, label it as complex.
                workflow_data.append({
                    "name": wf.get('id', {}).get('name') or wf.get('name'), 
                    "steps": steps, 
                    "transitions": trans, 
                    "intelligence": "High Complexity" if (steps > 10 or trans > 15) else "Optimized"
                })

        # --- 3. FIELDS ---
        # Get a list of all data fields (like 'Due Date' or 'Priority').
        field_res = session.get(f"{site_url}/rest/api/3/field")
        # Separate out only the 'Custom' fields created by users.
        custom_fields = [f for f in (field_res.json() if field_res.status_code == 200 else []) if f.get('custom')]
        field_analysis = []
        orphaned_count = 0
        for f in custom_fields:
            f_id = f.get('id')
            # Check how many screens actually show this field.
            s_res = session.get(f"{site_url}/rest/api/3/field/{f_id}/screens")
            screens = s_res.json().get('total', 0) if s_res.status_code == 200 else 0
            # If it's not on any screen, it's 'orphaned' (unused junk).
            if screens == 0: orphaned_count += 1
            field_analysis.append({
                "name": f.get('name'), 
                "id": f_id, 
                "priority": "CRITICAL" if screens == 0 else "Low", 
                "screens": screens, 
                "recommendation": "Active" if screens > 0 else "Inactive"
            })

        # --- 4. PROJECTS ---
        # Fetch all projects and look at how many tickets (issues) they contain.
        proj_res = session.get(f"{site_url}/rest/api/3/project/search", params={"expand": "insight", "maxResults": 1000})
        project_list = []
        total_issues = 0
        if proj_res.status_code == 200:
            for p in proj_res.json().get('values', []):
                insight = p.get('insight', {})
                count = insight.get('totalIssueCount', 0)
                total_issues += count
                project_list.append({
                    "name": p.get('name'), 
                    "key": p.get('key'), 
                    "issue_count": count, 
                    "last_updated": insight.get('lastIssueUpdateTime', 'N/A')[:10]
                })

        # --- 5. USERS ---
        # Count all the active human users currently in your Jira instance.
        user_count = 0
        start_at = 0
        page_size = 50
        while True:
            u_res = session.get(f"{site_url}/rest/api/3/users/search", params={"startAt": start_at, "maxResults": page_size})
            if u_res.status_code != 200: break
            users = u_res.json()
            if not users: break
            active_batch = [u for u in users if u.get('accountType') == 'atlassian' and u.get('active')]
            user_count += len(active_batch)
            if len(users) < page_size: break
            start_at += page_size

        # --- 6. AUTOMATION SUMMARY COUNT ---
        # Find out how many total 'robot' automation rules are running.
        automation_total = 0
        if cloud_id:
            auth_str = f"{email}:{token}"
            encoded_auth = base64.b64encode(auth_str.encode()).decode()
            auto_headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Basic {encoded_auth}"
            }
            sum_res = requests.post(
                f"https://api.atlassian.com/automation/public/jira/{cloud_id}/rest/v1/rule/summary",
                headers=auto_headers,
                json={"limit": 1},
                timeout=10
            )
            if sum_res.status_code == 200:
                automation_total = sum_res.json().get('total', 0)

        # --- REFINED HEALTH LOGIC ---
        # Calculate a percentage score: fewer unused fields = higher score.
        health_score = round((1 - (orphaned_count/max(len(field_analysis),1))) * 100)
        
        # Decide which specific advice to give based on the data.
        top_rec = "Architecture Healthy"
        if orphaned_count > 10:
            top_rec = "Field Bloat Detected"
        elif automation_total > 50:
            top_rec = "Optimize Automation Rules"
        elif len(installed_apps) > 20:
            top_rec = "Review App License Costs"

        # Bundle everything up and send the final report back to the user.
        return jsonify({
            "status": "success",
            "instance": domain.upper(),
            "stats": {
                "projects": len(project_list), "issues": total_issues, "fields": len(custom_fields),
                "users": user_count, "workflows": len(workflow_data), "addons": len(installed_apps),
                "automations": automation_total
            },
            "ai_analysis": {
                "instance_health": f"{health_score}%",
                "cleanup_required": orphaned_count > 10 or automation_total > 100,
                "top_recommendation": top_rec
            },
            "data": {
                "project_list": project_list, "addon_list": installed_apps,
                "field_list": field_analysis, "workflow_list": workflow_data
            }
        })
    except Exception as e:
        # If the app crashes for any reason, log the error and tell the user.
        logger.error(f"Dashboard Error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

# --- ROUTE 2: AUTOMATION INTEL ---
# A specialized section that lists all the specific automation rules.
@app.route('/api/jira/automation', methods=['POST'])
def get_automation_intel():
    data = request.json
    site_url = data.get('url', '').rstrip('/')
    email = data.get('email')
    token = data.get('token')

    if not all([site_url, email, token]):
        return jsonify({"status": "error", "message": "Missing credentials"}), 400

    # Prepare credentials for the public Atlassian Automation API.
    auth_str = f"{email}:{token}"
    encoded_auth = base64.b64encode(auth_str.encode()).decode()
    public_headers = {
        "Accept": "application/json", 
        "Content-Type": "application/json", 
        "Authorization": f"Basic {encoded_auth}"
    }

    try:
        # Get the Cloud ID to access the automation rules.
        cloud_id = get_cloud_id(site_url, email, token)
        if not cloud_id:
            return jsonify({"status": "error", "message": "Cloud ID not found"}), 404

        public_url = f"https://api.atlassian.com/automation/public/jira/{cloud_id}/rest/v1/rule/summary"
        auto_res = requests.post(public_url, headers=public_headers, json={"limit": 100})
        
        automation_list = []
        if auto_res.status_code == 200:
            rules = auto_res.json().get('data', [])
            for rule in rules:
                rule_uuid = rule.get('uuid')
                raw_updated = rule.get('updated', 0)
                # Convert the computer timestamp into a readable date (Year-Month-Day).
                updated_date = datetime.fromtimestamp(raw_updated).strftime('%Y-%m-%d %H:%M')
                
                # Save the name, status, and author of each rule.
                automation_list.append({
                    "name": rule.get('name'),
                    "uuid": rule_uuid,
                    "status": rule.get('state'),
                    "updated": updated_date,
                    "author_name": rule.get('author', {}).get('displayName', 'System'),
                    "author_id": rule.get('authorAccountId', 'N/A')
                })

        return jsonify({"status": "success", "automation_data": automation_list})

    except Exception as e:
        logger.error(f"Automation Intel Error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

# This is the starting gun—it tells the app to start running now.
if __name__ == '__main__':
    app.run(debug=True, port=5000)
