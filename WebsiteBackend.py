import json
import stripe
import firebase_admin
import pymysql
import pytz
from google.cloud.sql.connector import connector
from flask import Flask, render_template, jsonify, request, session, redirect
from flask_session import Session
from google.cloud import pubsub_v1, storage
from firebase_admin import credentials, auth, firestore
from datetime import datetime, timedelta

# This Flask App website function manages our process for accepting a user message, sending it along
# with the session Flask ID via a Pub/Sub message, and then polling for a final output message from
# a Cloud Bucket

# Constants representing the stock symbols and database configurations
DOW_30_SYMBOLS = [
    'AAPL', 'AMGN', 'CRM', 'GS', 'HON', 'NKE', 'MSFT', 'V', 'BA', 'CAT', 
    'CVX', 'DIS', 'HD', 'JNJ', 'MCD', 'MMM', 'PG', 'TRV', 'UNH', 'VZ', 
    'WBA', 'WMT', 'AXP', 'IBM', 'JPM', 'KO', 'MRK', 'DOW', 'CSCO', 'INTC'
]

cred_path = '/'
cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)


# domain for Stripe connection
YOUR_DOMAIN = '/'

user_tokens = {}
db = firestore.client()

# Initialize the Flask application
app = Flask(__name__)

app.secret_key = ''  
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = False
Session(app)  # <-- Initialize Flask-S

# Database connection details
INSTANCE_CONNECTION_NAME = ''
DB_USER = ''
DB_PASS = ''
STOCK_DB_NAME = ''

# Initialize PubSub client and set the topic path
publisher = pubsub_v1.PublisherClient()
TOPIC_PATH = publisher.topic_path('', '')

def connect_to_database(databaseName):
    """Connect to the Google Cloud SQL database and return the connection."""
    conn = connector.connect(
        INSTANCE_CONNECTION_NAME,
        "pymysql",
        user=DB_USER,
        password=DB_PASS,
        db=databaseName
    )
    return conn

def get_response_from_gcs(session_id):
    """Retrieve the ChatGPT response from the GCS bucket based on Flask session ID."""
    bucket_name = "dow_stock_news_data"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    
    blob_path = f"final-output/{session_id}/chatGPT_response.txt"
    blob = bucket.blob(blob_path)
    
    if blob.exists():
        return blob.download_as_text()
    else:
        return None 

def get_daily_top_stocks_from_gcs():
    """Retrieve the daily top 30 stocks from the GCS bucket."""
    bucket_name = "daily-pick"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    
    # This should match the blob path where you upload the top 30 stocks JSON
    blob_path = "top-30-stocks/daily_top_stocks.json"
    blob = bucket.blob(blob_path)
    
    if blob.exists():
        return json.loads(blob.download_as_text())
    else:
        return None

def get_user_transactions_from_gcs(email):
    """Retrieve all user transaction documentation from the GCS bucket for a given email."""
    bucket_name = "dow_stock_news_data"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    
    # Define the prefix to match the folder structure 
    prefix = f"transaction-documentation/{email}/"
    
    # List all blobs with the given prefix
    blobs = bucket.list_blobs(prefix=prefix) 
    user_transactions = []
    for blob in blobs:
        transaction_content = blob.download_as_text()

        # Parse the transaction content to extract the relevant part
        if "Purchase of" in transaction_content:
            # Extracts the token amount and date from the transaction content
            parts = transaction_content.split(' ')
            token_amount = parts[2]  # Assuming the amount always comes after 'Purchase of'
            date_str = transaction_content.split(' at ')[1].split(' ')[0]
            # Format the date
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            formatted_date = date_obj.strftime('%b %d, %Y')
            parsed_content = f"Purchase {token_amount} tokens {formatted_date}"
        elif "Question asked" in transaction_content:
            # Extracts the date from the question content
            date_str = transaction_content.split(' at ')[1][:10]
            # Format the date
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            formatted_date = date_obj.strftime('%b %d, %Y')
            parsed_content = f"Question asked on {formatted_date}"
        else:
            # Default case if the content does not match expected formats
            parsed_content = "Unknown transaction format"

        user_transactions.append(parsed_content)

    return user_transactions

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

