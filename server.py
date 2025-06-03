"""
Author: Oleg Shkolnik
Description: before running the server it's importnant to download ssl from the site https://slproweb.com/products/Win32OpenSSL.html
            and then write this command in the powershell
            & "C:\Program Files\OpenSSL-Win64\bin\openssl.exe" req -x509 -newkey rsa:2048 -keyout server.key -out server.crt -days 365 -nodes -subj "/CN=localhost"
            Server that receives GET and POST requests from client (site)
            and sends appropriate responses with user authentication support.
            Server works with streamer and viewers - recieve stream and resend it to the viewers. 
            It includes different servers for the correct work including udp, tcp, websocket.
Date: 3/06/25
"""

import socket
import os
import sqlite3
import urllib.parse
import hashlib
import re
import json
import time
import struct
import threading
import cv2
import numpy as np
import queue
import websockets
import asyncio
import base64
import datetime
from protocol import StreamProtocol, AuthProtocol
import ssl
import logging

# Storage for WebSocket connections
ws_clients = {}

HOST = '0.0.0.0'
PORT = 80

HTTPS_PORT = 443  # Standard port for HTTPS
SSL_CERT_FILE = 'server.crt'  # Path to certificate
SSL_KEY_FILE = 'server.key'  # Path to private key

TCP_AUTH_SSL = True  # Flag to enable/disable SSL
AUTH_SSL_CERT_FILE = 'server.crt'  # Path to SSL certificate
AUTH_SSL_KEY_FILE = 'server.key'

SITE_FOLDER = 'webroot'
MOVED_URL = '/index.html'
SOCKET_TIMEOUT = 10  # Increased socket timeout for Windows
specific_urls = ['forbidden', 'moved', 'error']
request_error = b"HTTP/1.1 400 Bad Request\r\n\r\n<h1>400 Bad Request</h1>"
types = {
    'tml': 'text/html;charset=utf-8',
    'css': 'text/css',
    '.js': 'text/javascript; charset=UTF-8',
    'txt': 'text/plain',
    'ico': 'image/x-icon',
    'gif': 'image/jpeg',
    'jpg': 'image/jpeg',
    'png': 'image/jpeg'
}

protected_pages = [
    'search-stream.html',
    'profile.html',
    'start-stream.html',
    'profile.html',
    'profile-true.html'
]

# TCP port for authentication
TCP_AUTH_PORT = 5050
# UDP port for streaming
PORT_UDP = 5001
PORT_CLIENT = 5002
ws_port = 8765

# Global variables
frame = None
frame_lock = threading.Lock()
clients = []
clients_lock = threading.Lock()
running = True

# Queue for broadcast delivery
broadcast_queue = queue.Queue(maxsize=100)

# Global variable to track active streamer
current_streamer = None
streamer_disconnected = threading.Event()


def setup_logging():
    """
    Sets up logging configuration for the server
    """
    # Create logs directory if it doesn't exist
    if not os.path.exists('logs'):
        os.makedirs('logs')

    # Configure logging format
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'

    # Set up file handler (logs to file)
    file_handler = logging.FileHandler(
        f'logs/server_{datetime.datetime.now().strftime("%Y%m%d")}.log'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format))

    # Set up console handler (logs to terminal)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format))

    # Create logger
    log = logging.getLogger('HTTPServer')
    log.setLevel(logging.DEBUG)

    # Add handlers to logger
    log.addHandler(file_handler)
    log.addHandler(console_handler)

    return log


# Initialize logger at module level
logger = setup_logging()


