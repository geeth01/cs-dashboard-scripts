import csv
from datetime import datetime, timedelta
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import time
from http.client import IncompleteRead
import re
import os
import pandas as pd
import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

slack_token = os.environ.get("SLACK_BOT_TOKEN", "")  # Placeholder for Slack Bot Token
channel_id = "C07SUNJ3ZEV" 

client = WebClient(token=slack_token)

# start_date = '2025-02-19'
# filename = "sanity_results.csv"
# start_date = datetime.today().strftime('%Y-%m-%d') # today's date

def fetch_messages(start_date):
    try:
        cursor = None
        message_dict = {}
        start_date_obj = datetime.strptime(start_date, "%Y-%m-%d")
        # Full day for the given date only (00:00:00 to 23:59:59)
        start_of_day = start_date_obj.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_date_obj.replace(hour=23, minute=59, second=59, microsecond=999999)
        oldest_ts = str(int(start_of_day.timestamp()))
        latest_ts = str(int(end_of_day.timestamp()))

        MAX_RETRIES = 3
        RETRY_DELAY = 2  # seconds

        while True:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    response = client.conversations_history(
                        channel=channel_id,
                        limit=200,
                        cursor=cursor,
                        oldest=oldest_ts,
                        latest=latest_ts,
                        timeout=120
                    )
                    break
                except IncompleteRead as e:
                    print(f"[Attempt {attempt}] IncompleteRead error: {e}. Retrying in {RETRY_DELAY}s...")
                    time.sleep(RETRY_DELAY)
                except SlackApiError as e:
                    print(f"[Attempt {attempt}] Slack API error: {e.response['error']}. Retrying in {RETRY_DELAY}s...")
                    time.sleep(RETRY_DELAY)
                except Exception as e:
                    print(f"[Attempt {attempt}] Unexpected error: {e}. Retrying in {RETRY_DELAY}s...")
                    time.sleep(RETRY_DELAY)
            else:
                print("Failed to fetch messages after retries.")
                break

            messages = response.get('messages', [])
            cursor = response.get('response_metadata', {}).get('next_cursor')

            # Process newest first; first occurrence per sanity per day is enough
            for message in messages:
                message_date_obj = datetime.fromtimestamp(float(message['ts']))
                if message_date_obj < start_of_day:
                    break  # Past our date range, no need to paginate further
                if message_date_obj > end_of_day:
                    continue

                # Skip image-only messages (no text and only files/images)
                text = message.get('text', '') or ''
                if not text.strip() and message.get('files'):
                    continue
                blocks = message.get('blocks') or []
                if not text.strip() and not any(b.get('type') in ('section', 'header') for b in blocks):
                    continue

                lines = text.split('\n') if text else []
                first_line, test_count = "", "NA"
                lp_line = ""

                if 'dev11' in text.lower() and (
                    'result' in text.lower()
                    or ':x:' in text.lower()
                    or ':white_check_mark:' in text.lower()
                    or 'failure' in text.lower()
                    or 'passed' in text.lower()
                    or 'success' in text.lower()
                ):
                    # Pipe format: "Dev11 | Suite Name :emoji: | N/N Modules | N/N Tests | ..."
                    if re.match(r'^dev11\s*\|', lines[0].strip() if lines else '', re.I):
                        pipe_line = lines[0].strip()
                        pipe_parts = [p.strip() for p in pipe_line.split('|')]
                        if len(pipe_parts) >= 2:
                            suite_segment = pipe_parts[1]
                            suite_segment = re.sub(r'\s*:white_check_mark:\s*$', '', suite_segment, flags=re.I)
                            suite_segment = re.sub(r'\s*:x:\s*$', '', suite_segment, flags=re.I)
                            first_line = suite_segment.replace('*', '').strip()
                        match = re.search(r'(\d+)/(\d+)\s*Tests', pipe_line)
                        if match:
                            test_count = match.group(2)
                    elif 'Live Preview' in text:
                        first_line = lines[1].strip() if len(lines) > 1 else ""
                        lp_line = lines[2].strip() if len(lines) > 2 else ""
                    else:
                        first_line = lines[0].strip() if lines else ""
                    if not first_line or test_count == "NA":
                        total_tests_found = test_count != "NA"
                        for line in lines[1:]:
                            if "*Total Tests*" in line:
                                match = re.search(r"\*Total Tests\*:\s*(\d+)", line)
                                if match:
                                    test_count = match.group(1)
                                    total_tests_found = True
                                break
                        if not total_tests_found:
                            for line in lines[1:]:
                                if "(" in line and ")" in line and "Passed" in line:
                                    match = re.search(r"\((\d+) / (\d+) Passed", line)
                                    if match:
                                        test_count = match.group(2)
                                    break
                            if "passed" in lp_line:
                                match = re.search(r"\((\d+) / (\d+) tests passed", lp_line)
                                if match:
                                    test_count = match.group(2)
                                    lp_line = ""

                elif blocks:
                    for block in blocks:
                        if block.get('type') == 'image':
                            continue  # Skip image blocks
                        if block.get('type') in ('section', 'header'):
                            block_text = block.get('text', {}).get('text', '').strip()
                            blines = block_text.split('\n')
                            if blines and 'dev11' in blines[0].lower():
                                first_line = blines[0].strip()
                            for line in blines[1:]:
                                match = re.search(r"\((\d+) / (\d+) Passed", line)
                                if match:
                                    test_count = match.group(2)
                                    break
                            break

                if first_line:
                    date_key = message_date_obj.strftime('%m/%d')
                    if first_line not in message_dict:
                        message_dict[first_line] = {}
                    # Keep first occurrence only (no need to overwrite)
                    if date_key not in message_dict[first_line]:
                        message_dict[first_line][date_key] = test_count

            if not cursor:
                break
        
        # save_to_csv(message_dict, start_date_obj, today_date_obj) 
        save_to_csv(message_dict, start_date_obj, start_date_obj, start_date)
    except SlackApiError as e:
        print(f"Error fetching messages: {e.response['error']}")
    except KeyError as e:
        print(f"Unexpected message format: {e}")

