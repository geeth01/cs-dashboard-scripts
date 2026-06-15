from datetime import datetime, timedelta
import os
import csv
import re
import sys
import shutil
import pandas as pd
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import plotly.graph_objects as go
import gspread
from datetime import date
from datetime import datetime
from google.oauth2.service_account import Credentials

file = 'sanity_messages.csv'
file1 = "table_image.png"
file2 = 'sanity_messages1.csv'
file3 = 'destination.csv'

# start_date = '2025-01-24'
# start_date = datetime.today().strftime('%Y-%m-%d') # today's date

# start_date_obj = datetime.strptime(start_date, '%Y-%m-%d')

# if start_date_obj.date() != datetime.today().date():
#     today_date_obj = datetime.combine(start_date_obj.date(), datetime.max.time())
# else:
#     today_date_obj = datetime.now()

given_date_str = sys.argv[1]  # e.g. "2025-07-08"
# Parse to datetime if needed
given_date = datetime.strptime(given_date_str, "%Y-%m-%d")

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

# channel_id_gk = 'C07G6R3FUDU' # test-gk-channel
team_name = "Platform"
channel_id_gk = "C07SUNJ3ZEV" # cms-sanity-report

# Initialize Slack client
client = WebClient(token=slack_token)

# Define the CSV file
csv_file = 'sanity_messages.csv'

# Dictionary to track first lines and their associated second lines
message_dict = {}

def fetch_team_name(start_date, end_date=None):
    team_name = "Unknown"
    try:
        cursor = None
        start_timestamp = str(start_date.timestamp())
        end_timestamp = str(end_date.timestamp()) if end_date else None

        while True:
            response = client.conversations_history(
                channel=channel_id,
                limit=100,
                cursor=cursor,
                oldest=start_timestamp, 
                latest=end_timestamp,
                timeout=120
            )

            messages = response['messages']
            cursor = response.get('response_metadata', {}).get('next_cursor', None)

            for message in messages:
                message_date_obj = datetime.fromtimestamp(float(message['ts']))

                if end_date and not (start_date <= message_date_obj <= end_date):
                    continue
                elif not end_date and message_date_obj < start_date:
                    continue

                # Get top-level text or extract from blocks
                text = message.get('text', '')

                if not text and 'blocks' in message:
                    try:
                        text_parts = []
                        for block in message['blocks']:
                            if block['type'] == 'rich_text':
                                for element in block.get('elements', []):
                                    if element['type'] == 'rich_text_section':
                                        for sub_element in element.get('elements', []):
                                            if sub_element.get('type') == 'text':
                                                text_parts.append(sub_element.get('text', ''))
                        text = ' '.join(text_parts).strip()
                    except Exception as e:
                        print("Error extracting text from blocks:", e)
                        continue

                # Extract team name
                match = re.search(r'Sanity triggered for Team -\s*([A-Za-z0-9_]+)', text, re.IGNORECASE)
                if match:
                    team_name = match.group(1)
                    # print(f"✅ Found team: {team_name}")
                    return team_name

            if not cursor:
                break

        print("❌ No team name found.")
        return "Unknown"

    except SlackApiError as e:
        print(f"Slack API error: {e.response['error']}")
        return "Unknown"
    except Exception as e:
        print(f"Error: {e}")
        return "Unknown"
    return team_name

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
                limit=100,
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


# start_date = datetime(2025, 7, 29, 5, 30)
# fetch_messages(start_date)

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

# *******************************

# Remove Timestamp columns

# Load the CSV file into a DataFrame
df = pd.read_csv(csv_file)

# Remove columns that contain 'Timestamp' in their name
df = df.loc[:, ~df.columns.str.contains('Timestamp')]

# Save the cleaned DataFrame to a new CSV file
df.to_csv(csv_file, index=False)

# Get the current number of columns
num_columns = df.shape[1]
# print(num_columns)

# Define the base name for the first column
base_column_name = "Sanity"
base_column_name_next = "TIBS"

# Define the dynamic column naming logic
if num_columns > 7:
    # If more than 6 columns, trim to the first 6
    df = df.iloc[:, :7]
    num_columns = 7  # Update to reflect the trimmed columns