def delete_response_from_gcs(session_id):
    """Delete the ChatGPT response from the GCS bucket based on Flask session ID."""
    bucket_name = "dow_stock_news_data"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    
    blob_path = f"final-output/{session_id}/chatGPT_response.txt"
    blob = bucket.blob(blob_path)
    
    if blob.exists():
        blob.delete()
        return True
    
    return False

def store_transaction_data_to_gcs(email, data):
    """Store user data (purchase/question) to GCS bucket."""
    bucket_name = "dow_stock_news_data"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    blob_path = f"transaction-documentation/{email}/{timestamp}/transaction.json"
    blob = bucket.blob(blob_path)
    
    blob.upload_from_string(json.dumps(data))

def GCSQL_Stock_Data(cursor, index):
    query = """
        SELECT * FROM stocks
        WHERE `Index` = %s
        ORDER BY Date DESC
        LIMIT 1
    """
    cursor.execute(query, (index,))
    record = cursor.fetchone()

    if record:
        # Directly use the record if it's already a dictionary
        return record
    else:
        print(f"No data found for index: {index}")
        return None


def GCS_bucket_back_up(email):
    """Update user-specific back-up bucket folder with current bucket folder contents."""
    try:
        source_bucket_name = "dow_stock_news_data"
        backup_bucket_name = "back_up_dow_stock_transactions"
        storage_client = storage.Client()
        
        source_bucket = storage_client.bucket(source_bucket_name)
        backup_bucket = storage_client.bucket(backup_bucket_name)
        
        prefix = f"transaction-documentation/{email}/"
        blobs = source_bucket.list_blobs(prefix=prefix)

        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        
        for blob in blobs:
            new_blob_name = blob.name.replace(prefix, f"transaction-documentation/{email}-deleted-{timestamp}/")
            new_blob = backup_bucket.blob(new_blob_name)
            new_blob.rewrite(blob)
        return True
    except Exception as e:
        print(f"Error in GCS_bucket_back_up: {e}")
        return False

def GCS_delete_bucket_folder_contents(email):
    """delete email specific bucket folder contents"""
    try:
        bucket_name = "dow_stock_news_data"
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        
        prefix = f"transaction-documentation/{email}/"
        blobs = bucket.list_blobs(prefix=prefix)
        
        for blob in blobs:
            blob.delete()
        
        return True
    except Exception as e:
        print(f"Error in GCS_delete_bucket_folder_contents: {e}")
        return False

def add_tokens_to_user(uid, token_amount):
    """Adding tokens to user account"""
    users_ref = db.collection('users')
    user_doc = users_ref.document(uid).get()
    
    if user_doc.exists:
        current_tokens = user_doc.to_dict().get('tokens', 0)
        users_ref.document(uid).set({'tokens': current_tokens + token_amount}, merge=True)
    else:
        users_ref.document(uid).set({'tokens': token_amount})

def delete_user_from_firebase(uid):
    """Delete user from Firebase using Firebase Admin SDK."""
    try:
        auth.delete_user(uid)
        return True
    
    except Exception as e:
        print(f"Error deleting user from Firebase: {e}")
        return False    

def delete_user_from_firestore(uid):
    """Delete user db data from Firestore"""
    try:
        # Assuming user data is stored in a 'users' collection with UID as document name
        doc_ref = db.collection('users').document(uid)
        doc_ref.delete()
        return True
    except Exception as e:
        print(f"Error in delete_user_from_firestore: {e}")
        return False

