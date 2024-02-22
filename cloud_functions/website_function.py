from google.cloud.sql.connector import connector
from google.cloud import storage
from functions_framework import http
import json
import base64
import requests
import datetime

# This function ties together the main processing of our website/project. We access real time
# stock and news data and feed it to a ChatGPT endpoint along with our website user input 
# resulting in financial AI chatbot capabilities.

# Database connection details
INSTANCE_CONNECTION_NAME = ''
DB_USER = ''
DB_PASS = ''
STOCK_DB_NAME = ''
CHATGPT_DB_NAME = ''
BUCKET_NAME = ''

def connect_to_database(databaseName):
    """Connect to the Google Cloud SQL database and return the connection."""
    conn = connector.connect(
        INSTANCE_CONNECTION_NAME,
        "",
        user=DB_USER,
        password=DB_PASS,
        db=databaseName
    )
    return conn

def pubsub_message_retrieval(request):
    """Retrieve the Pub/Sub message that triggers this cloud function."""
    envelope = json.loads(request.data.decode('utf-8'))
    payload = json.loads(base64.b64decode(envelope['message']['data']).decode('utf-8'))
    return payload


def fetch_stock_entries(index):
    """Retrieve all stock history for the provided stock index from a GCS bucket."""
    bucket_name = "stock-data-sp500"  
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)

    # Prefix to filter the files in the bucket
    prefix = f"{index}/"

    # List all files in the directory for the given stock index
    blobs = bucket.list_blobs(prefix=prefix)

    stock_data = []
    for blob in blobs:
        # Read and parse each file's content
        data = json.loads(blob.download_as_string())
        stock_data.append(data)

    number_of_days = len(stock_data)
    return stock_data, number_of_days

def find_stock_data_stats(stock_data, number_of_days, stock_index):
    """Generate statistics for the given stock data and return as a formatted string."""
    average_close = sum([entry["Previous Close"] for entry in stock_data]) / len(stock_data)
    highest_high = max([entry["High"] for entry in stock_data])
    lowest_low = min([entry["Low"] for entry in stock_data])
    average_percent_change = sum([entry["Percent Change"] for entry in stock_data]) / len(stock_data)
    net_change = stock_data[-1]["Previous Close"] - stock_data[0]["Open"]
    positive_days = sum([1 for entry in stock_data if entry["Change"] > 0])
    current_stock_value = stock_data[-1]["Close"]

    stats_string = (f"{stock_index}: Avg Close ${round(average_close, 2)}, "
                f"High ${round(highest_high, 2)}, Low ${round(lowest_low, 2)}, "
                f"Percent Change {round(average_percent_change, 2)}%, "
                f"Change {round(net_change, 2)}, "
                f"Close ${round(current_stock_value, 2)}, "
                f"↑ {positive_days}/{number_of_days}.")
    return stats_string

def connect_to_gcs_bucket(bucket_name):
    """Connect to the specified GCS bucket and return the bucket object."""
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    return bucket

def read_from_gcs(bucket, date):
    """Reads content from specific bucket file location and checks previous dates if today's data is not found."""
    max_attempts = 7  
    attempt_count = 0
    while attempt_count < max_attempts:
        file_path = f"news-data/{date}/chatGPT_response.txt"
        blob = bucket.blob(file_path)
        if blob.exists():
            content = blob.download_as_text()
            return content
        else:
            # Go to the previous day and try again
            date = (datetime.datetime.strptime(date, "%Y-%m-%d") - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
            attempt_count += 1
    
    print(f"No file found for the past {max_attempts} days in the bucket.")

def send_to_chatgpt(messages):
    """Send the given messages to the ChatGPT API and return the response."""
    endpoint_url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": "",
        "Content-Type": "application/json"
    }
    data = {
        "model": "gpt-4",  
        "messages": messages
    }
    response = requests.post(endpoint_url, headers=headers, json=data)
    return response.json()["choices"][0]["message"]["content"].strip()