# Dynamically generate column names based on the number of columns
column_names = [base_column_name]  # Start with the base column name
for i in range(1, num_columns):
    if i == 1:  # Add "(latest)" only to the most recent column
        column_names.append(f"TIBS")    
    elif i == 2:  # Add "(latest)" only to the most recent column
        column_names.append(f"Run {num_columns - i}(latest)")
    else:
        column_names.append(f"Run {num_columns - i}")

# print(column_names)
# Assign the generated column names
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

# Apply replacements, except for the second column ('TIBS')
for col in df.columns:
    if col != 'TIBS':  # Skip the 'TIBS' column
        df[col] = df[col].replace(replacements, regex=True)

# df.fillna("", inplace=True)
df.to_csv(csv_file, index=False)

# Load the CSV file
df = pd.read_csv(csv_file)

# Define the order list, ensure there are no leading/trailing spaces
all_order = [
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
    "Global Dashboard Sanity - UI",
    "Global Dashboard Sanity - Parameterized - UI",
    "Notifications Sanity - UI",
    "OneClickTrial - UI",
    "Top Level Navigation Smoke Test - UI",
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

cda_order = [
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
    "BulkDelete and Release20 - API",
    "Search Full Sanity - API",
    "Search Variants Sanity - API",
    "Variants Sanity - API",
    "CDA Full Sanity - API",
    "SDK-JS-CDA Sanity", 
    "SDK-JS-CMA Sanity",
    "SDK-Marketplace Sanity",
    "SDK-Dotnet-CDA Sanity",
    "SDK-Typescript-CDA Sanity",
    "SDK-Python-CDA Sanity"
]

ui_order = [
    "RTE Full Sanity - UI",
    "Full Sanity - UI",
    "Assets Full Sanity - UI",
    "Search Full Sanity - UI",
    "Taxonomy Full Sanity - UI",
    "Variants Full Sanity - UI",
    "Extensions Full Sanity - UI",
    "Releases20 Full Sanity - UI",
    "AutoDraft Full Sanity - UI",
    "Timeline Preview Sanity - UI",
    "Live Preview Sanity - UI",
    "Visual Builder E2E - UI",
    "CSI AI Assistant - E2E",
    "CSI Brand Kit - E2E",
    "Orgadmin Sanity - UI",
    "Auth background jobs events - UI",
    "Webhook Test Suite - UI",
    "Argus Basic Sanity - UI",
    "Global Dashboard Sanity - UI",
    "OneClickTrial - UI",
    "Top Level Navigation Smoke Test - UI",
    "Notifications Sanity - UI",
    "AssetManagement20 - API",
    "Asset Managment Test - UI",
    "AssetPicker Full Sanity - UI",
    "Marketplace Sanity - UI",
    "Developerhub Sanity - UI"
]

vb_order = [
    "Rest Preview Service - API",
    "Visual Builder E2E - UI",
]

vp_order = [
    "Rest Preview Service - API",
    "GraphQL Preview Service - API",
    "Timeline Preview Sanity - UI",
    "Live Preview Sanity - UI",
    "Visual Builder E2E - UI",
]

lpsdk_order = [
    "Timeline Preview Sanity - UI",
    "Live Preview Sanity - UI",
    "Visual Builder E2E - UI",
]

csi_order = [
    "RTE Full Sanity - UI",
    "Full Sanity - UI",
    "Assets Full Sanity - UI",
    "SDK-Marketplace Sanity",
    "Visual Builder E2E - UI",
    "CSI AI Assistant - E2E",
    "CSI Brand Kit - E2E",
]

cli_order = [
    "CMA Nested Global Fields - API",
    "Variants Sanity - API",
    "CLI-CMS Sanity",
    "CLI-Marketplace Sanity",
    "CLI-Personalize Sanity",
    "CLI-TSGEN Sanity",
]

sdk_order = [
    "CMA Nested Global Fields - API",
    "Variants Sanity - API",
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
    "SDK-Python-CDA Sanity"
]

team_orders = {
    "All": all_order,
    "CMA": all_order,
    "Platform": all_order,
    "Cloud": all_order,
    "SearchAPI": all_order,
    "UE": ui_order,
    "RTE": ui_order,
    "SearchUI": ui_order,
    "CDA": cda_order,
    "VisualBuilder": vb_order,
    "VisualPreview": vp_order,
    "LivePreviewSDK": lpsdk_order,
    "CSI": csi_order,
    "CLI": cli_order,
    "SDK": sdk_order
}

order = team_orders.get(team_name, [])

# Remove leading/trailing spaces in both order list and 'Sanity' column
order = [o.strip() for o in order]
df['Sanity'] = df['Sanity'].str.strip()

# have only values in order list
df = df[df['Sanity'].isin(order)]

# Find the missing Sanity values (those in the order list but not in the DataFrame)
missing_sanity_values = [sanity for sanity in order if sanity not in df['Sanity'].values]

# Get the second column dynamically (usually Run 2(latest), or whatever it may be)
second_column = df.columns[1]  # This grabs the second column name dynamically

# Create a list of new rows for missing sanity values
new_rows = []
for sanity in missing_sanity_values:
    # Prepare a new row with only the second column updated to 'NoRun'
    new_row = {'Sanity': sanity, second_column: 'NoRun'}
    new_rows.append(new_row)

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

# Run the function
generate_table_image_with_plotly(input_csv, output_image)

# Upload to Slack
client = WebClient(token=slack_token)
today_date = datetime.now().strftime("%d-%b-%Y")
# today_date = '2025-01-13'

# try:
#     response = client.files_upload_v2(
#     channel=channel_id_gk,  # Updated from 'channels' to 'channel'
#     file=output_image,
#     title = f"Dev11 CMS Sanity Results - {team_name} Team",  # No bold here — Slack titles don’t support formatting
#     initial_comment = f"@channel Dev11 CMS Sanity Results for *{team_name} Team* : {today_date}"  # Use * for bold/italics in Slack
#     )
#     # title = f"Dev11 CMS Sanity Results - All Team",  # No bold here — Slack titles don’t support formatting
#     # initial_comment = f"@channel Dev11 CMS Sanity Results for *All Team* : {today_date}"  # Use * for bold/italics in Slack
#     # )

# except SlackApiError as e:
#     print(f"Error uploading image to Slack: {e.response['error']}")

def upload_csv_to_gsheet(csv_file, sheet_id, sheet_name, credentials_path):
    # Authenticate and open the Google Sheet
    creds = Credentials.from_service_account_file(credentials_path, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    client = gspread.authorize(creds)
    sheet = client.open_by_key(sheet_id).worksheet(sheet_name)
    
    # Read CSV data
    with open(csv_file, newline='', encoding='utf-8') as file:
        reader = csv.reader(file)
        data = list(reader)
    
    # Extract first and second column values from CSV
    csv_mapping = {row[0].strip(): row[1].strip() if len(row) > 1 else "" for row in data[1:] if row and row[0].strip()}
    
    # Generate column header with today's date in 'DD-MMM-YYYY' format
    # today = datetime.date.today().strftime('%d-%b')
    # today = date.today().strftime('%d-%b')
    # header = today

    given_date = datetime.strptime(given_date_str, "%Y-%m-%d")
    today = given_date.strftime("%m/%d")
    header = today

    
    # Get the last column index and sheet data
    sheet_data = sheet.get_all_values()
    last_col_index = len(sheet_data[0]) + 1 if sheet_data else 1
    
    # Update header
    # sheet.update_cell(1, last_col_index, header)
    
    # Update the sheet with new column data based on first-column matching
    for i in range(1, len(sheet_data)):
        row_value = sheet_data[i][0].strip() if len(sheet_data[i]) > 0 else ""
        if row_value in csv_mapping:
            value = "✅" if "✅" in csv_mapping[row_value] else "❌" if "❌" in csv_mapping[row_value] else ""
            sheet.update_cell(i + 1, last_col_index, value)
    
    sheet.update_cell(1, last_col_index, header)

    print("CSV data uploaded successfully.")

# Example usage
csv_file_path = "sanity_messages.csv"  # Replace with actual CSV file path
sheet_id = "1rMUn-tPQpQwmj4qbdrOTWipu_sjRU0BCap11tPPFjrM"  # Replace with your Google Sheet ID
credentials_path = "jiraproject-key.json"  # Replace with your credentials JSON file
sheet_name = "FY27SanityStatus"

upload_csv_to_gsheet(csv_file_path, sheet_id, sheet_name, credentials_path)