def get_stock_indexes():
    stock_indices = []
    filepath = '/home/seanfite92/SP500DailyStockData/symbols.txt'
    
    with open(filepath, 'r') as file:
        for line in file:
            # Split the line by commas and strip whitespace from each stock symbol
            symbols = [symbol.strip() for symbol in line.split(',')]
            stock_indices.extend(symbols)

    return stock_indices

def read_specific_stock_data_from_bucket(selected_stocks, start, limit):
    """Read limited stock data from the GCS bucket for specific stock indexes."""
    bucket_name = "stock-data-sp500"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)

    limited_stock_data = {}
    today = datetime.now().date()

    stock_symbols = [stock['symbol'] for stock in selected_stocks]

    # Limit the selected stocks based on the start index and limit
    limited_selected_stocks = stock_symbols[start:start+limit]

    for stock in limited_selected_stocks:
        stock_data = []
        found_data = False
        for day_back in range(7):
            date_check = today - timedelta(days=day_back)
            prefix = f"{stock}/{date_check.strftime('%Y-%m-%d')}"
            blobs = list(bucket.list_blobs(prefix=prefix))
            for blob in blobs:
                if blob.name.endswith('.json'):
                    json_data = blob.download_as_string()
                    stock_info = json.loads(json_data)
                    stock_data.append(stock_info)
                    found_data = True
                    break  # Break after finding the first valid data
            if found_data:
                break

        if stock_data:
            limited_stock_data[stock] = stock_data
        else:
            print(f"No recent data found for {stock}")
            
    return limited_stock_data

def read_stock_data_from_bucket(start, limit):
    """Read limited stock data from the GCS bucket."""
    bucket_name = "stock-data-sp500"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)

    limited_stock_data = {}
    today = datetime.now().date()
    stock_list = get_stock_indexes()  

    # Select the stocks to process based on the start index and limit
    selected_stocks = stock_list[start:start+limit]

    for stock in selected_stocks:
        stock_data = []
        found_data = False
        for day_back in range(7):
            date_check = today - timedelta(days=day_back)
            prefix = f"{stock}/{date_check.strftime('%Y-%m-%d')}"
            blobs = list(bucket.list_blobs(prefix=prefix))
            for blob in blobs:
                if blob.name.endswith('.json'):
                    json_data = blob.download_as_string()
                    stock_info = json.loads(json_data)
                    stock_data.append(stock_info)
                    found_data = True
                    break  # Break after finding the first valid data
            if found_data:
                break

        if stock_data:
            limited_stock_data[stock] = stock_data
        else:
            print(f"No recent data found for {stock}")

    return limited_stock_data

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/chat')
def chat():
    return render_template('chat.html')

@app.route('/password-reset')
def how_it_works():
    """Render the password-reset page."""
    return render_template('forgot-password.html')

@app.route('/login')
def login():
    """Render the login page."""
    return render_template('login.html')

@app.route('/get-tokens')
def get_tokens():
    """Render the login page."""
    return render_template('get-tokens.html')

@app.route('/sign-up')
def signup():
    """Render the sign-up page."""
    return render_template('sign-up.html')

@app.route('/contact')
def contact():
    """Render the contact page."""
    return render_template('contact.html')

@app.route('/confirmation-email')
def confirmation_email():
    """Render the confirmation email page."""
    return render_template('confirmation-email.html')

@app.route('/news')
def news():
    """Render the news data page."""
    return render_template('news.html')

@app.route('/stocks')
def stocks():
    return render_template('stocks.html')

@app.route('/logged-in')
def logged_in():
    firebase_uid = session.get('firebase_uid')
    if firebase_uid:
        return jsonify({"status": "success", "message": "User is logged in.", "logged_in": True})
    else:
        return jsonify({"status": "failure", "message": "User is not logged in.", "logged_in": False})
    
