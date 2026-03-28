from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import requests
from requests.auth import HTTPBasicAuth
import logging
import base64
import json
from datetime import datetime

app = Flask(__name__)
CORS(app) # Open CORS for local development

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 1. HOME ROUTE ---
@app.route('/')
def home():
    # Flask will now look in the root folder instead of /templates
    return render_template('index.html')


# --- UTILITY: GET CLOUD ID ---
def get_cloud_id(site_url, email, token):
    try:
        res = requests.get(f"{site_url}/_edge/tenant_info", auth=HTTPBasicAuth(email, token), timeout=10)
        if res.status_code == 200:
            return res.json().get('cloudId')
        else:
            logger.error(f"Tenant Info Failed: {res.status_code}")
            return None
    except Exception as e:
        logger.error(f"CloudID Fetch Error: {e}")
    return None

# --- ROUTE 1: MAIN DASHBOARD (FAST LOAD) ---
@app.route('/api/jira/analyze', methods=['POST'])
def analyze_jira():
    data = request.json
    site_url = data.get('url', '').rstrip('/')
    domain = site_url.split("//")[-1]
    email = data.get('email')
    token = data.get('token')

    if not all([site_url, email, token]):
        return jsonify({"status": "error", "message": "Missing credentials"}), 400

    session = requests.Session()
    session.auth = HTTPBasicAuth(email, token)
    
    try:
        # --- CRITICAL FIX: THE GATEKEEPER CHECK ---
        # Verify credentials against Atlassian before performing any analysis
        verify_res = session.get(f"{site_url}/rest/api/3/myself", timeout=10)
        
        if verify_res.status_code == 401:
            return jsonify({"status": "error", "message": "Invalid API Token or Email"}), 401
        elif verify_res.status_code != 200:
            return jsonify({"status": "error", "message": "Could not connect to Jira"}), verify_res.status_code
        # ------------------------------------------

        # --- 1. ADD-ONS (UPDATED WITH VERSION & VENDOR LOGIC) ---
        cloud_id = get_cloud_id(site_url, email, token)
        installed_apps = []
        
        if cloud_id:
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
                
                for node in nodes:
                    app_info = node.get('app', {})
                    vendor = app_info.get('vendorName', '')
                    lic_info = node.get('license') or {}
                    
                    # Logic to handle null versions/types
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
        session.headers.update({"Accept": "application/json"})
        wf_res = session.get(f"{site_url}/rest/api/3/workflow/search", params={"expand": "statuses,transitions", "maxResults": 1000})
        workflow_data = []
        if wf_res.status_code == 200:
            for wf in wf_res.json().get('values', []):
                steps = len(wf.get('statuses', []))
                trans = len(wf.get('transitions', []))
                workflow_data.append({
                    "name": wf.get('id', {}).get('name') or wf.get('name'), 
                    "steps": steps, 
                    "transitions": trans, 
                    "intelligence": "High Complexity" if (steps > 10 or trans > 15) else "Optimized"
                })

        # --- 3. FIELDS ---
        field_res = session.get(f"{site_url}/rest/api/3/field")
        custom_fields = [f for f in (field_res.json() if field_res.status_code == 200 else []) if f.get('custom')]
        field_analysis = []
        orphaned_count = 0
        for f in custom_fields:
            f_id = f.get('id')
            s_res = session.get(f"{site_url}/rest/api/3/field/{f_id}/screens")
            screens = s_res.json().get('total', 0) if s_res.status_code == 200 else 0
            if screens == 0: orphaned_count += 1
            field_analysis.append({
                "name": f.get('name'), 
                "id": f_id, 
                "priority": "CRITICAL" if screens == 0 else "Low", 
                "screens": screens, 
                "recommendation": "Active" if screens > 0 else "Inactive"
            })

        # --- 4. PROJECTS ---
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
        health_score = round((1 - (orphaned_count/max(len(field_analysis),1))) * 100)
        
        top_rec = "Architecture Healthy"
        if orphaned_count > 10:
            top_rec = "Field Bloat Detected"
        elif automation_total > 50:
            top_rec = "Optimize Automation Rules"
        elif len(installed_apps) > 20:
            top_rec = "Review App License Costs"

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
        logger.error(f"Dashboard Error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

# --- ROUTE 2: AUTOMATION INTEL ---
@app.route('/api/jira/automation', methods=['POST'])
def get_automation_intel():
    data = request.json
    site_url = data.get('url', '').rstrip('/')
    email = data.get('email')
    token = data.get('token')

    if not all([site_url, email, token]):
        return jsonify({"status": "error", "message": "Missing credentials"}), 400

    auth_str = f"{email}:{token}"
    encoded_auth = base64.b64encode(auth_str.encode()).decode()
    public_headers = {
        "Accept": "application/json", 
        "Content-Type": "application/json", 
        "Authorization": f"Basic {encoded_auth}"
    }

    try:
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
                updated_date = datetime.fromtimestamp(raw_updated).strftime('%Y-%m-%d %H:%M')
                
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

if __name__ == '__main__':
    app.run(debug=True, port=5000)
