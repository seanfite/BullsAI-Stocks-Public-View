import finnhub
import datetime
import requests
from google.cloud import storage
import pytz

# This function is part of a worker service on our vm, polling for stock related news data every 
# weekday at 6am PCT and sending that data to ChatGPT API endpoint for a summarized version. The 
# news data is often too long to pass to ChatGPT in one message, so we have a token limit and a 
# splitting function to break it up into several messages  

FINNHUB_API_KEY = ''
INSTANCE_CONNECTION_NAME = ''
BUCKET_NAME = ''

# List of Dow 30 stock symbols
dow_30_symbols = [
    "AAPL", "AMGN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS", "DOW", 
    "GS", "HD", "HON", "IBM", "INTC", "JNJ", "JPM", "KO", "MCD", "MMM", 
    "MRK", "MSFT", "NKE", "PG", "TRV", "UNH", "V", "VZ", "WBA", "WMT"
]

# Log data printing for debugging
def log_execution():
    """Logs the current execution time to a file."""
    with open("/home/seanfite92/news_data_execution_log.txt", "a") as file:
        file.write(f"Script executed at {datetime.datetime.now(pytz.utc)}\n")

# Retrieve stock news data from Finnhub API
def fetch_stock_news(finnhub_client):
    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=1)
    raw_news_data_dict = {}

    for symbol in dow_30_symbols:
        try:
            raw_news = finnhub_client.company_news(symbol, _from=start_date.strftime('%Y-%m-%d'), to=end_date.strftime('%Y-%m-%d'))
            raw_news_data_dict[symbol] = raw_news
        except Exception as e:
            print(f"Error fetching news data for {symbol}: {e}")

    return raw_news_data_dict

# Clean up and condense stock news data
def clean_stock_news(raw_news_data_dict):
    cleaned_news_data = {}

    for symbol, raw_news_list in raw_news_data_dict.items():
        news_list = []
        for news_item in raw_news_list:
            summary_text = news_item['summary']
            
            # Only append the news if it's not from Zacks.com
            if "Zacks.com offers in-depth financial research" not in summary_text:
                news_list.append(summary_text.strip())
        
        # Store the cleaned news summaries in the dictionary with the symbol as the key
        if news_list:  # Only add symbols with actual news data after filtering
            cleaned_news_data[symbol] = news_list

    return cleaned_news_data

# Convert news data dict to string
def news_data_to_string(news_data):
    """Converts the news_data dictionary to a readable string format."""
    news_strings = []
    
    for symbol, news_list in news_data.items():
        # Combine all news items for a given symbol
        combined_news = f"News for {symbol}:\n" + '\n'.join(news_list)
        news_strings.append(combined_news)
    
    # Combine all symbol-specific news into a single string
    return '\n\n'.join(news_strings)

# Return token count of new string
def get_token_count(text):
    """Return the token count of a text."""
    return len(text.split())

# If news string is more than 3000 words, split into list of strings with length caps 
def break_into_parts(news_data_string, max_words=2800):  
    """Break the news data string into parts, each with a max word count."""
    parts = []
    current_part = ""
    for news_item in news_data_string.split('\n\n'):
        # Check if adding the next news item will exceed the word limit
        if get_token_count(current_part + news_item) > max_words:
            parts.append(current_part.strip())
            current_part = news_item
        else:
            current_part += '\n\n' + news_item
    
    # Add the remaining part
    if current_part:
        parts.append(current_part.strip())
    return parts

# Send message to ChatGPT via API endpoint
def chatGPT(news_data_string):
    """Generate a ChatGPT response based on the provided stock news data."""
    all_summaries = []
    parts = break_into_parts(news_data_string)  
    
    for part in parts:
        combined_message = [
            {"role": "system", "content": part},
            {
                "role": "user",
                "content": "Please consisely summarize this news data and think condensed, no line breaks or "
                "formatting needed, just one concurrent string response, forgo some grammar rules to shorten sentences. "
                "And again, no line breaks. "
            }
        ]
        summary = send_to_chatgpt(combined_message)
        all_summaries.append(summary)
        time.sleep(1)  # Wait for 5 seconds before sending the next request
    # Combine all the summaries into a single string
    return ' '.join(all_summaries)