class StreamManager:
    """Class for managing streams and streamer state"""

    def __init__(self, db_name='user_database.db'):
        self.db_name = db_name
        self.current_streamer = None
        self.streamer_disconnected = threading.Event()
        self.broadcast_queue = queue.Queue(maxsize=100)
        self.ws_clients = {}
        self.frame = None
        self.frame_lock = threading.Lock()

    def is_streaming(self, username):
        """Checks if user is streaming"""
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM active_streams WHERE username = ?', (username,))
            result = cursor.fetchone() is not None
            conn.close()
            logger.debug(f"Checking stream for {username}: {result}")
            return result
        except Exception as e:
            logger.error(f"Error checking stream status: {e}")
            return False

    def add_user_to_active_streams(self, username):
        """Adds user to active streams table"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM active_streams WHERE username = ?", (username,))
            cursor.execute("INSERT INTO active_streams (username, start_time) VALUES (?, ?)",
                           (username, datetime.datetime.now()))
            conn.commit()
            logger.info(f"User {username} added to active streams")
        except sqlite3.Error as e:
            logger.error(f"Error adding to active streams: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()

    def cleanup_database(self, username):
        """Removes user from active streams table"""
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM active_streams WHERE username = ?", (username,))
            conn.commit()
            logger.info(f"User {username} removed from active streams")
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Error removing from active streams: {e}")

    def cleanup_after_streamer(self):
        """Cleanup after streamer disconnection"""

        # Clear broadcast queue
        while not self.broadcast_queue.empty():
            try:
                self.broadcast_queue.get_nowait()
            except queue.Empty:
                pass

        # Clear frame
        with self.frame_lock:
            self.frame = None

        # Send notification to clients
        if self.current_streamer and self.current_streamer in self.ws_clients:
            for client in self.ws_clients[self.current_streamer][:]:
                try:
                    asyncio.run_coroutine_threadsafe(
                        send_ws_message(client, json.dumps({
                            "type": "info",
                            "message": "Streamer disconnected, broadcast ended"
                        })),
                        loop
                    )
                except Exception as e:
                    logger.error(f"Error sending notification: {e}")

        self.current_streamer = None


class SessionManager:
    """Class for managing user sessions"""

    def __init__(self, db_name='user_database.db'):
        self.db_name = db_name
        self.active_sessions = {}

    def create_session(self, username):
        """Creates a new session for user"""
        session_id = hashlib.md5(f"{username}:{time.time()}".encode()).hexdigest()
        self.active_sessions[session_id] = username
        return session_id

    def get_session_user(self, request):
        """Extracts username from session"""
        cookie_match = re.search(r'Cookie:([^\r\n]+)', request, re.IGNORECASE)
        if not cookie_match:
            return None

        cookie_header = cookie_match.group(1).strip()

        cookies = {}
        for item in cookie_header.split(';'):
            if '=' in item:
                key, value = item.strip().split('=', 1)
                cookies[key] = value

        session_id = cookies.get('session_id')

        if session_id and session_id in self.active_sessions:
            return self.active_sessions[session_id]

        return None

    def remove_session(self, session_id):
        """Removes session"""
        if session_id in self.active_sessions:
            del self.active_sessions[session_id]
            return True
        return False

    def authenticate_user(self, username, password):
        """Verifies user credentials"""
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()

            hashed_password = hashlib.sha256(password.encode()).hexdigest()
            cursor.execute('SELECT * FROM users WHERE username = ? AND password = ?',
                           (username, hashed_password))
            user = cursor.fetchone()
            conn.close()

            return user is not None
        except Exception as e:
            logger.error(f"Error authenticating user: {e}")
            return False

    def register_user(self, username, password):
        """Registers a new user"""
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()

            cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
            if cursor.fetchone():
                conn.close()
                return False

            hashed_password = hash_password(password)
            cursor.execute('INSERT INTO users (username, password) VALUES (?, ?)',
                           (username, hashed_password))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error registering user: {e}")
            return False


stream_manager = StreamManager()
session_manager = SessionManager()

stream_protocol = StreamProtocol()


def create_ssl_context():
    """Creates and configures SSL context for authentication"""
    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    try:
        ssl_context.load_cert_chain(certfile=AUTH_SSL_CERT_FILE, keyfile=AUTH_SSL_KEY_FILE)
        return ssl_context
    except Exception as e:
        logger.error(f"Error setting up SSL for auth server: {e}")
        logger.warning("Running auth server without SSL")
        return None


def auth_server_thread():
    """Thread to handle TCP authentication with SSL support"""
    global running

    # Create TCP socket for auth
    tcp_auth_socket = AuthProtocol.create_socket(HOST, TCP_AUTH_PORT, tcp=True)
    tcp_auth_socket.listen(5)

    # Setup SSL if enabled
    ssl_context = None
    if TCP_AUTH_SSL:
        ssl_context = create_ssl_context()
        if ssl_context:
            logger.info(f"TCP Authentication server started with SSL on {HOST}:{TCP_AUTH_PORT}")
        else:
            logger.info(f"TCP Authentication server started without SSL on {HOST}:{TCP_AUTH_PORT}")
    else:
        logger.info(f"TCP Authentication server started on {HOST}:{TCP_AUTH_PORT}")

    while running:
        try:
            # Accept connection
            client_socket, addr = tcp_auth_socket.accept()
            logger.debug(f"New auth connection from {addr}")

            # Wrap socket with SSL if enabled
            if TCP_AUTH_SSL and ssl_context:
                try:
                    client_socket = ssl_context.wrap_socket(client_socket, server_side=True)
                    logger.debug(f"SSL handshake successful with {addr}")
                except ssl.SSLError as e:
                    logger.warning(f"SSL handshake failed with {addr}: {e}")
                    client_socket.close()
                    continue
                except Exception as e:
                    logger.error(f"SSL error with {addr}: {e}")
                    client_socket.close()
                    continue

            # Process auth in a separate thread
            auth_thread = threading.Thread(
                target=handle_tcp_authentication,
                args=(client_socket, addr)
            )
            auth_thread.daemon = True
            auth_thread.start()
        except Exception as e:
            if running:  # Only log error if not shutting down
                logger.error(f"Error in auth server thread: {e}")

    tcp_auth_socket.close()


def handle_tcp_authentication(client_socket, addr):
    """Handle single client authentication over TCP"""
    global current_streamer

    auth_protocol = AuthProtocol()

    try:
        # Authenticate user
        username = auth_protocol.server_authenticate(
            client_socket,
            auth_function=session_manager.authenticate_user,
            register_function=session_manager.register_user
        )

        if not username:
            logger.warning(f"Authentication failed for {addr}")
            return

        logger.info(f"User {username} successfully authenticated from {addr}")

        # Send UDP port for streaming back to the client
        auth_protocol.send_message(client_socket, str(PORT_UDP))

        # Add user to active streams
        current_streamer = username
        stream_manager.add_user_to_active_streams(username)

        # Notify current streamer is ready for UDP connection
        logger.info(f"Streamer {username} authorized and ready to connect via UDP")

    except Exception as e:
        logger.error(f"Error in TCP authentication handler: {e}")
    finally:
        client_socket.close()


def streamer_listener():
    """Main loop waiting for streamer connection"""
    global running, current_streamer, streamer_disconnected

    logger.info("Streamer UDP connection handler started")

    while running:
        try:
            # Wait and process new streamer via UDP
            # Only after authentication has been completed via TCP
            if current_streamer:
                handle_streamer_udp_connection(current_streamer)

                # Notify all connected clients about new streamer
                if current_streamer and current_streamer in ws_clients:
                    for client in ws_clients[current_streamer][:]:
                        try:
                            asyncio.run_coroutine_threadsafe(
                                send_ws_message(client, json.dumps({
                                    "type": "new_streamer",
                                    "streamer": current_streamer,
                                    "message": "New streamer started broadcasting"
                                })),
                                loop
                            )
                        except Exception as e:
                            logger.error(f"Error sending new streamer notification: {e}")

                # Wait for disconnect signal to move to next streamer
                streamer_disconnected.wait()
                streamer_disconnected.clear()

                # Cleanup before next connection
                stream_manager.cleanup_after_streamer()

                # Reset current streamer
                current_streamer = None

            # Sleep a bit to prevent CPU overload if no streamer
            else:
                time.sleep(0.5)

        except Exception as e:
            logger.error(f"Error in streamer processing loop: {e}")
            time.sleep(1)  # Pause before retry


def handle_streamer_udp_connection(username):
    """Handle UDP streaming connection for authenticated user"""
    global frame, broadcast_queue

    # Clear buffers through protocol
    stream_protocol.clear_buffers()

    # Clear broadcast queue when connecting new streamer
    while not broadcast_queue.empty():
        try:
            broadcast_queue.get_nowait()
        except queue.Empty:
            pass

    # Create UDP socket for streaming
    udp_socket = AuthProtocol.create_socket(HOST, PORT_UDP, tcp=False)

    logger.info(f"UDP server ready for streamer {username} on {HOST}:{PORT_UDP}")

    # Receive stream from streamer who was already authenticated
    receive_stream_data(udp_socket, username)


def receive_stream_data(udp_socket, username):
    """Receive stream data from authorized streamer"""
    global frame, streamer_disconnected

    # Set timeout to detect disconnection
    udp_socket.settimeout(1.0)

    disconnect_counter = 0
    last_packet_time = time.time()

    while running:
        try:
            # Get packet
            data, addr = udp_socket.recvfrom(65535)

            # Reset disconnect counter when data received
            disconnect_counter = 0
            last_packet_time = time.time()

            # Process fragment through protocol
            parsed_data = stream_protocol.process_fragment(data)
            if not parsed_data:
                continue

            data_type, packet_seq, total_fragments, fragment_num, payload = parsed_data

            # Try to assemble full packet
            full_data = stream_protocol.reassemble_fragment(
                data_type, packet_seq, total_fragments, fragment_num, payload
            )

            if full_data:
                if data_type == "V":
                    # Decode video through protocol
                    new_frame = stream_protocol.decode_video_frame(full_data)

                    if new_frame is not None:
                        with frame_lock:
                            frame = new_frame

                        # Add to broadcast queue
                        broadcast_queue.put(("V", full_data, packet_seq))

                elif data_type == "A":
                    # Add audio to broadcast queue
                    broadcast_queue.put(("A", full_data, packet_seq))

        except socket.timeout:
            # Handle timeout - possible streamer disconnection
            disconnect_counter += 1
            current_time = time.time()
            inactive_time = current_time - last_packet_time

            # If no packets for a long time (5 seconds), consider streamer disconnected
            if disconnect_counter >= 5 or inactive_time > 10:
                logger.info(f"Streamer {username} disconnected (no data for {inactive_time:.1f} sec)")
                stream_manager.cleanup_database(username)
                streamer_disconnected.set()  # Signal for next streamer
                break

        except Exception as e:
            logger.error(f"Error receiving stream: {e}")
            # For serious errors, also disconnect streamer
            stream_manager.cleanup_database(username)
            streamer_disconnected.set()
            break


def init_database():
    """
    Creates SQLite database and necessary tables if they don't exist
    """
    conn = sqlite3.connect('user_database.db')
    cursor = conn.cursor()

    # Create users table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )
    ''')

    # Create active streams table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS active_streams (
        username TEXT PRIMARY KEY,
        start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (username) REFERENCES users(username)
    )
    ''')

    conn.commit()
    conn.close()
    logger.info("Database initialized")


def hash_password(password):
    """
    Hashes password using SHA-256
    :param password: password in text form
    :return: hashed password
    """
    return hashlib.sha256(password.encode()).hexdigest()


def choosing_type(filename):
    """
    Function for searching type of file
    :param filename: file which type we want to know
    :return: type
    """
    extension = filename.split('.')[-1]
    if extension == 'html' or extension == 'htm':
        return types['tml']
    elif len(extension) == 3 and extension in types:
        return types[extension]
    else:
        return 'text/plain'


def specific(filename):
    """
    Function checks if we have specific url
    :param filename: specific part
    :return: true or false
    """
    return filename in specific_urls


def searching_url(filename):
    """
    Function that determines what response we need to send on specific url
    :param filename: specific url
    :return: specific response
    """
    response = b''
    if filename == 'forbidden':
        response = b"HTTP/1.1 403 Forbidden\r\n\r\n<h1>403 Forbidden</h1>"
    elif filename == 'moved':
        response = b"HTTP/1.1 302 Moved Temporarily\r\nLocation: " + bytes(MOVED_URL, 'utf-8') + b"\r\n\r\n"
    elif filename == 'error':
        response = b"HTTP/1.1 500 Internal Server Error\r\n\r\n<h1>500 Internal Server Error</h1>"
    return response


def validating_get_request(request):
    """
    Function validates if GET request is correct
    :param request: GET request in type of list
    :return: true if request is correct and false if not
    """
    return request[0] == "GET" and request[2] == "HTTP/1.1"


def validating_post_request(request):
    """
    Function validates if POST request is correct
    :param request: POST request in type of list
    :return: true if request is correct and false if not
    """
    return request[0] == "POST" and request[2] == "HTTP/1.1"


def receive_all(sock, def_size=1024):
    """
    Receives data from client until HTTP request is complete
    :param sock: client socket
    :param def_size: buffer size for reading
    :return: complete request in bytes
    """
    data = b''
    try:
        # Set timeout for socket
        sock.settimeout(SOCKET_TIMEOUT)

        while True:
            chunk = sock.recv(def_size)
            if not chunk:  # Connection closed
                break

            data += chunk

            # Determine end of request
            if b'\r\n\r\n' in data:  # Standard HTTP header separator
                # For GET requests, headers are enough
                if data.startswith(b'GET'):
                    break

                # For POST requests, check if we received entire body
                elif data.startswith(b'POST'):
                    match = re.search(rb'Content-Length:\s*(\d+)', data)
                    if match:
                        content_length = int(match.group(1))
                        headers_end = data.find(b'\r\n\r\n') + 4
                        body_length = len(data) - headers_end

                        if body_length >= content_length:
                            break
                # For other request types also finish reading
                else:
                    break

    except socket.timeout:
        logger.warning("Timeout receiving data from client")
    except Exception as e:
        logger.error(f"Error reading data: {e}")

    return data


def serve_file(client_socket, filename):
    """
    This function opens a file requested from the browser and sends its contents to the client via a socket.
    If the file is not found, a "404 Not Found" message is sent.
    :param client_socket: socket of client
    :param filename: path to the file
    """
    try:
        with open(filename, 'rb') as file:
            content = file.read()
            content_type = choosing_type(filename)
            headers = f"HTTP/1.1 200 OK\r\n"
            headers += f"Content-Type: {content_type}\r\n"
            headers += f"Content-Length: {len(content)}\r\n"
            headers += "Connection: close\r\n\r\n"

            response = headers.encode() + content
            client_socket.sendall(response)
    except FileNotFoundError:
        try:
            with open(os.path.join(SITE_FOLDER, 'imgs', 'error.jpg'), 'rb') as file:
                content = file.read()
                headers = f"HTTP/1.1 404 Not Found\r\n"
                headers += f"Content-Type: {types['jpg']}\r\n"
                headers += f"Content-Length: {len(content)}\r\n"
                headers += "Connection: close\r\n\r\n"

                response = headers.encode() + content
                client_socket.sendall(response)
        except FileNotFoundError:
            # If error file not found, send simple error message
            content = b"<h1>404 Not Found</h1>"
            headers = f"HTTP/1.1 404 Not Found\r\n"
            headers += f"Content-Type: text/html\r\n"
            headers += f"Content-Length: {len(content)}\r\n"
            headers += "Connection: close\r\n\r\n"

            response = headers.encode() + content
            client_socket.sendall(response)


def handle_registration(client_socket, form_data):
    """
    Processes user registration request
    :param client_socket: client socket
    :param form_data: form data
    """
    # Check for required fields
    if 'username' not in form_data or 'password' not in form_data:
        send_error_page(client_socket, "Username and password are required", "register.html")
        return

    username = form_data['username']
    password = form_data['password']

    # Check that username is not empty
    if not username or not password:
        send_error_page(client_socket, "Username and password cannot be empty", "register.html")
        return

    # Try to register user
    if session_manager.register_user(username, password):
        # Successful registration - redirect to success page
        response = b"HTTP/1.1 302 Found\r\n"
        response += b"Location: /registration-success.html\r\n"
        response += b"Content-Length: 0\r\n"
        response += b"Connection: close\r\n\r\n"
        client_socket.sendall(response)
        logger.info(f"User {username} successfully registered")
    else:
        # User already exists
        send_error_page(client_socket, "Username already taken", "register.html", username)


def handle_login(client_socket, form_data):
    """
    Processes user login request
    :param client_socket: client socket
    :param form_data: form data
    """
    # Check for required fields
    if 'username' not in form_data or 'password' not in form_data:
        send_error_page(client_socket, "Username and password are required", "login.html")
        return

    username = form_data['username']
    password = form_data['password']

    # Verify credentials
    if session_manager.authenticate_user(username, password):
        # Create session
        session_id = session_manager.create_session(username)

        # Redirect to stream search page
        response = b"HTTP/1.1 302 Found\r\n"
        response += b"Location: /search-stream.html\r\n"
        response += f"Set-Cookie: session_id={session_id}; Path=/\r\n".encode()
        response += b"Content-Length: 0\r\n"
        response += b"Connection: close\r\n\r\n"
        client_socket.sendall(response)
        logger.info(f"User {username} successfully logged in")
    else:
        # Invalid credentials
        send_error_page(client_socket, "Invalid username or password", "login.html", username)


def send_error_page(client_socket, error_message, template_page, username=""):
    """
    Sends page with error message
    :param client_socket: client socket
    :param error_message: error message text
    :param template_page: template page
    :param username: username for prefilling
    """
    try:
        with open(os.path.join(SITE_FOLDER, template_page), 'r', encoding='utf-8') as file:
            content = file.read()

            # Add error message
            error_html = f'<div class="message error">{error_message}</div>'
            content = content.replace('<div id="message-container"></div>',
                                      f'<div id="message-container">{error_html}</div>')

            # Prefill username field if it exists
            if username:
                content = content.replace('name="username" required>',
                                          f'name="username" value="{username}" required>')

            content_bytes = content.encode('utf-8')
            headers = f"HTTP/1.1 200 OK\r\n"
            headers += f"Content-Type: text/html; charset=utf-8\r\n"
            headers += f"Content-Length: {len(content_bytes)}\r\n"
            headers += "Connection: close\r\n\r\n"

            response = headers.encode('utf-8') + content_bytes
            client_socket.sendall(response)

    except FileNotFoundError:
        # If template file not found, send simple message
        content = f"<h1>Error</h1><p>{error_message}</p>".encode('utf-8')
        headers = f"HTTP/1.1 200 OK\r\n"
        headers += f"Content-Type: text/html; charset=utf-8\r\n"
        headers += f"Content-Length: {len(content)}\r\n"
        headers += "Connection: close\r\n\r\n"

        response = headers.encode('utf-8') + content
        client_socket.sendall(response)


def handle_search_request(client_socket, query_params):
    """
    Processes streamer search request
    :param client_socket: client socket
    :param query_params: query parameters
    """
    search_term = query_params.get('term', [''])[0].lower()
    logger.debug(f"Searching streamers with term: {search_term}")

    # Get list of all registered users from database
    conn = sqlite3.connect('user_database.db')
    cursor = conn.cursor()
    cursor.execute('SELECT username FROM users')
    all_users = [user[0] for user in cursor.fetchall()]
    conn.close()

    # Filter users by search term
    matched_users = [user for user in all_users if search_term in user.lower()]
    # Limit result to three users
    matched_users = matched_users[:3]

    # Form JSON response
    response_data = json.dumps(matched_users).encode('utf-8')

    headers = f"HTTP/1.1 200 OK\r\n"
    headers += f"Content-Type: application/json; charset=utf-8\r\n"
    headers += f"Content-Length: {len(response_data)}\r\n"
    headers += "Connection: close\r\n\r\n"

    response = headers.encode('utf-8') + response_data
    client_socket.sendall(response)
    logger.debug(f"Search result sent: {matched_users}")


def handle_join_stream_request(client_socket, query_params):
    """
    Processes request to join stream
    :param client_socket: client socket
    :param query_params: query parameters
    """
    streamer_name = query_params.get('streamer', [''])[0]
    logger.info(f"Request to join stream of user: {streamer_name}")

    # Check if user exists
    conn = sqlite3.connect('user_database.db')
    cursor = conn.cursor()
    cursor.execute('SELECT username FROM users WHERE username = ?', (streamer_name,))
    user_exists = cursor.fetchone() is not None
    conn.close()

    # Check if stream is active
    is_active = stream_manager.is_streaming(streamer_name)

    # Form JSON response with clearer status indication
    response_data = json.dumps({
        "active": is_active,
        "exists": user_exists,
        "message": "Stream is active" if is_active else "Stream is not active"
    }).encode('utf-8')

    headers = f"HTTP/1.1 200 OK\r\n"
    headers += f"Content-Type: application/json; charset=utf-8\r\n"
    headers += f"Content-Length: {len(response_data)}\r\n"
    headers += "Connection: close\r\n\r\n"

    response = headers.encode('utf-8') + response_data
    client_socket.sendall(response)
    logger.info(f"Stream status sent for {streamer_name}: active = {is_active}, exists = {user_exists}")


def parse_query_string(query_string):
    """
    Parses query string into parameters
    :param query_string: query string
    :return: dictionary of parameters
    """
    if not query_string:
        return {}

    result = {}
    params = query_string.split('&')
    for param in params:
        if '=' in param:
            key, value = param.split('=', 1)
            key = urllib.parse.unquote_plus(key)
            value = urllib.parse.unquote_plus(value)

            if key in result:
                if isinstance(result[key], list):
                    result[key].append(value)
                else:
                    result[key] = [result[key], value]
            else:
                result[key] = [value]

    return result


def parse_post_data(request):
    """
    Extracts data from POST request
    :param request: POST request as string
    :return: dictionary with form data
    """
    form_data = {}
    try:
        # Find boundary between headers and body
        headers_body_separator = '\r\n\r\n'
        if headers_body_separator in request:
            headers, body = request.split(headers_body_separator, 1)
        else:
            return {}

        # Look for Content-Type and Content-Length
        content_type = None
        content_length = 0

        for line in headers.split('\r\n'):
            if line.lower().startswith('content-type:'):
                content_type = line.split(':', 1)[1].strip()
            elif line.lower().startswith('content-length:'):
                try:
                    content_length = int(line.split(':', 1)[1].strip())
                except ValueError:
                    pass

        # Check that request body has sufficient length
        if len(body) < content_length:
            logger.warning(f"Received {len(body)} bytes, expected {content_length}")

        # Process data depending on content type
        if content_type and 'application/json' in content_type:
            # JSON data
            try:
                form_data = json.loads(body)
            except json.JSONDecodeError:
                logger.error("Error parsing JSON data")
        elif content_type and 'application/x-www-form-urlencoded' in content_type:
            # Regular form data
            pairs = body.split('&')
            for pair in pairs:
                if '=' in pair:
                    key, value = pair.split('=', 1)
                    form_data[urllib.parse.unquote_plus(key)] = urllib.parse.unquote_plus(value)

        return form_data

    except Exception as e:
        logger.error(f"Error parsing POST request data: {e}")
        return {}


def change_user_password(username, current_password, new_password):
    """
    Changes user password after verifying current password
    :param username: username
    :param current_password: current password
    :param new_password: new password
    :return: True if password changed successfully, False otherwise
    """
    try:
        # Verify current password
        if not session_manager.authenticate_user(username, current_password):
            return False, "Incorrect current password"

        # Change password
        conn = sqlite3.connect('user_database.db')
        cursor = conn.cursor()

        hashed_password = hash_password(new_password)
        cursor.execute('UPDATE users SET password = ? WHERE username = ?',
                      (hashed_password, username))
        conn.commit()
        conn.close()

        return True, "Password changed successfully"
    except Exception as e:
        logger.error(f"Error changing password: {e}")
        return False, "Server error while changing password"


def handle_change_password(client_socket, form_data, request):
    """
    Processes user password change request
    :param client_socket: client socket
    :param form_data: form data
    :param request: original HTTP request
    """
    logger.info("Processing password change")
    logger.debug(f"Received form data: {form_data}")
    headers = request.split('\r\n')[:10]
    logger.debug(f"Request headers: {headers}")

    # Get cookies from the request
    cookie_header = None
    for line in request.split('\r\n'):
        if line.lower().startswith('cookie:'):
            cookie_header = line
            break

    logger.debug(f"Cookie header: {cookie_header}")

    # Get username from session
    username = session_manager.get_session_user(request)
    logger.debug(f"Extracted username: {username}")

    if not username:
        # If username not found, return error
        error_response = {
            "success": False,
            "message": "Failed to identify user. Please login again."
        }
        send_json_response(client_socket, error_response, 401)
        return

    # Check for required data
    if 'current-password' in form_data and 'new-password' in form_data:
        current_password = form_data['current-password']
        new_password = form_data['new-password']

        success, message = change_user_password(username, current_password, new_password)

        response_data = {
            "success": success,
            "message": message
        }

        send_json_response(client_socket, response_data)
    else:
        # Incomplete form data
        error_response = {
            "success": False,
            "message": "Current and new passwords are required"
        }
        send_json_response(client_socket, error_response, 400)


def handle_logout(client_socket, request):
    """
    Processes account logout request
    :param client_socket: client socket
    :param request: HTTP request
    """
    # Remove session
    cookie_match = re.search(r'Cookie:.*session_id=([^;]+)', request)
    if cookie_match:
        session_id = cookie_match.group(1)
        session_manager.remove_session(session_id)

    # Redirect to login page
    response = b"HTTP/1.1 302 Found\r\n"
    response += b"Location: /login.html\r\n"
    response += b"Set-Cookie: session_id=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT\r\n"
    response += b"Content-Length: 0\r\n"
    response += b"Connection: close\r\n\r\n"
    client_socket.sendall(response)
    logger.info("User logged out and redirected to login page")


def send_json_response(client_socket, data, status_code=200):
    """
    Sends JSON response to client
    :param client_socket: client socket
    :param data: data to send in JSON format
    :param status_code: HTTP status code
    """
    status_text = {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        500: "Internal Server Error"
    }.get(status_code, "OK")

    response_data = json.dumps(data).encode('utf-8')

    headers = f"HTTP/1.1 {status_code} {status_text}\r\n"
    headers += f"Content-Type: application/json; charset=utf-8\r\n"
    headers += f"Content-Length: {len(response_data)}\r\n"
    headers += "Connection: close\r\n\r\n"

    response = headers.encode('utf-8') + response_data
    client_socket.sendall(response)


def handle_client_connections():
    """Accept client connections"""
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client_socket.bind((HOST, PORT_CLIENT))
    logger.info(f"Client UDP server started on {HOST}:{PORT_CLIENT}")

    while running:
        try:
            # Get data from client
            data, client_addr = client_socket.recvfrom(1024)

            # If this is a connection packet
            if data == b'CONNECT':
                with clients_lock:
                    if client_addr not in clients:
                        clients.append(client_addr)
                        logger.info(f"New client: {client_addr}")

        except Exception as e:
            logger.error(f"Error accepting clients: {e}")


async def handle_websocket(websocket, path=None):
    # Default streamer name handling
    streamer_name = ""

    try:
        # Default path handling
        if path is None:
            path = websocket.path if hasattr(websocket, 'path') else ""

        logger.info(f"WebSocket connection request received with path: {path}")

        # Try to extract streamer name from various sources
        try:
            # 1. Try to get from path parameters
            if '?' in path:
                query_string = path.split('?', 1)[1]
                query_params = parse_query_string(query_string)
                if 'streamer' in query_params:
                    streamer_name = query_params['streamer'][0]
                    logger.debug(f"Found streamer in URL params: {streamer_name}")

            # 2. Try to get from request headers if available
            if not streamer_name and hasattr(websocket, 'request_headers'):
                headers = websocket.request_headers
                if 'streamer' in headers:
                    streamer_name = headers['streamer']
                    logger.debug(f"Found streamer in headers: {streamer_name}")

            # 3. Try to get from path directly if it has a simple format
            if not streamer_name and path.startswith('/ws-proxy/'):
                potential_streamer = path.split('/ws-proxy/', 1)[1]
                if potential_streamer:
                    streamer_name = potential_streamer
                    logger.debug(f"Found streamer in path: {streamer_name}")

            # 4. Try to get from initial message (for backward compatibility)
            if not streamer_name:
                try:
                    # Wait for a short time for initial message
                    message = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                    data = json.loads(message)
                    if isinstance(data, dict) and 'action' in data and data['action'] == 'join':
                        streamer_name = data.get('streamer', '')
                        logger.debug(f"Found streamer in initial message: {streamer_name}")
                except (asyncio.TimeoutError, json.JSONDecodeError) as e:
                    logger.debug(f"No initial message received or error: {e}")
        except Exception as e:
            logger.error(f"Error extracting streamer name: {e}")

        logger.info(f"WebSocket connection established for stream: {streamer_name}")

        # Add client to the list for this streamer
        if streamer_name not in ws_clients:
            ws_clients[streamer_name] = []
        ws_clients[streamer_name].append(websocket)

        # Send welcome message and audio initialization signal
        await websocket.send(json.dumps({"type": "info", "message": f"Connected to stream of {streamer_name}"}))
        await websocket.send(json.dumps({"type": "audio_init", "streamer": streamer_name}))

        # Keep connection open
        while True:
            try:
                # Wait for message from client (just to keep connection alive)
                message = await websocket.recv()

                # Process messages from client
                try:
                    data = json.loads(message)
                    if isinstance(data, dict) and data.get('type') == 'request_audio_reinit':
                        # Client requests audio reinitialization
                        await websocket.send(json.dumps({"type": "audio_init", "streamer": streamer_name}))
                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid JSON received from client: {e}")
                except Exception as e:
                    logger.error(f"Error processing client message: {e}")

            except websockets.exceptions.ConnectionClosed:
                break
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        # Remove client on disconnect
        if streamer_name in ws_clients and websocket in ws_clients[streamer_name]:
            ws_clients[streamer_name].remove(websocket)
            if not ws_clients[streamer_name]:
                del ws_clients[streamer_name]


def broadcast_stream():
    """Broadcast media stream to clients via UDP and WebSocket"""
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    while running:
        try:
            # Get data from broadcast queue
            try:
                data_type, payload, timestamp = broadcast_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            # Don't send data if there's no active streamer
            if current_streamer is None:
                continue

            # UDP broadcast (fragmentation)
            if clients:  # If there are UDP clients
                fragments = fragment_data(data_type, payload)

                with clients_lock:
                    for client in clients:
                        try:
                            for fragment in fragments:
                                udp_socket.sendto(fragment, client)
                        except Exception as e:
                            logger.error(f"Error sending to UDP client {client}: {e}")

            # WebSocket broadcast
            if ws_clients:  # If there are WebSocket clients
                try:
                    if data_type == "V":  # Video
                        # Convert frame for WebSocket (JPEG + base64)
                        ws_data = prepare_video_for_ws(payload)
                    elif data_type == "A":  # Audio
                        # Convert audio for WebSocket (base64)
                        ws_data = prepare_audio_for_ws(payload)
                    else:
                        continue

                    # Send to all connected WebSocket clients
                    for streamer, clients_list in list(ws_clients.items()):
                        for client in clients_list[:]:
                            asyncio.run_coroutine_threadsafe(send_ws_message(client, ws_data), loop)

                except Exception as e:
                    logger.error(f"WebSocket broadcast error: {e}")

        except Exception as e:
            logger.error(f"Error in broadcast process: {e}")


def fragment_data(data_type, payload):
    """
    Fragment data for UDP transmission
    :param data_type: data type (V - video, A - audio)
    :param payload: payload
    :return: list of fragments
    """
    max_payload = 1024
    fragments = []
    total_fragments = (len(payload) + max_payload - 1) // max_payload

    for i in range(total_fragments):
        start = i * max_payload
        end = min(start + max_payload, len(payload))
        data = payload[start:end]

        # Form fragment header
        header = struct.pack(
            "!5I",
            ord(data_type[0]),  # Data type as byte
            0,  # Sequence number
            total_fragments,  # Total number of fragments
            i,  # Current fragment number
            0  # Reserved field
        )

        fragments.append(header + data)

    return fragments


def prepare_video_for_ws(payload):
    """
    Prepare video data for sending via WebSocket
    :param payload: raw video data
    :return: JSON string for sending via WebSocket
    """
    np_data = np.frombuffer(payload, dtype=np.uint8)
    img = cv2.imdecode(np_data, cv2.IMREAD_COLOR)

    if img is not None:
        ret, jpeg = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        jpeg_bytes = jpeg.tobytes()
        base64_frame = base64.b64encode(jpeg_bytes).decode('utf-8')

        return json.dumps({
            "type": "video",
            "data": base64_frame
        })
    return None


def prepare_audio_for_ws(payload):
    """
    Prepare audio data for sending via WebSocket
    :param payload: raw audio data
    :return: JSON string for sending via WebSocket
    """
    base64_audio = base64.b64encode(payload).decode('utf-8')

    return json.dumps({
        "type": "audio",
        "data": base64_audio
    })


# Helper function for asynchronous WebSocket message sending
async def send_ws_message(client, message):
    try:
        await client.send(message)
        return True
    except websockets.exceptions.ConnectionClosed:
        # Client disconnected
        logger.debug("Client connection closed")
        return False
    except Exception as e:
        logger.error(f"Error sending WebSocket message: {e}")
        return False


# broadcast function to send message to all clients for a streamer
async def broadcast_to_streamer_clients(streamer_name, message):
    """
    Broadcast a message to all clients connected to a specific streamer
    :param streamer_name: Name of the streamer
    :param message: Message to broadcast (already formatted JSON string)
    """
    if streamer_name not in ws_clients:
        return

    # Track clients to remove (avoid modifying the list while iterating)
    to_remove = []

    # Send to all connected clients
    for client in ws_clients[streamer_name]:
        success = await send_ws_message(client, message)
        if not success:
            to_remove.append(client)

    # Remove disconnected clients
    for client in to_remove:
        if client in ws_clients[streamer_name]:
            ws_clients[streamer_name].remove(client)

    # Clean up empty lists
    if streamer_name in ws_clients and not ws_clients[streamer_name]:
        del ws_clients[streamer_name]


# Define a flexible WebSocket handler
async def ws_handler(websocket, path=None):
    try:
        # Extract path information from the websocket object if available
        if path is None and hasattr(websocket, 'path'):
            path = websocket.path
        elif path is None:
            path = ""  # Default empty path

        await handle_websocket(websocket, path)
    except Exception as e:
        logger.error(f"Error in WebSocket handler: {e}")


async def run_websocket_server():
    global loop
    loop = asyncio.get_event_loop()
    stop = asyncio.Future()
    logger.info(f"Starting WebSocket server on port {ws_port}")

    # Create SSL context for secure WebSocket connections
    ssl_context = None
    if os.path.exists(SSL_CERT_FILE) and os.path.exists(SSL_KEY_FILE):
        try:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(certfile=SSL_CERT_FILE, keyfile=SSL_KEY_FILE)
            logger.info(f"Using SSL for WebSocket server with cert: {SSL_CERT_FILE}")
        except Exception as e:
            logger.error(f"Error loading SSL certificates for WebSocket: {e}")
            logger.warning("WebSocket server will run without SSL")

    # Set proper SSL verification settings
    if ssl_context:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    # Start WebSocket server on the dedicated port
    server = await websockets.serve(
        ws_handler,
        HOST,
        ws_port,
        ssl=ssl_context,
        ping_interval=30,
        ping_timeout=60
    )

    # Start an additional non-SSL WebSocket server if SSL is enabled
    # This ensures we have both secure and non-secure WebSocket endpoints
    if ssl_context:
        non_ssl_ws_port = ws_port + 1  # Use next port for non-SSL
        non_ssl_server = await websockets.serve(
            ws_handler,
            HOST,
            non_ssl_ws_port,
            ssl=None,  # No SSL for this server
            ping_interval=30,
            ping_timeout=60
        )
        logger.info(f"Non-SSL WebSocket server running on ws://{HOST}:{non_ssl_ws_port}")

    logger.info(f"WebSocket server running on {'wss' if ssl_context else 'ws'}://{HOST}:{ws_port}")
    await stop


def start_websocket_server():
    asyncio.set_event_loop(asyncio.new_event_loop())
    wloop = asyncio.get_event_loop()
    wloop.run_until_complete(run_websocket_server())
    wloop.run_forever()


def is_safe_path(base_dir, requested_path):
    """
    Checks if the requested path is inside the base directory
    :param base_dir: base directory (site root)
    :param requested_path: requested path
    :return: True if path is safe, False otherwise
    """
    # Normalize paths for correct comparison
    base_dir = os.path.abspath(base_dir)
    requested_path = os.path.abspath(os.path.join(base_dir, str(requested_path)))

    # Check that requested path starts with base directory
    return requested_path.startswith(base_dir)


def create_self_signed_cert():
    """
    Creates a self-signed SSL certificate if it doesn't exist
    """
    if os.path.exists(SSL_CERT_FILE) and os.path.exists(SSL_KEY_FILE):
        logger.info(f"Using existing SSL certificate: {SSL_CERT_FILE}")
        return

    logger.info("Creating self-signed SSL certificate...")

    # Command to create self-signed certificate
    cmd = f'openssl req -x509 -newkey rsa:2048 -keyout {SSL_KEY_FILE} -out {SSL_CERT_FILE} ' \
          f'-days 365 -nodes -subj "/CN=localhost"'

    try:
        import subprocess
        subprocess.run(cmd, shell=True, check=True)
        logger.info(f"SSL certificate created: {SSL_CERT_FILE}")
    except Exception as e:
        logger.error(f"Error creating SSL certificate: {e}")
        logger.warning("Please create SSL certificate manually using OpenSSL")
        logger.warning(f"Example: {cmd}")


def main():
    # Initialize protocols and database at startup
    global stream_manager, session_manager, running, ws_port, stream_protocol

    # Initialize protocols
    stream_protocol = StreamProtocol()

    init_database()

    # Create SSL certificate if needed
    create_self_signed_cert()

    # Create HTTP server
    http_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # Create HTTPS server
    https_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # Wrap socket in SSL context
    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    try:
        ssl_context.load_cert_chain(certfile=SSL_CERT_FILE, keyfile=SSL_KEY_FILE)
        https_server_socket = ssl_context.wrap_socket(
            https_server_socket, server_side=True
        )
        https_enabled = True
    except Exception as e:
        logger.error(f"Error setting up HTTPS: {e}")
        logger.warning("Running with HTTP only")
        https_enabled = False

    try:
        # Start TCP authentication server thread
        tcp_auth_thread = threading.Thread(target=auth_server_thread)
        tcp_auth_thread.daemon = True
        tcp_auth_thread.start()

        # Start remaining threads
        streamer_thread = threading.Thread(target=streamer_listener)
        streamer_thread.daemon = True
        streamer_thread.start()

        client_thread = threading.Thread(target=handle_client_connections)
        client_thread.daemon = True
        client_thread.start()

        broadcast_thread = threading.Thread(target=broadcast_stream)
        broadcast_thread.daemon = True
        broadcast_thread.start()

        # Define WebSocket port and non-SSL port
        ws_port = 8765
        ws_non_ssl_port = 8766  # This port will be used for non-SSL WebSockets

        # Update global variable to be accessible elsewhere
        import builtins
        setattr(builtins, 'ws_non_ssl_port', ws_non_ssl_port)

        ws_thread = threading.Thread(target=start_websocket_server)
        ws_thread.daemon = True
        ws_thread.start()

        # Configure HTTP server
        http_server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        http_server_socket.bind((HOST, PORT))
        http_server_socket.listen(5)
        logger.info(f"HTTP server started on port {PORT}")

        # Configure HTTPS server if enabled
        if https_enabled:
            https_server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            https_server_socket.bind((HOST, HTTPS_PORT))
            https_server_socket.listen(5)
            logger.info(f"HTTPS server started on port {HTTPS_PORT}")
            logger.info(f"Open browser and go to https://localhost/register.html")
        else:
            logger.info(f"Open browser and go to http://localhost/register.html")

        logger.info(f"WebSocket server runs on ports: secure={ws_port}, non-secure={ws_non_ssl_port}")
        logger.info(f"TCP Authentication server running on port {TCP_AUTH_PORT}")
        logger.info(f"UDP Streaming server running on port {PORT_UDP}")

        # Create list for tracking sockets
        sockets_to_monitor = [http_server_socket]
        if https_enabled:
            sockets_to_monitor.append(https_server_socket)

        while running:
            # Use select to monitor multiple sockets
            import select
            readable, _, exceptional = select.select(sockets_to_monitor, [], sockets_to_monitor, 1.0)

            for s in readable:
                try:
                    client_socket, addr = s.accept()
                    # Separate thread for handling request
                    client_thread = threading.Thread(
                        target=handle_client_request,
                        args=(client_socket, addr, s == https_server_socket)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except Exception as e:
                    logger.error(f"Error accepting connection: {e}")

            for s in exceptional:
                logger.error(f"Socket {s} had an exception")
                sockets_to_monitor.remove(s)
                s.close()
                # If this was one of the server sockets, try to recreate it
                if s == http_server_socket:
                    http_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    http_server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    http_server_socket.bind((HOST, PORT))
                    http_server_socket.listen(5)
                    sockets_to_monitor.append(http_server_socket)
                    logger.info(f"HTTP server restarted on port {PORT}")
                elif https_enabled and s == https_server_socket:
                    https_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    https_server_socket = ssl_context.wrap_socket(
                        https_server_socket, server_side=True
                    )
                    https_server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    https_server_socket.bind((HOST, HTTPS_PORT))
                    https_server_socket.listen(5)
                    sockets_to_monitor.append(https_server_socket)
                    logger.info(f"HTTPS server restarted on port {HTTPS_PORT}")

    except socket.error as err:
        logger.error('Server socket error: ' + str(err))
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
        running = False
    finally:
        http_server_socket.close()
        if https_enabled:
            https_server_socket.close()


def handle_client_request(client_socket, addr, is_https):
    """
    Processes client requests for both HTTP and HTTPS connections
    :param client_socket: client socket
    :param addr: client address
    :param is_https: flag indicating this is an HTTPS connection
    """
    protocol = "HTTPS" if is_https else "HTTP"
    try:
        client_socket.settimeout(SOCKET_TIMEOUT)
        logger.info(f"{protocol} connection established with {addr}")

        raw_request = receive_all(client_socket)
        if raw_request:
            request = raw_request.decode('utf-8', errors='ignore')
            request_lines = request.split('\r\n')

            # Check for empty request
            if not request_lines or not request_lines[0]:
                logger.warning("Received empty request")
                response = request_error
                client_socket.sendall(response)
                return

            request_split = request_lines[0].split()
            logger.info(f"Received {protocol} request: {request_lines[0]}")

            # Check if this is a WebSocket upgrade request
            if len(request_lines) > 1:
                headers = {}
                for line in request_lines[1:]:
                    if line and ': ' in line:
                        key, value = line.split(': ', 1)
                        headers[key.lower()] = value

                if 'upgrade' in headers and headers['upgrade'].lower() == 'websocket':
                    logger.info(f"WebSocket upgrade request detected via {protocol}")
                    handle_websocket_proxy(client_socket, request, is_https)
                    return

            # Check request format validity
            if len(request_split) < 3:
                logger.warning(f"Invalid request format: {request_lines[0]}")
                response = request_error
                client_socket.sendall(response)
                return

            # Process GET requests
            if validating_get_request(request_split):
                if len(request_split) > 1:
                    request_path = request_split[1]

                    # Check for query parameters
                    path_parts = request_path.split('?', 1)
                    filename = path_parts[0][1:]  # Remove leading slash
                    query_string = path_parts[1] if len(path_parts) > 1 else ""

                    # Handle WebSocket proxy path
                    if filename == 'ws-proxy':
                        logger.info("WebSocket proxy path detected")
                        handle_websocket_proxy(client_socket, request, is_https)
                        return

                    if filename == '':
                        filename = 'register.html'  # Start with registration

                    # Fix link in register.html
                    if filename == 'index.html':
                        filename = 'login.html'

                    # Process special API requests
                    if filename == 'search':
                        query_params = parse_query_string(query_string)
                        handle_search_request(client_socket, query_params)
                        return
                    elif filename == 'join-stream':
                        query_params = parse_query_string(query_string)
                        handle_join_stream_request(client_socket, query_params)
                        return
                    elif filename == 'logout':
                        handle_logout(client_socket, request)
                        return

                    # Check if authorization is required for file access
                    if filename in protected_pages:
                        # Get username from session
                        username = session_manager.get_session_user(request)
                        if not username:
                            # If user is not authorized, redirect to login page
                            response = b"HTTP/1.1 302 Found\r\n"
                            response += b"Location: /login.html\r\n"
                            response += b"Content-Length: 0\r\n"
                            response += b"Connection: close\r\n\r\n"
                            client_socket.sendall(response)
                            logger.info(f"Redirecting to login: unauthorized access to {filename}")
                            return

                    filepath = os.path.join(SITE_FOLDER, filename)
                    logger.debug(f"Requested file: {filepath}")

                    if specific(filename):
                        response = searching_url(filename)
                        client_socket.sendall(response)
                    elif is_safe_path(SITE_FOLDER, filename):
                        serve_file(client_socket, filepath)
                    else:
                        # Request to unsafe path - send 403 Forbidden error
                        response = b"HTTP/1.1 403 Forbidden\r\n\r\n<h1>403 Forbidden</h1>"
                        client_socket.sendall(response)
                        logger.warning(f"Forbidden access attempt to unsafe path: {filename}")

            # Process POST requests
            elif validating_post_request(request_split):
                path = request_split[1]
                logger.info(f"Received {protocol} POST request for path: {path}")

                form_data = parse_post_data(request)
                logger.debug(f"Extracted form data: {form_data}")

                if path == '/register':
                    handle_registration(client_socket, form_data)
                elif path == '/login':
                    handle_login(client_socket, form_data)
                elif path == '/change-password':
                    handle_change_password(client_socket, form_data, request)
                else:
                    logger.warning(f"Unknown POST request: {path}")
                    response = request_error
                    client_socket.sendall(response)
            else:
                logger.warning(f"Unsupported request type: {request_split[0]}")
                response = request_error
                client_socket.sendall(response)
        else:
            logger.warning("Received empty request (no data)")
            response = request_error
            client_socket.sendall(response)

    except socket.error as err:
        logger.error(f'Client socket error ({protocol}): {str(err)}')

    except Exception as e:
        logger.error(f"Unhandled exception in {protocol} handler: {e}")
        # Send 500 Internal Server Error for unexpected exceptions
        error_response = b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/html\r\nContent-Length: 35\r\nConnection: close\r\n\r\n<h1>500 Internal Server Error</h1>"
        try:
            client_socket.sendall(error_response)
        except socket.error as err:
            logger.error(f'Client socket error during error response ({protocol}): {str(err)}')

    finally:
        client_socket.close()


def handle_websocket_proxy(client_socket, request, is_https):
    """
    Handles WebSocket upgrade requests coming through the HTTP/HTTPS server
    by implementing the WebSocket protocol directly
    """
    protocol = "HTTPS" if is_https else "HTTP"
    logger.info(f"WebSocket upgrade request detected via {protocol}")

    # Parse the request to get headers
    request_lines = request.split('\r\n')
    headers = {}
    for line in request_lines[1:]:  # Skip the first line which is the request line
        if line and ': ' in line:
            key, value = line.split(': ', 1)
            headers[key.lower()] = value

    # Extract query parameters to get streamer name
    request_path = request_lines[0].split()[1]
    streamer_name = ""
    if '?' in request_path:
        query_string = request_path.split('?', 1)[1]
        query_params = parse_query_string(query_string)
        if 'streamer' in query_params:
            streamer_name = query_params['streamer'][0]
            logger.info(f"WebSocket proxy request for streamer: {streamer_name} via {protocol}")

    # Verify this is a valid WebSocket request
    if 'upgrade' not in headers or headers['upgrade'].lower() != 'websocket':
        logger.warning("Not a valid WebSocket upgrade request")
        response = b"HTTP/1.1 400 Bad Request\r\n\r\n"
        client_socket.sendall(response)
        return

    if 'sec-websocket-key' not in headers:
        logger.warning("WebSocket key missing")
        response = b"HTTP/1.1 400 Bad Request\r\n\r\n"
        client_socket.sendall(response)
        return

    # Create the WebSocket accept key
    ws_key = headers['sec-websocket-key']
    ws_accept = base64.b64encode(hashlib.sha1(
        f"{ws_key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11".encode()
    ).digest()).decode()

    # Send the WebSocket handshake response
    response = (
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + ws_accept.encode() + b"\r\n\r\n"
    )
    client_socket.sendall(response)
    logger.info(f"WebSocket handshake completed via {protocol}")

    # Now the connection is a WebSocket connection
    handle_websocket_connection(client_socket, streamer_name)


def handle_websocket_connection(client_socket, streamer_name):
    """
    Handles an established WebSocket connection
    """
    global ws_clients

    logger.info(f"WebSocket connection established for streamer: {streamer_name}")

    # Register this client
    if streamer_name not in ws_clients:
        ws_clients[streamer_name] = []

    # Use a custom class to track this connection
    class WSClient:
        def __init__(self, socket):
            self.socket = socket
            self.closed = False

        async def send(self, message):
            if self.closed:
                return False
            try:
                # Format message according to WebSocket protocol
                message_bytes = message.encode() if isinstance(message, str) else message

                # Create WebSocket frame
                length = len(message_bytes)
                frame = bytearray()

                # First byte: FIN bit (1) + RSV bits (000) + Opcode (0001 for text)
                frame.append(0x81)  # 10000001

                # Second byte onwards: MASK bit (0) + Payload length
                if length < 126:
                    frame.append(length)
                elif length < 65536:
                    frame.append(126)
                    frame.append((length >> 8) & 0xFF)
                    frame.append(length & 0xFF)
                else:
                    frame.append(127)
                    for k in range(7, -1, -1):
                        frame.append((length >> (k * 8)) & 0xFF)

                # Add payload
                frame.extend(message_bytes)

                # Send frame
                self.socket.sendall(frame)
                return True
            except Exception as err:
                logger.error(f"Error sending WebSocket message: {err}")
                self.closed = True
                return False

    # Create client object and add to clients list
    client = WSClient(client_socket)
    ws_clients[streamer_name].append(client)

    # Send welcome message
    asyncio.run_coroutine_threadsafe(
        client.send(json.dumps({"type": "info", "message": f"Connected to stream of {streamer_name}"})),
        loop
    )

    # Send audio initialization signal
    asyncio.run_coroutine_threadsafe(
        client.send(json.dumps({"type": "audio_init", "streamer": streamer_name})),
        loop
    )

    # Now handle incoming WebSocket frames
    try:
        while True:
            # Read WebSocket frame header
            try:
                header = client_socket.recv(2)
                if not header or len(header) < 2:
                    break

                # Parse header
                fin = (header[0] & 0x80) != 0
                opcode = header[0] & 0x0F
                masked = (header[1] & 0x80) != 0
                payload_length = header[1] & 0x7F

                # Get extended payload length if needed
                if payload_length == 126:
                    payload_length = struct.unpack(">H", client_socket.recv(2))[0]
                elif payload_length == 127:
                    payload_length = struct.unpack(">Q", client_socket.recv(8))[0]

                # Get masking key if frame is masked
                masking_key = client_socket.recv(4) if masked else None

                # Read payload
                payload = client_socket.recv(payload_length)

                # Unmask payload if needed
                if masked and masking_key:
                    unmasked = bytearray(payload_length)
                    for i in range(payload_length):
                        unmasked[i] = payload[i] ^ masking_key[i % 4]
                    payload = bytes(unmasked)

                # Process based on opcode
                if opcode == 0x1:  # Text frame
                    try:
                        message = payload.decode('utf-8')
                        logger.debug(f"Received text message: {message}")

                        # Process message if needed
                        try:
                            data = json.loads(message)
                            if isinstance(data, dict) and data.get('action') == 'ping':
                                # Respond to ping with pong
                                asyncio.run_coroutine_threadsafe(
                                    client.send(json.dumps({"type": "pong"})),
                                    loop
                                )
                        except json.JSONDecodeError:
                            pass
                    except UnicodeDecodeError:
                        logger.error("Error decoding message")

                elif opcode == 0x8:  # Close frame
                    logger.info("Received close frame")
                    break

                elif opcode == 0x9:  # Ping frame
                    # Respond with pong
                    pong_frame = bytearray([0x8A, len(payload)])
                    pong_frame.extend(payload)
                    client_socket.sendall(pong_frame)

                elif opcode == 0xA:  # Pong frame
                    # Just acknowledge
                    pass

            except socket.timeout:
                # Check if client is still connected
                try:
                    # Send a ping frame
                    ping_frame = bytearray([0x89, 0x00])
                    client_socket.sendall(ping_frame)
                except:
                    break

            except Exception as e:
                logger.error(f"Error reading WebSocket frame: {e}")
                break

    except Exception as e:
        logger.error(f"WebSocket connection error: {e}")

    finally:
        # Clean up
        if streamer_name in ws_clients and client in ws_clients[streamer_name]:
            ws_clients[streamer_name].remove(client)
            if not ws_clients[streamer_name]:
                del ws_clients[streamer_name]

        try:
            client_socket.close()
        except:
            pass

        logger.info(f"WebSocket connection closed for streamer: {streamer_name}")


if __name__ == "__main__":
    """
    checking function situations and launching the main
    """
    # basic tests
    assert specific('error')
    assert specific('forbidden')
    assert specific('moved')
    assert not specific('abc')
    assert searching_url('error')
    assert not searching_url('abc')
    assert choosing_type('test.jpg') == types['jpg']
    assert choosing_type('test.html') == types['tml']
    assert validating_get_request(['GET', '/', 'HTTP/1.1', 'headers'])
    assert not validating_get_request(['POST', '/', 'HTTP/1.1', 'headers'])
    assert validating_post_request(['POST', '/', 'HTTP/1.1', 'headers'])
    main()
