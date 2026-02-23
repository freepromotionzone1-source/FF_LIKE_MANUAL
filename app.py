from flask import Flask, request, jsonify
import asyncio
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import binascii
import aiohttp
import requests
import json
import like_pb2
import like_count_pb2
import uid_generator_pb2
import threading
import urllib3
import random
import logging
from datetime import datetime
import os

# Configuration
TOKEN_BATCH_SIZE = 100
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('like_api.log'),
        logging.StreamHandler()
    ]
)

# Global State for Batch Management
current_batch_indices = {}
batch_indices_lock = threading.Lock()

def get_next_batch_tokens(server_name, all_tokens):
    """Get next sequential batch of tokens"""
    if not all_tokens:
        return []
    
    total_tokens = len(all_tokens)
    
    # If we have fewer tokens than batch size, use all available tokens
    if total_tokens <= TOKEN_BATCH_SIZE:
        return all_tokens
    
    with batch_indices_lock:
        if server_name not in current_batch_indices:
            current_batch_indices[server_name] = 0
        
        current_index = current_batch_indices[server_name]
        
        # Calculate the batch
        start_index = current_index
        end_index = start_index + TOKEN_BATCH_SIZE
        
        # If we reach or exceed the end, wrap around
        if end_index > total_tokens:
            remaining = end_index - total_tokens
            batch_tokens = all_tokens[start_index:total_tokens] + all_tokens[0:remaining]
        else:
            batch_tokens = all_tokens[start_index:end_index]
        
        # Update the index for next time
        next_index = (current_index + TOKEN_BATCH_SIZE) % total_tokens
        current_batch_indices[server_name] = next_index
        
        logging.info(f"Server {server_name}: Using batch starting at index {start_index}")
        return batch_tokens

def get_random_batch_tokens(server_name, all_tokens):
    """Alternative method: use random sampling for better distribution"""
    if not all_tokens:
        return []
    
    total_tokens = len(all_tokens)
    
    # If we have fewer tokens than batch size, use all available tokens
    if total_tokens <= TOKEN_BATCH_SIZE:
        return all_tokens.copy()
    
    # Randomly select tokens without replacement
    selected_tokens = random.sample(all_tokens, TOKEN_BATCH_SIZE)
    logging.info(f"Server {server_name}: Selected {TOKEN_BATCH_SIZE} random tokens")
    return selected_tokens

def load_tokens(server_name, for_visit=False):
    """Load tokens from JSON file based on server and type"""
    if for_visit:
        if server_name == "IND":
            path = "token_ind_visit.json"
        elif server_name in {"BR", "US", "SAC", "NA"}:
            path = "token_br_visit.json"
        else:
            path = "token_bd_visit.json"
    else:
        if server_name == "IND":
            path = "token_ind.json"
        elif server_name in {"BR", "US", "SAC", "NA"}:
            path = "token_br.json"
        else:
            path = "token_bd.json"

    # Check if file exists
    if not os.path.exists(path):
        logging.warning(f"Token file {path} not found. Creating empty file...")
        with open(path, "w") as f:
            json.dump([], f)
        return []

    try:
        with open(path, "r") as f:
            tokens = json.load(f)
            if isinstance(tokens, list):
                # Validate token format
                valid_tokens = []
                for t in tokens:
                    if isinstance(t, dict) and "token" in t:
                        valid_tokens.append(t)
                    elif isinstance(t, str):
                        # Convert string tokens to dict format
                        valid_tokens.append({"token": t})
                
                logging.info(f"Loaded {len(valid_tokens)} tokens from {path} for server {server_name}")
                return valid_tokens
            else:
                logging.error(f"Token file {path} is not in the expected list format.")
                return []
    except FileNotFoundError:
        logging.error(f"Token file {path} not found.")
        return []
    except json.JSONDecodeError as e:
        logging.error(f"Token file {path} contains invalid JSON: {e}")
        return []

def encrypt_message(plaintext):
    """Encrypt message using AES CBC mode"""
    key = b'Yg&tc%DEuh6%Zc^8'
    iv = b'6oyZDr22E3ychjM%'
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded_message = pad(plaintext, AES.block_size)
    encrypted_message = cipher.encrypt(padded_message)
    return binascii.hexlify(encrypted_message).decode('utf-8')

def create_protobuf_message(user_id, region):
    """Create protobuf message for like"""
    message = like_pb2.like()
    message.uid = int(user_id)
    message.region = region
    return message.SerializeToString()

def create_protobuf_for_profile_check(uid):
    """Create protobuf message for profile check"""
    message = uid_generator_pb2.uid_generator()
    message.krishna_ = int(uid)
    message.teamXdarks = 1
    return message.SerializeToString()

def enc_profile_check_payload(uid):
    """Encrypt profile check payload"""
    protobuf_data = create_protobuf_for_profile_check(uid)
    encrypted_uid = encrypt_message(protobuf_data)
    return encrypted_uid

