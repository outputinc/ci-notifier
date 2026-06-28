#!/usr/bin/env python3
import datetime
import hashlib
import hmac
import json
import os
import time
import urllib.parse

import requests
from flask import Flask, jsonify, request
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

app = Flask(__name__)

CIRCLECI_TOKEN      = os.environ['CIRCLECI_TOKEN']
SLACK_BOT_TOKEN     = os.environ['SLACK_BOT_TOKEN']
SLACK_SIGNING_SECRET = os.environ['SLACK_SIGNING_SECRET']
INTERNAL_TOKEN      = os.environ['INTERNAL_TOKEN']
GCP_PROJECT         = os.environ['GCP_PROJECT']
GCP_LOCATION        = os.environ.get('GCP_LOCATION', 'us-central1')
TASKS_QUEUE         = os.environ['TASKS_QUEUE']
SERVICE_URL         = os.environ['SERVICE_URL']
ORG_PREFIX          = os.environ.get('ORG_PREFIX', 'github/outputinc')
POLL_INTERVAL       = int(os.environ.get('POLL_INTERVAL_SECONDS', '30'))

# Optional — Pushover notifications for a specific Slack user
PUSHOVER_TOKEN        = os.environ.get('PUSHOVER_TOKEN')
PUSHOVER_USER         = os.environ.get('PUSHOVER_USER')
PUSHOVER_SLACK_USER_ID = os.environ.get('PUSHOVER_SLACK_USER_ID')

TERMINAL_STATES = {'success', 'failed', 'error', 'canceled', 'unauthorized'}


def verify_slack_signature(req):
    ts = req.headers.get('X-Slack-Request-Timestamp', '')
    sig = req.headers.get('X-Slack-Signature', '')
    if not ts or abs(time.time() - int(ts)) > 300:
        return False
    base = f'v0:{ts}:{req.get_data(as_text=True)}'.encode()
    expected = 'v0=' + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


def verify_internal(req):
    return req.headers.get('X-Internal-Token') == INTERNAL_TOKEN