def save_to_csv(data, start_date_obj, end_date_obj, start_date):
    date_range = [(start_date_obj + timedelta(days=i)).strftime('%m/%d') 
                  for i in range((end_date_obj - start_date_obj).days + 1)]
    filename = "sanity_results_" + start_date + ".csv"
    
    with open(filename, "w", newline="") as file:
        writer = csv.writer(file)
        header = ["Sanity"] + date_range
        writer.writerow(header)
        
        for sanity, results in data.items():
            row = [sanity] + [results.get(date, "NA") for date in date_range]
            writer.writerow(row)
    
    print(f"CSV file '{filename}' created successfully.")

start_date = '2026-05-13'
# end_date = '2025-02-22'

# start_date = datetime.today().strftime('%Y-%m-%d')

start_date_temp = start_date
# end_date_temp = end_date

# # Convert string to datetime object
start_date_obj = datetime.strptime(start_date, "%Y-%m-%d")
# end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")

# # Loop through each date from start_date to end_date
current_date = start_date_obj
# while current_date <= end_date_obj:
start_date = current_date.strftime("%Y-%m-%d")
fetch_messages(current_date.strftime("%Y-%m-%d"))  # Call the method with the formatted date
# current_date += timedelta(days=1)

# Define the order list, ensure there are no leading/trailing spaces

order = [
    "RTE Full Sanity - UI",
    "Full Sanity - UI",
    "Assets Full Sanity - UI",
    "Search Full Sanity - UI",
    "Taxonomy Full Sanity - UI",
    "Variants Full Sanity - UI",
    "Extensions Full Sanity - UI",
    "Releases20 Full Sanity - UI",
    "AutoDraft Full Sanity - UI",
    "CMA Full Sanity - API",
    "CMA API AutoDraft - API",
    "Taxonomy - API",
    "CMA Nested Global Fields - API",
    "BulkDelete - API",
    "Release20 - API",
    "CMA Basic Sanity - API",
    "Search Full Sanity - API",
    "Search Variants Sanity - API",
    "Variants Sanity - API",
    "CDA Full Sanity - API",
    "CLI-CMS Sanity",
    "CLI-Marketplace Sanity",
    "CLI-Personalize Sanity",
    "SDK-JS-CDA Sanity", 
    "SDK-JS-CMA Sanity",
    "SDK-Java-CDA Sanity",
    "SDK-Java-CMA Sanity",
    "SDK-Marketplace Sanity",
    "SDK-Dotnet-CDA Sanity",
    "SDK-Typescript-CDA Sanity",
    "SDK-Python-CDA Sanity",
    "Rest Preview Service - API",
    "GraphQL Preview Service - API",
    "Timeline Preview Sanity - UI",
    "Live Preview Sanity - UI",
    "Visual Builder E2E - UI",
    "CSI AI Assistant - E2E",
    "CSI Brand Kit - E2E",
    "Orgadmin Sanity - UI",
    "Auth background jobs events - UI",
    "Webhook Test Suite - UI",
    "Argus Basic Sanity - UI",
    "Global Dashboard Sanity - Parameterized - UI",
    "OneClickTrial - UI",
    "Top Level Navigation Smoke Test - UI",
    "Notifications Sanity - UI",
    "Platform Discovery Sanity - UI",
    "Org Compare API",
    "Auth Tokens Sanity Test",
    "Analytics V1 V2 parity",
    "RBAC Sanity - UI",
    "AssetManagement20 - API",
    "Asset Management2.0 Test - UI",
    "AssetPicker Full Sanity - UI",
    "Marketplace Sanity - UI",
    "Developerhub Sanity - UI"
]

