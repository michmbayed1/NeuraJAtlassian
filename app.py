from flask import Flask, jsonify, request, render_template # Added render_template
from flask_cors import CORS
import requests
from requests.auth import HTTPBasicAuth
import logging

app = Flask(__name__)
CORS(app) 

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 1. HOME ROUTE (This serves your HTML file) ---
@app.route('/')
def home():
    # Flask looks for index.html inside the /templates folder automatically
    return render_template('index.html')

# --- 2. ANALYSIS ROUTE (Your Jira Logic) ---
@app.route('/api/jira/analyze', methods=['POST'])
def analyze_jira():
    data = request.json
    if not data:
        return jsonify({"status": "error", "message": "No data provided"}), 400
        
    site_url = data.get('url', '').rstrip('/')
    email = data.get('email')
    token = data.get('token')

    if not all([site_url, email, token]):
        return jsonify({"status": "error", "message": "Missing credentials"}), 400

    session = requests.Session()
    session.auth = HTTPBasicAuth(email, token)
    
    try:
        # --- 1. ADD-ONS ---
        session.headers.update({"Accept": "application/vnd.atl.plugins.installed+json", "X-Atlassian-Token": "nocheck"})
        addon_res = session.get(f"{site_url}/rest/plugins/1.0/?os_authType=basic", timeout=20)
        installed_apps = []
        if addon_res.status_code == 200:
            plugins = addon_res.json().get('plugins', [])
            installed_apps = [{
                "name": a.get('name'),
                "key": a.get('key'),
                "status": "Active" if a.get('enabled') else "Disabled",
                "version": a.get('version', 'N/A')
            } for a in plugins if a.get('userInstalled')]

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
                    "name": p.get('name'), "key": p.get('key'), "issue_count": count,
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

        # --- AI Summary ---
        health_score = round((1 - (orphaned_count/max(len(field_analysis),1))) * 100)
        
        return jsonify({
            "status": "success",
            "instance": site_url.split("//")[-1].upper(),
            "stats": {
                "projects": len(project_list), "issues": total_issues, "fields": len(custom_fields),
                "users": user_count, "workflows": len(workflow_data), "addons": len(installed_apps)
            },
            "ai_analysis": {
                "instance_health": f"{health_score}%",
                "cleanup_required": orphaned_count > 10,
                "top_recommendation": "Field Bloat Detected" if orphaned_count > 10 else "Architecture Healthy"
            },
            "data": {
                "project_list": project_list, "addon_list": installed_apps,
                "field_list": field_analysis, "workflow_list": workflow_data
            }
        })
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    # Render uses gunicorn, but this is fine for local testing
    app.run(debug=True, host='0.0.0.0', port=5000)