def chatGPT(stats_string, user_question, number_of_days, news_data):
    """Generate a ChatGPT response based on the user's question and provided stock statistics."""
    combined_message = [
        {
            "role": "system",
            "content": ("Over " + str(number_of_days) + " days of data, we have some simulated stock stats and a question, "
                        "come up with insightful answers to the question given the simulated data.") 
        },
        {
            "role": "user",
            "content": user_question + ". Here are the stats: " + stats_string + "Also take into consideration"
                        "this current stock news: " + news_data
        }
    ]
    print(combined_message)
    return send_to_chatgpt(combined_message)

def store_output_in_gcs(session_id, response_text):
    """Store the given response text in the GCS bucket 'dow_stock_news_data' 
    inside the 'final-output' folder organized by the Flask session ID."""
    bucket_name = "dow_stock_news_data"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)   
    # Store the response text in a file inside the 'final-output' folder named by the Flask session ID
    blob = bucket.blob(f"final-output/{session_id}/chatGPT_response.txt")
    blob.upload_from_string(response_text)

def insert_question(cursor, response_text):
    """Insert the ChatGPT response into the Google SQL database."""
    insert_sql = """
        INSERT INTO questions (`WebsiteOutput`) 
        VALUES (%s)
    """
    cursor.execute(insert_sql, (response_text,))

def print_column_names(cursor):
    """Debugging function: Print column names of the database table."""
    query = "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'websiteOutputMYSQL';"
    cursor.execute(query)
    columns = cursor.fetchall()
    for column in columns:
        print(column[0])

def print_all_stock_advice(cursor):
    """Debugging function: Print all stock advice entries in the database."""
    cursor.execute("SELECT * FROM questions")
    records = cursor.fetchall()
    for row in records:
        print(f"{row[0]}")
    print("Table retrieved successfully!")

def create_questions_table(cursor):
    """Start up table creation utitity function"""
    create_table_sql = """
        CREATE TABLE IF NOT EXISTS questions (
            `WebsiteOutput` LONGTEXT NOT NULL
        );
    """
    cursor.execute(create_table_sql)

@http
def main(request):
    """Main function handling the request."""
    # Connect to the stock and ChatGPT databases
    conn_stock = connect_to_database(STOCK_DB_NAME)
    conn_chatGPT = connect_to_database(CHATGPT_DB_NAME)

    try:
        # Create cursors for both databases for executing SQL statements
        chatGPTDBCursor = conn_chatGPT.cursor()
        
        # Retrieve user question from the incoming request using Pub/Sub
        data = pubsub_message_retrieval(request)
        user_question = data['user_input']
        selected_stocks = data.get('selected_stocks')
        session_id = data['session_id']
        
        # List to store stock statistics strings
        stats_strings = []
        # If no stock indexes are found in the user's question, get a generic response
        total_number_of_days = 0
        for index in selected_stocks:
            stock_data, number_of_days = fetch_stock_entries(index)
            total_number_of_days += number_of_days
            stats_strings.append(find_stock_data_stats(stock_data, total_number_of_days, index))
            # Combine all the stock stats into a single string
        stats_string_combined = ". ".join(stats_strings)
        
        # Retrieve current stock news data from cloud bucket
        date = datetime.date.today().strftime('%Y-%m-%d')
        bucket = connect_to_gcs_bucket(BUCKET_NAME)
        news_data = read_from_gcs(bucket, date)
        
        # Send wrapper message to ChatGPT endpoint and return a response
        chat_response = chatGPT(stats_string_combined, user_question, number_of_days, news_data)

        # Insert the chatbot's response into the database
        store_output_in_gcs(session_id, chat_response)
        
        # For debugging: Print all stock advice entries in the database
        print_all_stock_advice(chatGPTDBCursor)
        
        # Commit changes made in both databases
        conn_stock.commit()
        conn_chatGPT.commit()
    
    except Exception as e:
        # Handle any errors by printing the exception and rolling back the database changes
        print(f"An error occurred: {e}")
        conn_stock.rollback()
        conn_chatGPT.rollback()
    
    finally:
        # Ensure connections to both databases are closed, regardless of success or failure
        if conn_stock:
            conn_stock.close()
        if conn_chatGPT:
            conn_chatGPT.close()
    
    # Indicate the function executed successfully
    return "Function executed successfully!", 200
    
# If the script is executed directly (not imported), run the main function
if __name__ == "__main__":
    main(None)