def ci_get(path):
    r = requests.get(
        f'https://circleci.com/api/v2/{path}',
        headers={'Circle-Token': CIRCLECI_TOKEN, 'Accept': 'application/json'},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def slack_dm(user_id, text):
    r = requests.post(
        'https://slack.com/api/conversations.open',
        headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'},
        json={'users': user_id},
        timeout=10,
    )
    r.raise_for_status()
    channel_id = r.json()['channel']['id']
    requests.post(
        'https://slack.com/api/chat.postMessage',
        headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'},
        json={'channel': channel_id, 'text': text},
        timeout=10,
    ).raise_for_status()


def pushover_notify(title, message, url):
    requests.post(
        'https://api.pushover.net/1/messages.json',
        data={
            'token': PUSHOVER_TOKEN,
            'user': PUSHOVER_USER,
            'title': title,
            'message': message,
            'url': url,
            'url_title': 'View your build',
        },
        timeout=10,
    ).raise_for_status()


def enqueue_poll(payload, delay_seconds=0):
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(GCP_PROJECT, GCP_LOCATION, TASKS_QUEUE)

    task = {
        'http_request': {
            'http_method': tasks_v2.HttpMethod.POST,
            'url': f'{SERVICE_URL}/poll',
            'headers': {
                'Content-Type': 'application/json',
                'X-Internal-Token': INTERNAL_TOKEN,
            },
            'body': json.dumps(payload).encode(),
        }
    }

    if delay_seconds > 0:
        schedule_time = datetime.datetime.utcnow() + datetime.timedelta(seconds=delay_seconds)
        ts = timestamp_pb2.Timestamp()
        ts.FromDatetime(schedule_time)
        task['schedule_time'] = ts

    client.create_task(parent=parent, task=task)


def parse_command(text):
    """Accept '<repo> <branch>' or '<org/repo> <branch>'."""
    parts = text.strip().split()
    if len(parts) != 2:
        return None, None
    repo, branch = parts
    project_slug = repo if '/' in repo else f'{ORG_PREFIX}/{repo}'
    return project_slug, branch


@app.route('/slash', methods=['POST'])
def slash():
    if not verify_slack_signature(request):
        return jsonify({'error': 'Unauthorized'}), 403

    text = request.form.get('text', '')
    user_id = request.form.get('user_id')
    project_slug, branch = parse_command(text)

    if not project_slug:
        return jsonify({
            'response_type': 'ephemeral',
            'text': (
                'Usage: `/ci-notify <repo> <branch>`\n'
                'Example: `/ci-notify Hydra main` or `/ci-notify github/outputinc/Hydra my-feature`'
            ),
        })

    enqueue_poll({
        'project_slug': project_slug,
        'branch': branch,
        'user_id': user_id,
        'triggered_after': time.time(),
    })

    repo = project_slug.split('/')[-1]
    return jsonify({
        'response_type': 'ephemeral',
        'text': f"Got it — I'll DM you when *{repo}* @ `{branch}` completes. :circle-ci:",
    })


@app.route('/poll', methods=['POST'])
def poll():
    if not verify_internal(request):
        return 'Unauthorized', 403

    payload = request.get_json()
    project_slug    = payload['project_slug']
    branch          = payload['branch']
    user_id         = payload['user_id']
    pipeline_id     = payload.get('pipeline_id')
    pipeline_number = payload.get('pipeline_number')
    triggered_after = payload.get('triggered_after', 0)

    # Resolve pipeline
    if not pipeline_id:
        encoded = urllib.parse.quote(branch, safe='')
        data = ci_get(f'project/{project_slug}/pipeline?branch={encoded}')
        all_items = data.get('items', [])

        # Prefer pipelines created after the slash command was run
        items = [i for i in all_items if _parse_iso(i.get('created_at', '')) >= triggered_after]

        if not items and all_items:
            # No new pipeline yet — check if the most recent one is still running.
            # This handles the common case where the user ran the command while a
            # pipeline was already in progress (created_at < triggered_after).
            most_recent = all_items[0]
            wf_data = ci_get(f'pipeline/{most_recent["id"]}/workflow')
            wf_items = wf_data.get('items', [])
            if wf_items and not all(w['status'] in TERMINAL_STATES for w in wf_items):
                print(f"[poll] no post-command pipeline found; adopting in-progress pipeline "
                      f"{most_recent['id']} for {project_slug}/{branch}")
                items = [most_recent]

        if not items:
            print(f"[poll] no active pipeline found for {project_slug}/{branch}, will retry")
            enqueue_poll(payload, delay_seconds=POLL_INTERVAL)
            return 'ok', 200

        pipeline_id = items[0]['id']
        pipeline_number = items[0]['number']
        payload['pipeline_id'] = pipeline_id
        payload['pipeline_number'] = pipeline_number
        print(f"[poll] tracking pipeline {pipeline_id} (#{pipeline_number}) for {project_slug}/{branch}")

    # Check workflow statuses
    data = ci_get(f'pipeline/{pipeline_id}/workflow')
    workflows = data.get('items', [])

    statuses = [w['status'] for w in workflows]
    print(f"[poll] pipeline {pipeline_id} workflow statuses: {statuses}")

    if not workflows or not all(w['status'] in TERMINAL_STATES for w in workflows):
        enqueue_poll(payload, delay_seconds=POLL_INTERVAL)
        return 'ok', 200

    # All done — notify
    failed = [w for w in workflows if w['status'] != 'success']
    overall = 'failed' if failed else 'success'
    icon = '🟢' if overall == 'success' else '🔴'
    repo = project_slug.split('/')[-1]

    pipeline_url = f"https://app.circleci.com/pipelines/{project_slug}/{pipeline_number}"

    lines = [f"{icon} *{repo}* pipeline #{pipeline_number} on `{branch}` — *{overall}*"]
    if failed:
        lines.append('Failed workflows: ' + ', '.join(f"`{w['name']}`" for w in failed))
    lines.append(f"<{pipeline_url}|View your build>")

    slack_dm(user_id, '\n'.join(lines))

    if PUSHOVER_TOKEN and PUSHOVER_USER and user_id == PUSHOVER_SLACK_USER_ID:
        push_lines = [f"Pipeline #{pipeline_number} on {branch}"]
        if failed:
            push_lines.append('Failed workflows: ' + ', '.join(w['name'] for w in failed))
        pushover_notify(
            title=f"{icon} {repo} — {overall}",
            message='\n'.join(push_lines),
            url=pipeline_url,
        )

    return 'ok', 200


def _parse_iso(ts):
    """Parse an ISO 8601 timestamp to a Unix epoch float."""
    if not ts:
        return 0.0
    try:
        dt = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        return dt.timestamp()
    except ValueError:
        return 0.0


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
