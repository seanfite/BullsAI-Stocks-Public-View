import pandas as pd
import numpy as np
import re
import json
import math
from google.cloud import storage
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
from textblob import TextBlob
from nltk.corpus import stopwords
from nltk.stem import WordNetLemmatizer
from nltk.tokenize import word_tokenize
from google.cloud import storage
from datetime import datetime, timedelta

# Ensure NLTK resources are downloaded
import nltk
nltk.download('punkt')
nltk.download('wordnet')
nltk.download('stopwords')

# This function is part of a worker service on our vm, running daily, retrieving S&P500 stock data and news from our google cloud 
# bucket, cleaning the data, assigning sentiment value from news and pushing it into a random forest machine learning algorithm to 
# output the top 30 stocks that are predicted to perform well. News data is also retrieved in this function and a sentimnet analysis 
# is completed to assign a value to associated stocks, 

def get_stock_indices():
    stock_indices_dict = {}
    filepath = '/home/seanfite92/SP500DailyStockData/stock_indexes.txt'

    
    with open(filepath, 'r') as file:
        for line in file:
            parts = line.split(',')
            if len(parts) >= 2:
                stock_symbol = parts[0].strip()
                company_name = parts[1].strip()
                stock_indices_dict[company_name.lower()] = stock_symbol

    return stock_indices_dict

def read_news_from_gcs():
    """Reads the newest news content from a specific GCS bucket file location."""
    bucket_name = "dow_stock_news_data"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)

    max_attempts = 7
    attempt_count = 0
    date = datetime.utcnow().date()

    while attempt_count < max_attempts:
        file_path = f"news-data/{date}/chatGPT_response.txt"
        blob = bucket.blob(file_path)

        if blob.exists():
            content = blob.download_as_text()
            return content
        else:
            # Go to the previous day and try again
            date = date - timedelta(days=1)
            attempt_count += 1

    return "No news data found for the past 7 days."

def clean_text(text):
    """
    Clean the text data (remove punctuation, lowercase, etc.)
    """
    # Remove non-alphabet characters and lowercase the text
    text = re.sub(r'[^a-zA-Z\s]', '', text, re.I|re.A)
    text = text.lower()
    text = text.strip()

    # Tokenization
    tokens = word_tokenize(text)
    # Filter stopwords
    filtered_tokens = [token for token in tokens if token not in stopwords.words('english')]
    # Lemmatization
    lemmatizer = WordNetLemmatizer()
    lemmatized_text = ' '.join([lemmatizer.lemmatize(token) for token in filtered_tokens])

    return lemmatized_text

def analyze_sentiment(text):
    """
    Perform sentiment analysis on the text and return a polarity score.
    """
    # Using TextBlob for sentiment analysis
    analysis = TextBlob(text)
    # Polarity score: -1 (negative) to 1 (positive)
    return analysis.sentiment.polarity

def segment_news(content):
    """
    Segment the news content into individual news snippets.
    Splits the text at a period followed by a whitespace and a capital letter.
    """
    # Regular expression pattern: period followed by whitespace and uppercase letter
    pattern = r'\.\s+(?=[A-Z])'
    news_snippets = re.split(pattern, content)
    return news_snippets

def prepare_news_data(news_df, stock_indices_dict):
    """
    Clean, perform sentiment analysis, and associate stock indices on news data.
    """
    # Create a new column for stock index
    news_df['stock_index'] = None

    # Clean and analyze sentiment for each news
    for index, row in news_df.iterrows():
        cleaned_text = clean_text(row['news_text'])
        sentiment_score = analyze_sentiment(cleaned_text)

        # Update the DataFrame
        news_df.at[index, 'cleaned_text'] = cleaned_text
        news_df.at[index, 'sentiment_score'] = sentiment_score

        # Check for company names and assign stock index
        for company, stock_index in stock_indices_dict.items():
            if company in cleaned_text:
                news_df.at[index, 'stock_index'] = stock_index
                break  # Stop checking after the first match

    return news_df

def read_limited_stock_data_from_bucket(bucket_name):
    limit=30
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    stock_data_dict = {}
    stock_count = 0
    blobs = bucket.list_blobs()

    for blob in blobs:
        if blob.name.endswith('.json'):
            stock_index = blob.name.split('/')[0]
            
            # Check if the stock is already in the dictionary
            if stock_index not in stock_data_dict:
                stock_count += 1
            
            # If the limit is reached, stop fetching more data
            if stock_count > limit:
                break
            
            # Fetch and store data
            raw_data = blob.download_as_string(client=None)
            stock_data = json.loads(raw_data)
            if stock_index in stock_data_dict:
                stock_data_dict[stock_index].append(stock_data)
            else:
                stock_data_dict[stock_index] = [stock_data]
    
    return stock_data_dict