replacements = {
    r'\*Result\*:': '',  # Matches "*Result*:"
    r'dev11,': '', 
    r'dev11 ,': '', 
    r'Dev11,': '', 
    r'Passed': '',       # Matches "Passed"
    r'Success': '',      # Matches "Success"
    r'Failure': '',      # Matches "Failure"
    r'\bResult\b': '',   # Matches "Result" as a whole word
    r'\bModules\b': '',  # Matches "Modules" as a whole word
    r'\(': '',           # Removes "("
    r'\)': '',           # Removes ")"
    r':': '',            # Removes ":"
    r'tests passed': '', 
    r'"': '',
    r'mins': 'm',
    r'sec.': 's',   
    r'\*': ''            # Removes all asterisks
}

filename = "sanity_results_" + start_date + ".csv"
df = pd.read_csv(filename)

for col in df.columns:
    df[col] = df[col].replace(replacements, regex=True)

# Remove leading/trailing spaces in both order list and 'Sanity' column
order = [o.strip() for o in order]
df['Sanity'] = df['Sanity'].str.strip()

# Find the missing Sanity values (those in the order list but not in the DataFrame)
missing_sanity_values = [sanity for sanity in order if sanity not in df['Sanity'].values]

# Get the second column dynamically (usually Run 2(latest), or whatever it may be)
second_column = df.columns[1]  # This grabs the second column name dynamically

# Create a list of new rows for missing sanity values
new_rows = []
for sanity in missing_sanity_values:
    # Prepare a new row with only the second column updated to 'NoRun'
    new_row = {'Sanity': sanity, second_column: 'NA'}
    new_rows.append(new_row)

# Create a DataFrame for the new rows
new_df = pd.DataFrame(new_rows)

# Concatenate the new rows with the existing DataFrame
df = pd.concat([df, new_df], ignore_index=True)

df['SortOrder'] = df['Sanity'].apply(lambda x: order.index(x) if x in order else float('inf'))
df_sorted = df.sort_values(by='SortOrder').drop(columns=['SortOrder'])

csv_file = "sorted_tests_" + start_date + ".csv"

print(csv_file)

# Write the sorted DataFrame back to the CSV file
df_sorted.to_csv(csv_file, index=False)

# Load CSV file (assuming tab-delimited)
df = pd.read_csv(csv_file)  # Read CSV with headers

# Extract column names
test_name_column = df.columns[0]  # "Sanity"
date_column_name = df.columns[1]  # e.g., "02/21"

print(test_name_column)
print(date_column_name)

# Google Sheets Authentication
json_key_file = "jiraproject-key.json"  # Update this with your JSON key file path
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file(json_key_file, scopes=scope)
client = gspread.authorize(creds)

sheet_name = "FY27TC"
sheet_id = "1rMUn-tPQpQwmj4qbdrOTWipu_sjRU0BCap11tPPFjrM"

worksheet = client.open_by_key(sheet_id).worksheet(sheet_name)

data = worksheet.get_all_values()

# Get the last column index and check if the date column exists
header_row = data[0]  # First row contains headers
if date_column_name not in header_row:
    date_column_index = len(header_row) + 1  # Append a new column
    worksheet.update_cell(1, date_column_index, date_column_name)  # Add new date column header
else:
    date_column_index = header_row.index(date_column_name) + 1  # Existing column index (1-based)

# Batch updates for speed (one API call instead of many)
updates = []
for index, row in enumerate(data):
    if index == 0:
        continue
    test_name = row[0].strip()
    matching_row = df[df[test_name_column] == test_name]
    if not matching_row.empty:
        value_to_update = str(matching_row[date_column_name].values[0])
        if value_to_update.strip().upper() == "NA":
            value_to_update = 0
        cell_a1 = rowcol_to_a1(index + 1, date_column_index)
        updates.append({"range": cell_a1, "values": [[value_to_update]]})
if updates:
    worksheet.batch_update(updates, value_input_option="USER_ENTERED")
print("Google Sheet updated successfully!")