import json
import finnhub
import datetime
import time
from google.cloud import storage

# This function is part of a worker service on our vm, running M-F to retrieve stock data from Finnhub.
# This data is stored into a Google Cloud Bucket. 

FINNHUB_API_KEY = ''

def get_stock_indexes():
    ticker_symbols = []
    filepath = '/home/seanfite92/SP500DailyStockData/stock_indexes.txt'
    with open(filepath, 'r') as file:
        for line in file:
            # Split each line by comma and extract the first element
            ticker = line.split(',')[0]
            ticker_symbols.append(ticker)
    return ticker_symbols

def fetch_stock_data(finnhub_client, symbols, retry_queue):
    """Retrieve stock data from FinnHub for provided symbols."""
    current_date = datetime.date.today().strftime('%Y-%m-%d')
    stock_stats = {}
    
    for symbol in symbols:
        try:
            stock_data = finnhub_client.quote(symbol)
            if stock_data:
                formatted_data = {
                    'Date': current_date,
                    'Index': symbol,
                    'Open': stock_data['o'],
                    'High': stock_data['h'],
                    'Low': stock_data['l'],
                    'Close': stock_data['c'],
                    'Previous Close': stock_data['pc'],
                    'Change': stock_data['d'],
                    'Percent Change': stock_data['dp']
                }
                stock_stats[symbol] = formatted_data
                print(f"{symbol}:", formatted_data)
            else:
                print(f"No data received for {symbol}")
                retry_queue.append(symbol)

        except Exception as e:
            print(f"Error fetching data for {symbol}: {e}")
            retry_queue.append(symbol)
        time.sleep(1.5)

    return stock_stats

def store_stock_data_to_bucket(data):
    """Store stock data to GCS bucket."""
    bucket_name = "stock-data-sp500"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)

    # Iterate over each stock index in the data
    for index, stock_info in data.items():
        # Create a blob path in the format index/YYYY-MM-DD.json
        blob_path = f"{index}/{stock_info['Date']}.json"
        blob = bucket.blob(blob_path)

        # Upload the stock information
        blob.upload_from_string(json.dumps(stock_info))
        # print(f"Data for {index} stored to GCS at {blob_path}")


def main():
    """Main function to fetch and store stock data."""
    stock_indexes = get_stock_indexes()
    finnhub_client = finnhub.Client(api_key=FINNHUB_API_KEY)  
    batch_size = 30
    retry_queue = [] 

    for i in range(0, len(stock_indexes), batch_size):
        batch = stock_indexes[i:i+batch_size]
        # print(f"Processing batch: {batch}")

        # Fetch stock data for the current batch
        stock_data = fetch_stock_data(finnhub_client, batch, retry_queue)
        store_stock_data_to_bucket(stock_data)
        time.sleep(20)
    
    # Process retry queue
    max_attempts = 3
    for attempt in range(max_attempts):
        if not retry_queue:
            break
        print(f"Retry attempt {attempt + 1} for {len(retry_queue)} stocks")
        failed_stocks = retry_queue[:]
        retry_queue.clear()
        stock_data = fetch_stock_data(finnhub_client, failed_stocks, retry_queue)
        store_stock_data_to_bucket(stock_data)
        time.sleep(20)

if __name__ == "__main__":
    main()