@app.route('/daily-top-stocks')
def daily_top_stocks():
    firebase_uid = session.get('firebase_uid')
    if firebase_uid:
        try:
            daily_stocks = get_daily_top_stocks_from_gcs()
            if daily_stocks:
                return jsonify(daily_stocks)
            else:
                return jsonify({'status': 'error', 'message': 'No data available for today\'s top 30 stocks.'}), 404
        except Exception as e:
            print(f"Error retrieving top 30 stocks data: {e}")
            return jsonify({'status': 'error', 'message': 'Server error while fetching data.'}), 500
    else:
        return jsonify({'status': 'error', 'message': 'LOGIN TO ACCESS THIS FEATURE'}), 401

@app.route('/stock-data')
def stock_data():
    start = request.args.get('start', default=0, type=int)
    limit = 30  # Number of stocks to return at a time

    firebase_uid = session.get('firebase_uid')
    if firebase_uid:
        try:
            bucket_data = read_stock_data_from_bucket(start, limit)
            return jsonify(bucket_data)
        except Exception as e:
            print(f"Error retrieving stock data: {e}")
            return jsonify({'status': 'error', 'message': 'Server error while fetching data.'}), 500
    else:
        return jsonify({'status': 'error', 'message': '&nbsp;&nbsp;&nbsp;&nbsp;LOGIN TO ACCESS THIS FEATURE'}), 401
    
@app.route('/check-login-status', methods=['POST'])
def check_login_status():
    firebase_uid = session.get('firebase_uid')
    if firebase_uid:
        return jsonify({'status': 'success', 'message': 'User is logged in'}), 200
    else:
        return jsonify({'status': 'error', 'message': 'LOGIN TO ACCESS THIS FEATURE'}), 401
    
@app.route('/specific-stock-data', methods=['POST'])
def specific_stock_data():
    data = request.get_json()
    selected_stocks = data.get('selectedStocks', [])
    start = data.get('start', 0)
    limit = data.get('limit', 30)

    firebase_uid = session.get('firebase_uid')
    if firebase_uid:
        try:
            stock_data = read_specific_stock_data_from_bucket(selected_stocks, start, limit)
            return jsonify(stock_data)
        except Exception as e:
            print(f"Error retrieving stock data: {e}")
            return jsonify({'status': 'error', 'message': 'Server error while fetching data.'}), 500
    else:
        return jsonify({'status': 'error', 'message': '&nbsp;&nbsp;&nbsp;&nbsp;LOGIN TO ACCESS THIS FEATURE'}), 401
    
@app.route('/get-stocks')
def get_stocks():
    stock_data = []
    filepath = '/home/seanfite92/SP500DailyStockData/stock_indexes.txt'
    with open(filepath, 'r') as file:
        for line in file:
            parts = line.split(',')
            if len(parts) >= 3:
                stock_index = parts[0].strip()
                company_name = parts[1].strip()  
                industry = parts[2].strip()
                stock_data.append({'index': stock_index, 'company': company_name, 'industry': industry})
    return jsonify(stock_data)

@app.route('/password-reset')
def password_reset():
    """Render the sign-up page."""
    return render_template('password-reset.html')

@app.route('/disclaimers')
def disclaimer():
    """Render the sign-up page."""
    return render_template('disclaimers.html')

@app.route('/account-settings')
def account_settings():
    """Render the account-settings page."""
    if not session.get('user_authenticated'):
        return redirect('/login')  
    return render_template('account-settings.html')

@app.route('/delete-account')
def delete_account():
    """Render the delete-account page."""
    if not session.get('user_authenticated'):
        return redirect('/login')  
    return render_template('delete-account.html')

@app.route('/get-news-data')
def get_news_data():
    news_data = read_news_from_gcs()
    return news_data

@app.route('/auth')
def auth_account():
    if not session.get('user_authenticated'):
        return redirect('/login')  

