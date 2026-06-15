import os
import time
from datetime import datetime, timezone, timedelta
import requests

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
CHANNEL_ID = "C0B87NGBE73"

IST = timezone(timedelta(hours=5, minutes=30))
today = datetime.now(IST).date()
cutoff_ist = datetime(today.year, today.month, today.day, 20, 0, 0, tzinfo=IST)
cutoff_ts = cutoff_ist.timestamp()

print(f"Deleting messages sent after: {cutoff_ist.strftime('%Y-%m-%d %H:%M:%S IST')}")
print(f"Unix timestamp cutoff: {cutoff_ts}\n")


def slack_get(method, params):
    resp = requests.get(
        f"https://slack.com/api/{method}",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        params=params,
    )
    resp.raise_for_status()
    return resp.json()


def slack_post(method, payload):
    resp = requests.post(
        f"https://slack.com/api/{method}",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_messages_after(channel, oldest_ts):
    messages = []
    cursor = None
    while True:
        params = {"channel": channel, "oldest": oldest_ts, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = slack_get("conversations.history", params)
        if not data.get("ok"):
            print(f"Error fetching messages: {data.get('error')}")
            break
        messages.extend(data.get("messages", []))
        next_cursor = data.get("response_metadata", {}).get("next_cursor")
        if not next_cursor:
            break
        cursor = next_cursor
    return messages


def delete_message(channel, ts):
    result = slack_post("chat.delete", {"channel": channel, "ts": ts})
    return result.get("ok"), result.get("error")


def delete_file(file_id):
    result = slack_post("files.delete", {"file": file_id})
    return result.get("ok"), result.get("error")


def main():
    if not SLACK_BOT_TOKEN:
        print("Error: SLACK_BOT_TOKEN environment variable not set.")
        return

    print("Fetching messages...")
    messages = fetch_messages_after(CHANNEL_ID, cutoff_ts)
    print(f"Found {len(messages)} message(s) to process.\n")

    deleted_msgs = 0
    failed_msgs = 0
    deleted_files = 0
    failed_files = 0

    for msg in messages:
        ts = msg.get("ts")
        msg_type = msg.get("type")
        subtype = msg.get("subtype", "")

        # Delete attached files (images, etc.)
        for f in msg.get("files", []):
            file_id = f.get("id")
            ok, err = delete_file(file_id)
            if ok:
                print(f"  Deleted file: {f.get('name', file_id)}")
                deleted_files += 1
            else:
                print(f"  Failed to delete file {file_id}: {err}")
                failed_files += 1
            time.sleep(0.5)

        # Delete the message itself
        ok, err = delete_message(CHANNEL_ID, ts)
        if ok:
            print(f"Deleted message at ts={ts}")
            deleted_msgs += 1
        else:
            print(f"Failed to delete message ts={ts}: {err}")
            failed_msgs += 1

        time.sleep(1)  # Respect Slack rate limits (Tier 3: ~50 req/min)

    print(f"\nDone.")
    print(f"Messages deleted: {deleted_msgs}, failed: {failed_msgs}")
    print(f"Files deleted:    {deleted_files}, failed: {failed_files}")


if __name__ == "__main__":
    main()