async def send_single_like_request(encrypted_like_payload, token_dict, url):
    """Send a single like request asynchronously"""
    token_value = token_dict.get("token", "")
    if not token_value:
        logging.warning("send_single_like_request received an empty or invalid token_dict.")
        return 999

    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {token_value}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB52"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=encrypted_like_payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    logging.warning(f"Like request failed for token {token_value[:10]}... with status: {response.status}")
                return response.status
    except asyncio.TimeoutError:
        logging.warning(f"Like request timed out for token {token_value[:10]}...")
        return 998
    except Exception as e:
        logging.error(f"Exception in send_single_like_request for token {token_value[:10]}...: {e}")
        return 997

async def send_likes_with_token_batch(uid, server_region_for_like_proto, like_api_url, token_batch_to_use):
    """Send likes using a batch of tokens"""
    if not token_batch_to_use:
        logging.warning("No tokens provided in the batch to send_likes_with_token_batch.")
        return []

    # Create the like payload once for all requests
    like_protobuf_payload = create_protobuf_message(uid, server_region_for_like_proto)
    encrypted_like_payload = encrypt_message(like_protobuf_payload)
    
    # Create tasks for all tokens
    tasks = []
    for token_dict_for_request in token_batch_to_use:
        tasks.append(send_single_like_request(encrypted_like_payload, token_dict_for_request, like_api_url))
    
    # Execute all tasks concurrently
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Count successful and failed requests
    successful_sends = sum(1 for r in results if isinstance(r, int) and r == 200)
    failed_sends = len(token_batch_to_use) - successful_sends
    
    logging.info(f"Attempted {len(token_batch_to_use)} like sends from batch. Successful: {successful_sends}, Failed/Error: {failed_sends}")
    return results

def make_profile_check_request(encrypted_profile_payload, server_name, token_dict):
    """Make a profile check request to get like count"""
    token_value = token_dict.get("token", "")
    if not token_value:
        logging.warning("make_profile_check_request received an empty token_dict.")
        return None

    # Select URL based on server
    if server_name == "IND":
        url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
    else:
        url = "https://clientbp.ggblueshark.com/GetPlayerPersonalShow"

    edata = bytes.fromhex(encrypted_profile_payload)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {token_value}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB52"
    }
    
    try:
        response = requests.post(url, data=edata, headers=headers, verify=False, timeout=10)
        response.raise_for_status()
        binary_data = response.content
        decoded_info = decode_protobuf_profile_info(binary_data)
        return decoded_info
    except requests.exceptions.HTTPError as e:
        logging.error(f"HTTP error in make_profile_check_request for token {token_value[:10]}...: {e.response.status_code}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Request error in make_profile_check_request for token {token_value[:10]}...: {e}")
    except Exception as e:
        logging.error(f"Unexpected error in make_profile_check_request: {e}")
    return None

def decode_protobuf_profile_info(binary_data):
    """Decode protobuf profile information"""
    try:
        items = like_count_pb2.Info()
        items.ParseFromString(binary_data)
        return items
    except Exception as e:
        logging.error(f"Error decoding Protobuf profile data: {e}")
        return None

app = Flask(__name__)

@app.route('/', methods=['GET'])
def home():
    """Home endpoint with API info"""
    return jsonify({
        "name": "FreeFire Like API",
        "version": "1.0",
        "endpoints": {
            "/like": "Send likes to a profile (GET)",
            "/token_info": "Check token counts (GET)"
        },
        "usage": "/like?uid=123456789&server_name=IND&random=false"
    })