@app.route('/create-checkout-session1', methods=['POST'])
def create_checkout_session1():
    """Set Stripe checkout price"""
    if not session.get('user_authenticated'):
        return redirect('/login')  
    price_id = 'price_1OIJjbKBbkKJ7oTaBuV1jSBC'
    try:
        checkout_session = stripe.checkout.Session.create(
            line_items=[
                {
                    'price': price_id,
                    'quantity': 1,
                },
            ],
            mode='payment',
            success_url=YOUR_DOMAIN + 'success?session_id={CHECKOUT_SESSION_ID}&price_option=117',
            cancel_url=YOUR_DOMAIN + 'chat',
            automatic_tax={'enabled': True},
        )
    
    except Exception as e:
        return str(e)

    return redirect(checkout_session.url, code=303)

@app.route('/create-checkout-session2', methods=['POST'])
def create_checkout_session2():
    """Set Stripe checkout price"""
    if not session.get('user_authenticated'):
        return redirect('/login')  
    price_id = 'price_1OIJkLKBbkKJ7oTa59QIhFDE'
    try:
        checkout_session = stripe.checkout.Session.create(
            line_items=[
                {
                    'price': price_id,
                    'quantity': 1,
                },
            ],
            mode='payment',
            success_url=YOUR_DOMAIN + 'success?session_id={CHECKOUT_SESSION_ID}&price_option=134',
            cancel_url=YOUR_DOMAIN + 'chat',
            automatic_tax={'enabled': True},
        )
    
    except Exception as e:
        return str(e)

    return redirect(checkout_session.url, code=303)

@app.route('/create-checkout-session3', methods=['POST'])
def create_checkout_session3():
    """Set Stripe checkout price"""
    if not session.get('user_authenticated'):
        return redirect('/login') 
    price_id = 'price_1OIJkzKBbkKJ7oTaBow1hxkB'
    try:
        checkout_session = stripe.checkout.Session.create(
            line_items=[
                {
                    'price': price_id,
                    'quantity': 1,
                },
            ],
            mode='payment',
            success_url=YOUR_DOMAIN + 'success?session_id={CHECKOUT_SESSION_ID}&price_option=102',
            cancel_url=YOUR_DOMAIN + 'chat',
            automatic_tax={'enabled': True},
        )
    
    except Exception as e:
        return str(e)

    return redirect(checkout_session.url, code=303)

@app.route('/verify-token', methods=['POST'])
def verify_token():
    """Verifying user authentication"""
    token = request.json.get('token')   
    try:
        # Verify the token using Firebase SDK
        decoded_token = auth.verify_id_token(token)
        uid = decoded_token['uid']
        # If token is verified successfully, set the session variable
        session['user_authenticated'] = True
        session['firebase_uid'] = uid  # Store UID in session
        return jsonify({'status': 'success'})
    except Exception as e:
        print(f"Error verifying ID token: {e}")
        return jsonify({'status': 'fail'}), 401

@app.route('/logged-in')
def is_logged_in():
    firebase_uid = session.get('firebase_uid')
    if firebase_uid:
        return jsonify({'authenticated': True}) 
    else:
        return jsonify({'authenticated': False})

@app.route('/auth', methods=['GET'])
def auth_user():
    if not session.get('user_authenticated'):
        return jsonify({'authenticated': False}) 
    return jsonify({'authenticated': True})        
    
@app.route('/get-token-count', methods=['GET'])
def get_token_count():
    """Retrieve token count for user"""
    firebase_uid = session.get('firebase_uid')
    if firebase_uid:
        user_doc = db.collection('users').document(firebase_uid).get()
        if user_doc.exists:
            return jsonify({'status': 'success', 'token_count': user_doc.to_dict().get('tokens', 0)})
        else:
            return jsonify({'status': 'success', 'token_count': 0})
    else:
        print("Firebase UID missing in session.")
        return jsonify({'status': 'fail', 'message': 'Firebase UID missing.'}), 400