# Helper function to send wrapper message to ChatGPT API endpoint
def send_to_chatgpt(messages):
    """Send the given messages to the ChatGPT API and return the response."""
    endpoint_url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": "Bearer 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",
        "Content-Type": "application/json"
    }
    data = {
        "model": "gpt-3.5-turbo",  
        "messages": messages
    }
    response = requests.post(endpoint_url, headers=headers, json=data)
    return response.json()["choices"][0]["message"]["content"].strip()

# Connect to Gooogle Cloud bucket
def connect_to_gcs_bucket(bucket_name):
    """Connect to the specified GCS bucket and return the bucket object."""
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    return bucket

# Upload bucket data
def upload_to_gcs(bucket, date, content):
    """Uploads content to the specified GCS bucket organized by date."""
    file_path = f"news-data/{date}/chatGPT_response.txt"
    blob = bucket.blob(file_path)
    blob.upload_from_string(content)

# Read bucket data (for website cloud function, this is just for testing)
def read_from_gcs(bucket, date):
    """Reads content from specific bucket file location"""
    file_path = f"news-data/{date}/chatGPT_response.txt"
    blob = bucket.blob(file_path)
    if blob.exists():
        content = blob.download_as_text()
        print(content)
    else:
        print(f"No file found at {file_path} in the bucket.")

# Delete bucket data 
def delete_from_gcs(bucket, date):
    """Deletes content from the specified GCS bucket organized by date."""
    # Define the path to the file, which follows the same logic you used to upload
    file_path = f"news-data/{date}/chatGPT_response.txt"
    blob = bucket.blob(file_path)
    
    # Delete the blob from the bucket
    if blob.exists():
        blob.delete()
        print(f"Deleted {file_path} from the bucket.")
    else:
        print(f"No file found at {file_path} in the bucket.")

# Maintain 5 days of news data at a time, delete the oldest bucket
def delete_oldest_if_exceeds(bucket, max_days=5):
    """If the bucket has more than max_days of data, delete the oldest one."""
    # List all blobs in the bucket.
    blobs = bucket.list_blobs(prefix="news-data/") 
    # Extract dates from the blob names.
    dates = []
    for blob in blobs:
        date_part = blob.name.split('/')[1]
        try:
            date_obj = datetime.datetime.strptime(date_part, '%Y-%m-%d').date()
            dates.append(date_obj)
        except ValueError:
            continue   
    # Sort the dates.
    dates.sort()  
    # If there are more than max_days, delete the oldest one.
    while len(dates) > max_days:
        oldest_date = dates.pop(0)  # get the oldest date
        file_path = f"news-data/{oldest_date}/chatGPT_response.txt"
        blob = bucket.blob(file_path)
        if blob.exists():
            blob.delete()
            print(f"Deleted {file_path} from the bucket.")

def main():
    """Main function to fetch and store stock data."""
    # log_execution()  # Log the execution time every time this script runs
    # Create an instance of the FinnHub client with the provided API key
    finnhub_client = finnhub.Client(api_key=FINNHUB_API_KEY)  
    # Fetch news data and store in dict object
    news_data = fetch_stock_news(finnhub_client)
    # Format news data removing labels, redundency, and uneeded characters
    cleaned_news_data = clean_stock_news(news_data)
    # Convert news data dict into string
    cleaned_news_data_to_string = news_data_to_string(cleaned_news_data)
    # Send news data to ChatGPT to return condensed summary
    summary = chatGPT(cleaned_news_data_to_string) 
    # Get UTC standard time
    utc_now = datetime.datetime.now(pytz.utc)
    # Retrieve datestamp for bucket storage formatting 
    date = utc_now.date().strftime('%Y-%m-%d')
    # Create bucket object
    bucket = connect_to_gcs_bucket(BUCKET_NAME)
    # Upload news string to bucket 
    upload_to_gcs(bucket, date, summary)
    # Maintain 5 days of stock news in bucket
    delete_oldest_if_exceeds(bucket)
    
    # For debugging and maintanence: delete bucket data
    # delete_from_gcs(bucket, date)
    # For debugging: read bucket data
    # read_from_gcs(bucket, date)

if __name__ == "__main__":
    main()