def create_target_variable(data, look_ahead_days=5):
    data['Date'] = pd.to_datetime(data['Date'])
    data['Future_Close'] = data.groupby('Index')['Close'].shift(-look_ahead_days)
    data['Target'] = ((data['Future_Close'] - data['Close']) / data['Close']) * 100
    data.dropna(subset=['Target'], inplace=True)
    return data

def feature_engineering(data):
    # Simple 5-day Moving Average
    data['MA_5'] = data.groupby('Index')['Close'].transform(lambda x: x.rolling(window=5).mean())

    # Volatility (Standard Deviation of Returns) over 5 days
    data['Volatility_5'] = data.groupby('Index')['Close'].transform(lambda x: x.pct_change().rolling(window=5).std())

    # Rate of Change over 5 days
    data['ROC_5'] = data.groupby('Index')['Close'].transform(lambda x: x.pct_change(periods=5) * 100)

    # Daily Price Change Percentage
    data['Price_Change'] = data.groupby('Index')['Close'].transform(lambda x: x.pct_change() * 100)

    # Calculating the 20-day Moving Average
    data['MA_20'] = data.groupby('Index')['Close'].transform(lambda x: x.rolling(window=20).mean())

    return data

def calculate_percentage_error(y_test, predictions):
    errors = abs(y_test - predictions)
    percentage_errors = (errors / abs(y_test)) * 100
    return np.mean(percentage_errors)

def evaluate_model(y_test, predictions):
    mse = mean_squared_error(y_test, predictions)
    rmse = math.sqrt(mse)
    mae = mean_absolute_error(y_test, predictions)
    r2 = r2_score(y_test, predictions)
    avg_percentage_error = calculate_percentage_error(y_test, predictions)

    print(f"Mean Squared Error: {mse}")
    print(f"Root Mean Squared Error: {rmse}")
    print(f"Mean Absolute Error: {mae}")
    print(f"R-squared: {r2}")
    print(f"Average Percentage Error: {avg_percentage_error}%")

def train_random_forest(data, target_column):
    # Handle NaN values
    data = data.dropna()

    X = data.select_dtypes(include=[np.number])
    if target_column in X.columns:
        X = X.drop(target_column, axis=1)

    y = data[target_column]
    
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    
    model = RandomForestRegressor(n_estimators=100, random_state=42)
    model.fit(X_train, y_train)
    
    predictions = model.predict(X_test)
    mse = mean_squared_error(y_test, predictions)
    print(f"Mean Squared Error: {mse}")

    return model

def upload_daily_top_stocks_to_gcs(top_stocks):
    """Uploads the daily top stocks to Google Cloud Storage, replacing the previous data."""
    bucket_name = 'daily-pick'
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    destination_blob_name = "top-30-stocks/daily_top_stocks.json"  # Consistent file path
    blob = bucket.blob(destination_blob_name)

    # Convert the top stocks list to a JSON string
    top_stocks_data = json.dumps(top_stocks)

    # Upload the top stocks information, replacing any existing file
    blob.upload_from_string(top_stocks_data)
    print(f"Daily top 30 stocks data uploaded to {destination_blob_name} in bucket {bucket_name}.")

