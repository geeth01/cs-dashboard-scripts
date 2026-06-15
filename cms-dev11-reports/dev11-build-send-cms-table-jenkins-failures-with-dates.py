import datetime
import sys
from datetime import datetime, timedelta
import os
import csv
import re
import pandas as pd
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import plotly.graph_objects as go
import shutil
import gspread
from google.oauth2.service_account import Credentials

file = 'sanity_messages.csv'
file1 = "table_image.png"
file2 = 'sanity_messages1.csv'
file3 = 'destination.csv'

# for range
# Get the date passed from the command line
given_date_str = sys.argv[1]  # e.g. "2025-07-08"
# Parse to datetime if needed
given_date = datetime.strptime(given_date_str, "%Y-%m-%d")

# start_date = '2025-07-04'
# start_date = datetime.today().strftime('%Y-%m-%d') # today's date

# start_date_obj = datetime.strptime(start_date, '%Y-%m-%d')

# if start_date_obj.date() != datetime.today().date():
#     today_date_obj = datetime.combine(start_date_obj.date(), datetime.max.time())
# else:
#     today_date_obj = datetime.now()

if os.path.exists(file):
    try:
        os.remove(file)
        if os.path.exists(file1):
            os.remove(file1)
        if os.path.exists(file2):
            os.remove(file2)
        if os.path.exists(file3):
            os.remove(file3)
    except Exception as e:
        print(f"Error deleting {file}: {e}")
else:
    print(f"File does not exist: {file}")


slack_token = os.environ.get("SLACK_BOT_TOKEN", "")  # Placeholder for Slack Bot Token
channel_id = "C07SUNJ3ZEV" 

channel_id_gk = 'C07G6R3FUDU' # gk-channel
# channel_id_gk = "C07SUNJ3ZEV" # cms-sanity-report

# Initialize Slack client
client = WebClient(token=slack_token)

# Define the CSV file
csv_file = 'sanity_messages.csv'

# Dictionary to track first lines and their associated second lines
message_dict = {}

# Fetch messages from a Slack channel starting from a specified date
def fetch_messages(start_date, end_date=None):
    try:
        cursor = None
        start_timestamp = str(start_date.timestamp())  # Convert start_date to Slack timestamp
        end_timestamp = str(end_date.timestamp()) if end_date else None  # Convert end_date if provided

        while True:
            # Fetch messages from the Slack channel
            response = client.conversations_history(
                channel=channel_id,
                limit=50,
                cursor=cursor,
                oldest=start_timestamp, 
                latest=end_timestamp,
                timeout=120
            )

            messages = response['messages']
            cursor = response.get('response_metadata', {}).get('next_cursor', None)

            for message in messages:
                # Convert timestamp to date and store the datetime object
                message_date_obj = datetime.fromtimestamp(float(message['ts']))

                # Ensure the message falls within the specified range
                if end_date:
                    if not (start_date <= message_date_obj <= end_date):
                        continue  # Skip messages outside the range
                else:
                    if message_date_obj < start_date:
                        continue  # Skip older messages

                text = message.get('text', '')
                first_line, second_line = "", ""

                if 'dev11' in text.lower() and 'result' in text.lower():
                    lines = text.split('\n')
                    first_line = lines[0].strip()
                    second_line = lines[1].strip() if len(lines) > 1 else ''
                    if 'Live Preview' in text:
                        first_line = lines[1].strip() if len(lines) > 1 else ''
                        second_line = lines[2].strip() if len(lines) > 2 else ''
                elif 'blocks' in message:
                    for block in message['blocks']:
                        if block['type'] in ['section', 'header']:
                            block_text = block.get('text', {}).get('text', '').strip()
                            lines = block_text.split('\n')

                            if len(lines) > 0 and 'dev11' in lines[0].lower():
                                first_line = lines[0].strip()
                                second_line = lines[1].strip() if len(lines) > 1 else ''
                                break
                            elif not first_line and len(lines) > 0:
                                first_line = lines[0].strip()
                            elif first_line and len(lines) > 0:
                                second_line = lines[0].strip()
                                break
                        elif block['type'] == 'section' and first_line:
                            block_text = block.get('text', {}).get('text', '').strip()
                            if block_text:
                                second_line = block_text
                                break

                # Store the lines and timestamp in the dictionary
                if first_line:
                    if first_line not in message_dict:
                        message_dict[first_line] = []
                    message_dict[first_line].append((second_line, message_date_obj))

            # Break if there are no more pages
            if not cursor:
                break

        save_to_csv()

    except SlackApiError as e:
        print(f"Error fetching messages: {e.response['error']}")
    except KeyError as e:
        print(f"Unexpected message format: {e}")