@app.route('/get-transaction-history')
def get_transaction_history():
    """Fetch transaction history from GCS bucket"""
    try:
        firebase_uid = session.get('firebase_uid')
        if not firebase_uid:
            return jsonify({"error": "User not authenticated."}), 403
        # Get user's email from Firebase
        user = auth.get_user(firebase_uid)
        email = user.email
        # Retrieve data from your bucket using the user's email
        transaction_data = get_user_transactions_from_gcs(email)
        return jsonify(transaction_data)
    except Exception as e:
        print(e)  
        return jsonify({"error": "Failed to fetch transaction history."}), 500

@app.route('/firestore-initialize', methods=['POST'])
def firestore_initialize():
    data = request.json
    uid = data.get('uid', None)
    terms_accepted = data.get('termsAccepted', False)

    if not uid:
        return jsonify({"error": "Missing UID"}), 400

    users_ref = db.collection('users')
    user_doc = users_ref.document(uid).get()
    
    if not user_doc.exists:
        users_ref.document(uid).set({'tokens': 0, 'termsAccepted': terms_accepted})
        return jsonify({"message": "User added to database with 0 tokens and terms accepted status"}), 201

    return jsonify({"message": "User already exists in database"}), 200

    
@app.route('/submit', methods=['POST'])
def submit_data():
    try:
        data = request.json
        user_input = data['userInput']
        selected_stocks = data['selectedStocks']

        message_data = {
            'user_input': user_input,
            'selected_stocks': selected_stocks,
            'session_id': session.sid
        }

        # Convert the data to a serialized JSON string and then encode it to bytes
        serialized_message = json.dumps(message_data).encode('utf-8')

        # Publishing the serialized message to Pub/Sub
        publish_future = publisher.publish(TOPIC_PATH, serialized_message)

        # Wait for the publish call to complete.
        publish_future.result()

        return jsonify({"message": user_input})
    
    except Exception as e:
        print(f"Error encountered: {e}")
        return jsonify({"message": "Failed to submit data."}), 500

@app.route('/fetch_data', methods=['GET'])
def fetch_data():
    """Fetch ChatGPT response from GCS bucket."""
    try:
        response_text = get_response_from_gcs(session.sid)
        # Delete the response from the bucket after retrieving it 
        if response_text:
            delete_response_from_gcs(session.sid)  
            # Decrement the token count upon successful retrieval from GCS
            firebase_uid = session.get('firebase_uid')
            if firebase_uid:
                user = auth.get_user(firebase_uid)
                store_transaction_data_to_gcs(user.email, f"Question asked at {datetime.now()} by user {user.email}")
                user_doc = db.collection('users').document(firebase_uid).get()
                if user_doc.exists:
                    current_tokens = user_doc.to_dict().get('tokens', 0)
                    if current_tokens > 0:
                        db.collection('users').document(firebase_uid).set({'tokens': current_tokens - 1}, merge=True)       
            return jsonify({"status": "success", "message": response_text})     
        else:
            return jsonify({"status": "pending", "message": "Data not ready yet."})
    
    except Exception as e:
        print(f"Error encountered during data fetch: {e}")
        return jsonify({"status": "error", "message": "Failed to fetch data."}), 500

@app.route('/success')
def payment_success():
    """Handle Stripe payment success"""
    # Define a mapping of price IDs to token amounts
    price_code = request.args.get('price_option') 
    session_id = request.args.get('session_id')

    PRICE_OPTIONS = {
        '117': 1,    # 1 token for price option 117
        '134': 10,   # 10 tokens for price option 134
        '102': 30    # 30 tokens for price option 102
    }

    if price_code in PRICE_OPTIONS:
        token_amount = PRICE_OPTIONS[price_code] 

    if not session_id:
        print("Error: Missing session ID in /success route.") 
        return "Error: Missing session ID.", 400
    
    # Fetch the checkout session to verify the payment
    checkout_session = stripe.checkout.Session.retrieve(session_id)
    if checkout_session.payment_status == "paid":
        firebase_uid = session.get('firebase_uid')
        # Add tokens to the user
        add_tokens_to_user(firebase_uid, token_amount)  
        user = auth.get_user(firebase_uid)

        pacific_tz = pytz.timezone('America/Los_Angeles')
        pacific_time = datetime.now(pacific_tz).strftime("%Y-%m-%d %H:%M:%S")

        # Store transaction data  
        store_transaction_data_to_gcs(user.email, f"Purchase of {token_amount} tokens at {pacific_time} for user {user.email}") 
       
        return redirect('/chat')    
    else:
        print(f"Payment Failed for session ID: {session_id}")  
        return "Payment Failed.", 400