def main():

    # Load stock indices
    stock_indices_dict = get_stock_indices()

    # Read news content from GCS
    news_content = read_news_from_gcs()

    # Segment the news content into individual snippets
    news_snippets = segment_news(news_content)

    # Create a DataFrame from the list of snippets
    news_df = pd.DataFrame(news_snippets, columns=['news_text'])

    # Check if DataFrame is empty
    if news_df.empty:
        print("No news data found.")
        return

    # Prepare the news data
    prepared_news_data = prepare_news_data(news_df, stock_indices_dict)

    prepared_news_data.to_csv('prepared_news_data.csv', index=False)
    print("Prepared news data saved.")

    # Average the sentiment scores for each stock index
    average_sentiment = prepared_news_data.groupby('stock_index')['sentiment_score'].mean().reset_index()

    # Create a list to store data for machine learning
    ml_data = []

    # Iterate through the prepared DataFrame
    for index, row in prepared_news_data.iterrows():
        if row['stock_index'] is not None:
            # Append a dictionary for each relevant row
            ml_data.append({
                'stock_index': row['stock_index'],
                'sentiment_score': row['sentiment_score']
                # Add other features if necessary
            })

    # Print the data for confirmation
    for data in ml_data:
        print(data)

    bucket_name = 'stock-data-sp500'

    stock_data_dict = read_limited_stock_data_from_bucket(bucket_name)

    # Convert the dictionary to a DataFrame
    all_stock_data = []
    for stock_data in stock_data_dict.values():
        for data_point in stock_data:
            all_stock_data.append(data_point)
    data = pd.DataFrame(all_stock_data)

    if 'Index' in data.columns and 'stock_index' in average_sentiment.columns:
        data = pd.merge(data, average_sentiment, left_on='Index', right_on='stock_index', how='left')
        data.drop('stock_index', axis=1, inplace=True)

        # Fill NaN values in sentiment_score with 0
        data['sentiment_score'].fillna(0, inplace=True)

        # Print the rows where sentiment scores were merged
        merged_data = data[data['sentiment_score'] != 0]
        for index, row in merged_data.iterrows():
            print(f"Match found for index {row['Index']}:")
            print(f"Sentiment Score: {row['sentiment_score']}")
            print("------")
        else:
            print("Column for merging not found, proceeding without sentiment data.")


    data = data.sort_values(by=['Index', 'Date']) 

    # Feature Engineering
    data = feature_engineering(data)
    data.to_csv('feature_engineered_data.csv', index=False)  # Save to file

    # Create the target variable
    processed_data = create_target_variable(data, look_ahead_days=5)
    
    # Handle NaN values in the dataset
    processed_data = processed_data.dropna()

    # Sort data by date
    # processed_data = processed_data.sort_values(by='Date').copy()

    # New time series split method
    unique_dates = processed_data['Date'].unique()
    num_dates = len(unique_dates)
    split_index = int(num_dates * 0.7)  # Adjust this ratio as needed

    train_data = processed_data[processed_data['Date'] < unique_dates[split_index]]
    test_data = processed_data[processed_data['Date'] >= unique_dates[split_index]]

    print("Training data dates:", train_data['Date'].unique())

    # Separating features and target for training and testing
    X_train = train_data.drop('Target', axis=1).select_dtypes(include=[np.number])
    y_train = train_data['Target']
    X_test = test_data.drop('Target', axis=1).select_dtypes(include=[np.number])
    y_test = test_data['Target']

    print("Testing data dates:", test_data['Date'].unique())

    # Train the RandomForestRegressor and evaluate
    model = RandomForestRegressor(n_estimators=100, random_state=42)
    model.fit(X_train, y_train)
    predictions = model.predict(X_test)
    evaluate_model(y_test, predictions)

    # Add predictions to the test data DataFrame for analysis
    test_data = test_data.copy()
    test_data.loc[:, 'Predicted_Performance'] = predictions
    test_data.to_csv('test_data_with_predictions.csv', index=False)  # Save test data with predictions to file
    
    performance_dict = {}
    for stock in test_data['Index'].unique():
        stock_test_data = test_data[test_data['Index'] == stock]
        y_test_stock = stock_test_data['Target']
        predictions_stock = stock_test_data['Predicted_Performance']

        avg_predicted_performance = stock_test_data['Predicted_Performance'].mean()
        avg_percentage_error_stock = calculate_percentage_error(y_test_stock, predictions_stock)
        
        performance_dict[stock] = avg_predicted_performance

        print(f"\nStock: {stock}")
        print(f"Average Predicted Performance: {avg_predicted_performance:.2f}%")
        print(f"Percentage Error: {avg_percentage_error_stock:.2f}%")
        print("Actual vs Predicted Values:")
        for actual, predicted in zip(y_test_stock, predictions_stock):
            print(f"Actual: {actual:.2f}%, Predicted: {predicted:.2f}%")

    top_30_stocks = sorted(performance_dict, key=performance_dict.get, reverse=True)[:30]
    print(f"\nThe top 30 stocks predicted to perform the best on average are: {top_30_stocks}")
    upload_daily_top_stocks_to_gcs(top_30_stocks)

if __name__ == "__main__":
    main()