# Function to save data to CSV
def save_to_csv():
    if not os.path.exists(csv_file):
        with open(csv_file, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            header = ['Sanity', 'TIBS'] + sum([[f'Run {i}', f'Run {i} Timestamp'] for i in range(len(max(message_dict.values(), key=len)))], [])
            writer.writerow(header)

    with open(csv_file, mode='a', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        for first_line, entries in message_dict.items():
            statuses, timestamps = zip(*entries)

            # Save original order
            original_entries = list(zip(statuses, timestamps))

            # Sort the entries for TIBS calculation
            sorted_entries = sorted(zip(statuses, timestamps), key=lambda x: x[1])

            # Separate the sorted entries back into statuses and timestamps
            sorted_statuses, sorted_timestamps = zip(*sorted_entries)

            timestamp_strs = [ts.strftime('%Y-%m-%d %H:%M:%S') for ts in sorted_timestamps]
            TIBS = calculate_TIBS(sorted_statuses, sorted_timestamps)
            
            # Now, restore original order of the statuses and timestamps for saving
            timestamp_strs_original = [ts.strftime('%Y-%m-%d %H:%M:%S') for ts in timestamps]
            row = [first_line, TIBS] + [val for pair in zip(statuses, timestamp_strs_original) for val in pair]
            writer.writerow(row)

# Updated TIBS calculation with better debugging and formatting
def calculate_TIBS(statuses, timestamps):
    """
    Calculate TIBS as the time between the first failure and the next success.
    """
    def is_failure(status):
        return re.search(r'(fail|failure|x|:x:)', status, re.IGNORECASE)

    def is_success(status):
        return re.search(r'(success|:white_check_mark:)', status, re.IGNORECASE)

    first_failure_time = None
    next_success_time = None

    # Loop through sorted statuses and timestamps
    for i, (status, timestamp) in enumerate(zip(statuses, timestamps)):
        # print(f"Checking status: {status}, Timestamp: {timestamp}")

        if is_failure(status) and first_failure_time is None:
            # Record the first failure time
            first_failure_time = timestamp
            # print(f"First failure found at {first_failure_time}")

        elif is_success(status) and first_failure_time:
            # Found the next success after the first failure
            next_success_time = timestamp
            # print(f"Next success found at {next_success_time}")
            # Calculate TIBS
            tibs = next_success_time - first_failure_time
            # print(f"Calculated TIBS: {tibs}")
            return format_tibs(tibs)

    # If no success is found after the first failure
    # if first_failure_time:
    #     # tibs = datetime.now() - first_failure_time
    #     tibs = today_date_obj - first_failure_time
    #     return format_tibs(tibs)

    return "0"

def format_tibs(tibs):
    """Format the TIBS result into hours, minutes, and seconds."""
    # Format TIBS as a string in HH:MM:SS format
    if isinstance(tibs, timedelta):
        hours, remainder = divmod(tibs.total_seconds(), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{int(hours):02}:{int(minutes):02}:{int(seconds):02}"
    return str(tibs)


# Filter CSV content and update it
def filter_csv_with_pandas(input_file, output_file):
    df = pd.read_csv(input_file)
    filtered_df = df[~df.iloc[:, 0].str.contains(r'<@|report|Azure-eu', case=False, na=False)]
    filtered_df.to_csv(output_file, index=False)
    # print(f"Filtered rows saved to {output_file}")

# start_date = datetime.today().replace(hour=5, minute=0, second=0, microsecond=0)
# fetch_messages(start_date)

# for specific date
# given_date_str = "2025-07-08"  # Format: YYYY-MM-DD
# given_date = datetime.strptime(given_date_str, "%Y-%m-%d")


# Set to 5:00 AM on that date
start_date = given_date.replace(hour=5, minute=0, second=0, microsecond=0)

# Optional: set end_date to 24 hours after start_date
end_date = start_date + timedelta(days=1)

# Call your function with the given date range
fetch_messages(start_date, end_date)

filter_csv_with_pandas(csv_file, csv_file)

# Copy the contents of source.csv to destination.csv
shutil.copy(csv_file, 'destination.csv')

# Reading CSV to ensure updated headers match columns
df = pd.read_csv(csv_file)

# Base name for columns
base_column_name = "Sanity"
TIBS_column_name = "TIBS"

# Calculate the number of run columns dynamically
num_columns = df.shape[1]  # Total columns in DataFrame
runs_count = (num_columns - 2) // 2  # Each status-timestamp pair takes two columns

# Dynamically generate column names
column_names = [base_column_name, TIBS_column_name] + sum([[f"Run {i+1}", f"Run {i+1} Timestamp"] for i in range(runs_count)], [])

# Assign the generated column names to the DataFrame
df.columns = column_names

# Save the updated DataFrame
df.to_csv(csv_file, index=False)

# Remove Timestamp columns

# Load the CSV file into a DataFrame
df = pd.read_csv(csv_file)

# Remove columns that contain 'Timestamp' in their name
df = df.loc[:, ~df.columns.str.contains('Timestamp')]

# Save the cleaned DataFrame to a new CSV file
df.to_csv(csv_file, index=False)

for col in df.columns:
    col_values = df[col][1:]  # exclude the first row
    # Check if all values after the first row are empty or NaN
    if col_values.replace("", pd.NA).isna().all():
        df.drop(columns=[col], inplace=True)

# Get the current number of columns
num_columns = df.shape[1]
# print(num_columns)

# Define the base name for the first column
base_column_name = "Sanity"
base_column_name_next = "TIBS"

# Dynamically generate column names based on the number of columns
column_names = [base_column_name]  # Start with the base column name
for i in range(1, num_columns):
    if i == 1:  # Add "(latest)" only to the most recent column
        column_names.append(f"TIBS")    
    # elif i == 2:  # Add "(latest)" only to the most recent column
    #     column_names.append(f"Run {num_columns - i}(latest)")
    else:
        column_names.append(f"Run {num_columns - i}")

df.columns = column_names

# Save the modified CSV
output_path = 'sanity_messages1.csv'  # Replace with desired output file path
# df.to_csv(csv_file, index=False)
df.to_csv(output_path, index=False)

# remove TIBS column
if "TIBS" in df.columns:
    df = df.drop(columns=["TIBS"])

df.fillna("", inplace=True)

df.replace({
    r':white_check_mark:': '✅',  # Replace with Unicode check mark
    r':x:': '❌',
    r':X:': '❌'              # Replace with Unicode cross mark
}, regex=True, inplace=True)

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

# df = df[df.apply(lambda row: row.astype(str).str.contains("❌").any(), axis=1)]


# Apply replacements, except for the second column ('TIBS')
for col in df.columns:
    if col != 'TIBS':  # Skip the 'TIBS' column
        df[col] = df[col].replace(replacements, regex=True)

# df.fillna("", inplace=True)
df.to_csv(csv_file, index=False)

# Load the CSV file
df = pd.read_csv(csv_file)

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
    "CLI-TSGEN Sanity",
    "SDK-JS-CDA Sanity", 
    "SDK-JS-CMA Sanity",
    "SDK-Java-CDA Sanity",
    "SDK-Java-CMA Sanity",
    "SDK-Marketplace Sanity",
    "SDK-Dotnet-CDA Sanity",
    "SDK-Typescript-CDA Sanity",
    "Rest Preview Service - API",
    "GraphQL Preview Service - API",
    "Timeline Preview Sanity - UI",
    "Live Preview Sanity - UI",
    "Visual Builder E2E - UI",
    "CSI AI Assistant - E2E",
    "CSI Brand Kit - E2E",
    "OrgAdminSanity - UI",
    "Auth background jobs events - UI",
    "Webhook Test Suite - UI",
    "Argus Basic Sanity - UI",
    "Global Dashboard Sanity - Parameterized - UI",
    "Top Level Navigation Smoke Test - UI",
    "OneClickTrial - UI",
    "Notifications Sanity - UI",
    "Platform Discovery Sanity - UI",
    "Org Compare API",
    "Auth Tokens Sanity Test",
    "Analytics V1 V2 parity",
    "AssetManagement20 - API",
    "Asset Managment Test - UI",
    "AssetPicker Full Sanity - UI",
    "Marketplace Sanity - UI",
    "Developerhub Sanity - UI"
]


# Remove leading/trailing spaces in both order list and 'Sanity' column
order = [o.strip() for o in order]
df['Sanity'] = df['Sanity'].str.strip()

# Find the missing Sanity values (those in the order list but not in the DataFrame)
missing_sanity_values = [sanity for sanity in order if sanity not in df['Sanity'].values]

# Get the second column dynamically (usually Run 2(latest), or whatever it may be)
second_column = df.columns[1]  # This grabs the second column name dynamically

# Create a list of new rows for missing sanity values
new_rows = []

# Create a DataFrame for the new rows
new_df = pd.DataFrame(new_rows)

# Concatenate the new rows with the existing DataFrame
df = pd.concat([df, new_df], ignore_index=True)

# Sort the DataFrame by the order list
df['SortOrder'] = df['Sanity'].apply(lambda x: order.index(x) if x in order else float('inf'))
df_sorted = df.sort_values(by='SortOrder').drop(columns=['SortOrder'])

# Write the sorted DataFrame back to the CSV file
df_sorted.to_csv(csv_file, index=False)

input_csv = csv_file
output_image = "table_image.png"

def generate_table_image_with_plotly(input_file, output_file):
    # Read the CSV and filter the first 6 columns
    df = pd.read_csv(input_file)
    # for col in df.select_dtypes(include=["object", "string"]).columns:
    #     df[col] = df[col].fillna("")

    df.fillna("", inplace=True)

    font_styles = {
        'header': dict(size=12, family='Arial', color='white'),
        'cells': dict(size=7, family='Arial', color='black')
    }
    
    alignments = ['left' if col != 'TIBS' else 'center' for col in df.columns]
 
    # Create a Plotly table
    fig = go.Figure(data=[go.Table(
        header=dict(
            values=list(df.columns),
            fill_color='#0E4C92',  # Header background color
            align='center',
            font=font_styles['header'],  # White text for header
            line=dict(color='black', width=1),  # Border for header
            height=20  # Set the height of the header
        ),
        cells=dict(
            values=[df[col] for col in df.columns],
            align=alignments,
            fill_color='white',
            font=font_styles['cells'],  # Regular text font for all cells
            line=dict(color='black', width=1),  # Border for cells
            height=30  # Adjust row height
        )
    )])
    
    # Adjust column widths in the table trace (instead of in the layout)
    fig.update_traces(
        columnwidth=[0.2, 0.15, 0.15, 0.15, 0.15, 0.15]  # Adjust first column width
    )

    # Set general layout properties
    fig.update_layout(
        margin=dict(l=10, r=10, t=10, b=10),
        height=50 + len(df) * 30,  # Adjust height dynamically based on rows
        autosize=True
    )

    # Save the table as an image
    fig.write_image(output_file, scale=2)
    # print(f"Table image saved as {output_file}")


# # === Setup credentials and sheet ===
json_key_file = "jiraproject-key.json"
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file(json_key_file, scopes=scope)
client = gspread.authorize(creds)

sheet_name = "FY27Failures"
sheet_id = "1rMUn-tPQpQwmj4qbdrOTWipu_sjRU0BCap11tPPFjrM"
worksheet = client.open_by_key(sheet_id).worksheet(sheet_name)

# === Read current sheet data ===
sheet_data = worksheet.get_all_values()
header = sheet_data[0]
existing_rows = sheet_data[1:]

# === Get today's date as column header ===
# today = datetime.today().strftime("%m/%d")

given_date = datetime.strptime(given_date_str, "%Y-%m-%d")
today = given_date.strftime("%m/%d")

# === If today's column doesn't exist, append it ===
if today not in header:
    worksheet.update_cell(1, len(header) + 1, today)
    header.append(today)

# === Build row map: test name -> row index ===
row_map = {row[0].strip(): idx + 2 for idx, row in enumerate(existing_rows)}  # +2 because sheet is 1-based and first row is header

# === Read CSV and count ❌ ===
# csv_file = file  # Replace with your actual CSV file
fail_counts = {}

with open(csv_file, newline='', encoding='utf-8') as f:
    reader = csv.reader(f)
    next(reader)  # Skip header
    for row in reader:
        if not row or not row[0].strip():
            continue
        test_name = row[0].strip()
        failures = sum(cell.count("❌") for cell in row[1:] if cell)
        fail_counts[test_name] = failures

# === Write to sheet ===
col_index = header.index(today) + 1  # 1-based index for Google Sheets

# for test_name, row_idx in row_map.items():
#     fail_value = fail_counts.get(test_name, 0)
#     worksheet.update_cell(row_idx, col_index, fail_value)

# Build list of cell ranges + values
updates = []
for test_name, row_idx in row_map.items():
    fail_value = fail_counts.get(test_name, 0)
    updates.append({
        "range": gspread.utils.rowcol_to_a1(row_idx, col_index),
        "values": [[fail_value]]
    })

# === Send batch update ===
if updates:
    worksheet.batch_update(updates)


print(f"Updated Google Sheet with failure counts for {today}.")



# TOTAL COUNT

sheet_name = "FY27TotalRuns"
sheet_id = "1rMUn-tPQpQwmj4qbdrOTWipu_sjRU0BCap11tPPFjrM"
worksheet = client.open_by_key(sheet_id).worksheet(sheet_name)

# === Read current sheet data ===
sheet_data = worksheet.get_all_values()
header = sheet_data[0]
existing_rows = sheet_data[1:]

# === Get today's date as column header ===
# today = datetime.today().strftime("%m/%d")

given_date = datetime.strptime(given_date_str, "%Y-%m-%d")
today = given_date.strftime("%m/%d")

# === If today's column doesn't exist, append it ===
if today not in header:
    worksheet.update_cell(1, len(header) + 1, today)
    header.append(today)

# === Build row map: test name -> row index ===
row_map = {row[0].strip(): idx + 2 for idx, row in enumerate(existing_rows)}  # +2 because sheet is 1-based and first row is header

# === Read CSV and count ❌ ===
# csv_file = csv_file  # Replace with your actual CSV file
exec_counts = {}

with open(csv_file, newline='', encoding='utf-8') as f:
    reader = csv.reader(f)
    next(reader)  # Skip header
    for row in reader:
        if not row or not row[0].strip():
            continue
        test_name = row[0].strip()
        failures = sum(cell.count("❌") for cell in row[1:] if cell)
        passes   = sum(cell.count("✅") for cell in row[1:] if cell)
        total    = failures + passes
        exec_counts[test_name] = total  

# === Write to sheet ===
# col_index = header.index(today) + 1  # 1-based index for Google Sheets

# for test_name, row_idx in row_map.items():
#     exec_value = exec_counts.get(test_name, 0)
#     worksheet.update_cell(row_idx, col_index, exec_value)

# === Prepare batch update ===
col_index = header.index(today) + 1  # 1-based index for Google Sheets

# Build list of cell ranges + values
updates = []
for test_name, row_idx in row_map.items():
    exec_value = exec_counts.get(test_name, 0)
    updates.append({
        "range": gspread.utils.rowcol_to_a1(row_idx, col_index),
        "values": [[exec_value]]
    })

# === Send batch update ===
if updates:
    worksheet.batch_update(updates)


print(f"Updated Google Sheet with total counts for {today}.")