@app.route('/like', methods=['GET'])
def handle_requests():
    """Main endpoint to handle like requests"""
    uid_param = request.args.get("uid")
    server_name_param = request.args.get("server_name", "").upper()
    use_random = request.args.get("random", "false").lower() == "true"

    # Validate parameters
    if not uid_param:
        return jsonify({"error": "UID parameter is required"}), 400
    
    if not server_name_param:
        return jsonify({"error": "Server name parameter is required"}), 400
    
    if not uid_param.isdigit():
        return jsonify({"error": "UID must be a number"}), 400

    logging.info(f"Processing like request: UID={uid_param}, Server={server_name_param}, Random={use_random}")

    # Load visit token for profile checking
    visit_tokens = load_tokens(server_name_param, for_visit=True)
    if not visit_tokens:
        error_msg = f"No visit tokens loaded for server {server_name_param}."
        logging.error(error_msg)
        return jsonify({"error": error_msg}), 500
    
    # Use the first visit token for profile check
    visit_token = visit_tokens[0] if visit_tokens else None
    
    # Load regular tokens for like sending
    all_available_tokens = load_tokens(server_name_param, for_visit=False)
    if not all_available_tokens:
        error_msg = f"No tokens loaded or token file invalid for server {server_name_param}."
        logging.error(error_msg)
        return jsonify({"error": error_msg}), 500

    logging.info(f"Total tokens available for {server_name_param}: {len(all_available_tokens)}")

    # Get the batch of tokens for like sending
    if use_random:
        tokens_for_like_sending = get_random_batch_tokens(server_name_param, all_available_tokens)
        batch_type = "RANDOM"
    else:
        tokens_for_like_sending = get_next_batch_tokens(server_name_param, all_available_tokens)
        batch_type = "ROTATING"
    
    logging.info(f"Using {batch_type} batch selection for {server_name_param} with {len(tokens_for_like_sending)} tokens")
    
    # Create encrypted payload for profile check
    encrypted_player_uid_for_profile = enc_profile_check_payload(uid_param)
    
    # Get likes BEFORE using visit token
    before_info = make_profile_check_request(encrypted_player_uid_for_profile, server_name_param, visit_token)
    before_like_count = 0
    
    if before_info and hasattr(before_info, 'AccountInfo'):
        before_like_count = int(before_info.AccountInfo.Likes)
        logging.info(f"UID {uid_param} ({server_name_param}): Likes before = {before_like_count}")
    else:
        logging.warning(f"Could not reliably fetch 'before' profile info for UID {uid_param} on {server_name_param}.")

    # Determine the URL for sending likes
    if server_name_param == "IND":
        like_api_url = "https://client.ind.freefiremobile.com/LikeProfile"
    elif server_name_param in {"BR", "US", "SAC", "NA"}:
        like_api_url = "https://client.us.freefiremobile.com/LikeProfile"
    else:
        like_api_url = "https://clientbp.ggblueshark.com/LikeProfile"

    # Send likes using the token batch
    if tokens_for_like_sending:
        logging.info(f"Sending likes using {len(tokens_for_like_sending)} tokens to {like_api_url}")
        
        # Create and run async event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(send_likes_with_token_batch(
                uid_param, server_name_param, like_api_url, tokens_for_like_sending
            ))
        except Exception as e:
            logging.error(f"Error in async like sending: {e}")
        finally:
            loop.close()
    else:
        logging.warning(f"Skipping like sending for UID {uid_param} as no tokens available.")
    
    # Get likes AFTER using visit token
    after_info = make_profile_check_request(encrypted_player_uid_for_profile, server_name_param, visit_token)
    after_like_count = before_like_count
    actual_player_uid_from_profile = int(uid_param)
    player_nickname_from_profile = "N/A"

    if after_info and hasattr(after_info, 'AccountInfo'):
        after_like_count = int(after_info.AccountInfo.Likes)
        actual_player_uid_from_profile = int(after_info.AccountInfo.UID)
        if after_info.AccountInfo.PlayerNickname:
            player_nickname_from_profile = str(after_info.AccountInfo.PlayerNickname)
        else:
            player_nickname_from_profile = "N/A"
        logging.info(f"UID {uid_param} ({server_name_param}): Likes after = {after_like_count}")
    else:
        logging.warning(f"Could not reliably fetch 'after' profile info for UID {uid_param} on {server_name_param}.")

    likes_increment = after_like_count - before_like_count
    
    # Determine status code
    if likes_increment > 0:
        request_status = 1  # Success
    elif likes_increment == 0:
        request_status = 2  # No change
    else:
        request_status = 3  # Decreased (unusual)

    response_data = {
        "LikesGivenByAPI": likes_increment,
        "LikesafterCommand": after_like_count,
        "LikesbeforeCommand": before_like_count,
        "PlayerNickname": player_nickname_from_profile,
        "UID": actual_player_uid_from_profile,
        "status": request_status,
        "batch_size": len(tokens_for_like_sending),
        "batch_type": batch_type,
        "timestamp": datetime.now().isoformat(),
        "Note": f"Used visit token for profile check and {batch_type.lower()} batch of {len(tokens_for_like_sending)} tokens for like sending."
    }
    
    logging.info(f"Like request completed for UID {uid_param}: +{likes_increment} likes")
    return jsonify(response_data)

@app.route('/token_info', methods=['GET'])
def token_info():
    """Endpoint to check token counts for each server"""
    servers = ["IND", "BD", "BR", "US", "SAC", "NA"]
    info = {}
    
    for server in servers:
        regular_tokens = load_tokens(server, for_visit=False)
        visit_tokens = load_tokens(server, for_visit=True)
        info[server] = {
            "regular_tokens": len(regular_tokens),
            "visit_tokens": len(visit_tokens)
        }
    
    return jsonify({
        "token_counts": info,
        "timestamp": datetime.now().isoformat()
    })

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    })

if __name__ == '__main__':
    logging.info("Starting FreeFire Like API server on port 5001...")
    app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)