@app.route('/user-settings-data', methods=['GET'])
def user_settings_data():
    """Get user settings data from Firebase"""
    firebase_uid = session.get('firebase_uid')
    if firebase_uid:
        try:
            user = auth.get_user(firebase_uid)
            email = user.email
            user_doc = db.collection('users').document(firebase_uid).get()
            if user_doc.exists:
                tokens = user_doc.to_dict().get('tokens', 0)
                return jsonify({'status': 'success', 'email': email, 'tokens': tokens})
            else:
                return jsonify({'status': 'error', 'message': 'User data not found in Firestore.'}), 404
        except Exception as e:
            print(f"Error retrieving user settings data: {e}")
            return jsonify({'status': 'error', 'message': 'Server error while fetching data.'}), 500
    else:
        print("Firebase UID missing in session.")
        return jsonify({'status': 'error', 'message': 'Not authenticated.'}), 401
    
@app.route('/verify_payment')
def verify_payment():
    """Stripe payment verification"""
    session_id = request.args.get('session_id')
    if not session_id:
        print("Error: Missing session ID in /verify_payment route.")  
        return jsonify(status="error", message="Missing session ID")
    try:
        # Check with Stripe API
        checkout_session = stripe.checkout.Session.retrieve(session_id)
        if checkout_session.payment_status == 'paid': 
            return jsonify(status="success")
        else:
            print(f"Payment verification failed for session ID: {session_id}")  
            return jsonify(status="error", message="Payment not completed")
    except Exception as e:
        print(f"Error in /verify_payment route: {e}")  
        return jsonify(status="error", message=str(e))

@app.route('/sign-out', methods=['POST'])
def sign_out():
    """Handle Firebase user sign out."""
    try:
        if 'user_authenticated' in session:
            session.pop('user_authenticated', None)
        
        if 'firebase_uid' in session:
            session.pop('firebase_uid', None)

        return redirect('/')  

    except Exception as e:
        print(f"Error during sign out: {e}")
        return jsonify({"status": "error", "message": "Failed to sign out."}), 500

@app.route('/delete-user', methods=['POST'])
def delete_user():
    """When user delete accounts, process for deleting profile and backing-up transaction history"""
    firebase_uid = session.get('firebase_uid')
    user = auth.get_user(firebase_uid)
    email = user.email

    # A sequence of steps. If any function returns False, we stop.
    steps = [
        (delete_user_from_firebase, firebase_uid, "Successfully deleted firebase user", "There was an error deleting Firebase user"),
        (delete_user_from_firestore, firebase_uid, "Successfully deleted db user data from Firestore", "There was an error deleting Firestore user db data"),
        (GCS_bucket_back_up, email, "Successfully backed up email specific user transaction bucket", "There was an error backing-up email specific user db data"),
        (GCS_delete_bucket_folder_contents, email, "Successfully deleted transaction bucket file user contents", "There was an error deleting bucket folder user data")
      ]

    for func, arg, success_msg, error_msg in steps:
        if func(arg):
            print(success_msg)
        else:
            print(error_msg)
            return jsonify({"status": "error", "message": error_msg}), 500

    # If all steps succeed
    session.clear()
    return jsonify({"status": "success", "message": "User deletion successful"}), 200

# Entry point for the flask application
if __name__ == "__main__":  
    app.run(host='0.0.0.0', port=8080, debug=False)   