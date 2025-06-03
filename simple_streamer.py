"""
Author: Oleg Shkolnik
Class: יב9
Description: application for streamer that gives him to choose ip and port streamer wants to connect, 
             authenticate and start streaming.
Date: 3/06/25
"""


import socket
import cv2
import sounddevice as sd
import numpy as np
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import PIL.Image, PIL.ImageTk
from protocol import StreamProtocol, AuthProtocol
import ssl
import platform
import logging
import os
import datetime

SERVER_IP = "127.0.0.1"
SERVER_AUTH_PORT = 5050

CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480

AUDIO_RATE = 44100
AUDIO_CHANNELS = 1
AUDIO_CHUNK = 1024
AUDIO_DTYPE = np.int16

stream_protocol = StreamProtocol()
auth_protocol = AuthProtocol()


def setup_logging():
    if not os.path.exists('logs'):
        os.makedirs('logs')

    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'

    file_handler = logging.FileHandler(
        f'logs/streamer_{datetime.datetime.now().strftime("%Y%m%d")}.log'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format))

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format))

    log = logging.getLogger('StreamerApp')
    log.setLevel(logging.DEBUG)

    log.addHandler(file_handler)
    log.addHandler(console_handler)

    return log


logger = setup_logging()


class StreamerApp:
    def __init__(self, root):
        logger.info("Initializing StreamerApp")
        self.root = root
        self.root.title("Video Streamer")
        self.root.geometry("800x600")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.running = False
        self.authenticated = False
        self.username = None
        self.udp_port = None
        self.client_socket = None
        self.cap = None

        self.create_login_frame()
        self.create_stream_frame()

        self.show_login_frame()
        logger.info("StreamerApp initialization completed")

    def create_login_frame(self):
        logger.debug("Creating login frame UI components")
        self.login_frame = ttk.Frame(self.root, padding=20)

        ttk.Label(self.login_frame, text="Server Settings", font=("Arial", 14, "bold")).grid(row=0, column=0,
                                                                                             columnspan=2, pady=10,
                                                                                             sticky='w')

        ttk.Label(self.login_frame, text="Server IP:").grid(row=1, column=0, sticky='w', pady=5)
        self.server_ip_var = tk.StringVar(value=SERVER_IP)
        ttk.Entry(self.login_frame, textvariable=self.server_ip_var, width=30).grid(row=1, column=1, sticky='w', padx=5)

        ttk.Label(self.login_frame, text="Auth Port:").grid(row=2, column=0, sticky='w', pady=5)
        self.auth_port_var = tk.StringVar(value=str(SERVER_AUTH_PORT))
        ttk.Entry(self.login_frame, textvariable=self.auth_port_var, width=30).grid(row=2, column=1, sticky='w', padx=5)

        ttk.Label(self.login_frame, text="Login", font=("Arial", 14, "bold")).grid(row=3, column=0, columnspan=2,
                                                                                   pady=(20, 10), sticky='w')

        ttk.Label(self.login_frame, text="Username:").grid(row=4, column=0, sticky='w', pady=5)
        self.username_var = tk.StringVar()
        ttk.Entry(self.login_frame, textvariable=self.username_var, width=30).grid(row=4, column=1, sticky='w', padx=5)

        ttk.Label(self.login_frame, text="Password:").grid(row=5, column=0, sticky='w', pady=5)
        self.password_var = tk.StringVar()
        ttk.Entry(self.login_frame, textvariable=self.password_var, width=30, show="*").grid(row=5, column=1,
                                                                                             sticky='w', padx=5)

        self.status_var = tk.StringVar(value="Enter your credentials to start streaming")
        ttk.Label(self.login_frame, textvariable=self.status_var, foreground='blue').grid(row=6, column=0, columnspan=2,
                                                                                          pady=10)

        self.login_button = ttk.Button(self.login_frame, text="Login", command=self.authenticate)
        self.login_button.grid(row=7, column=0, columnspan=2, pady=10)
        logger.debug("Login frame created successfully")

    def create_stream_frame(self):
        logger.debug("Creating streaming frame UI components")
        self.stream_frame = ttk.Frame(self.root, padding=10)

        self.video_frame = ttk.Frame(self.stream_frame)
        self.video_frame.pack(fill=tk.BOTH, expand=True, pady=10)

        self.canvas = tk.Canvas(self.video_frame, width=CAMERA_WIDTH, height=CAMERA_HEIGHT, bg="black")
        self.canvas.pack()

        control_frame = ttk.Frame(self.stream_frame)
        control_frame.pack(fill=tk.X, pady=10)

        self.stream_status_var = tk.StringVar(value="Connected as: Not logged in")
        ttk.Label(control_frame, textvariable=self.stream_status_var).pack(side=tk.LEFT, padx=10)

        self.stop_button = ttk.Button(control_frame, text="Disconnect", command=self.stop_streaming)
        self.stop_button.pack(side=tk.RIGHT, padx=10)
        logger.debug("Streaming frame created successfully")

    def show_login_frame(self):
        logger.debug("Switching to login frame")
        self.stream_frame.pack_forget()
        self.login_frame.pack(fill=tk.BOTH, expand=True)

    def show_stream_frame(self):
        logger.debug("Switching to streaming frame")
        self.login_frame.pack_forget()
        self.stream_frame.pack(fill=tk.BOTH, expand=True)

    def authenticate(self):
        logger.info("Starting authentication process")
        server_ip = self.server_ip_var.get()
        auth_port = int(self.auth_port_var.get())

        username = self.username_var.get()
        password = self.password_var.get()

        logger.debug(f"Authentication attempt for user: {username} to server: {server_ip}:{auth_port}")

        if not username or not password:
            logger.warning("Authentication failed: empty username or password")
            messagebox.showerror("Error", "Username and password cannot be empty")
            return

        self.login_button.config(state=tk.DISABLED)
        self.status_var.set("Authenticating...")
        self.root.update()

        auth_thread = threading.Thread(target=self.authentication_thread,
                                       args=(server_ip, auth_port),
                                       daemon=True)
        auth_thread.start()
        logger.debug("Authentication thread started")

    def authentication_thread(self, server_ip, auth_port):
        logger.debug("Authentication thread executing")
        try:
            auth_result, username, udp_port = self.gui_authenticate(server_ip, auth_port)

            if auth_result:
                logger.info(f"Authentication successful for user: {username}, UDP port: {udp_port}")
                self.authenticated = True
                self.username = username
                self.udp_port = udp_port

                self.root.after(0, lambda: self.start_streaming())
            else:
                logger.warning("Authentication failed")
                self.root.after(0, lambda: self.status_var.set("Authentication failed. Try again."))
                self.root.after(0, lambda: self.login_button.config(state=tk.NORMAL))
        except Exception as e:
            logger.error(f"Authentication thread error: {str(e)}", exc_info=True)
            error_msg = f"Authentication error: {str(e)}"
            self.root.after(0, lambda: messagebox.showerror("Error", error_msg))
            self.root.after(0, lambda: self.login_button.config(state=tk.NORMAL))

    def gui_authenticate(self, server_host, server_port):
        logger.debug(f"Starting GUI authentication to {server_host}:{server_port}")
        tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        try:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            tcp_socket = ssl_context.wrap_socket(tcp_socket, server_hostname=server_host)
            logger.debug("SSL context created and socket wrapped")
        except Exception as e:
            logger.warning(f"SSL setup error: {e}. Continuing without SSL")
            self.root.after(0, lambda: self.status_var.set(f"SSL setup error: {e}. Trying without SSL..."))

        try:
            tcp_socket.connect((server_host, server_port))
            logger.info(f"Connected to authentication server at {server_host}:{server_port}")

            response = 'f'
            flag = False

            while response != 't':
                if response == 'fr':
                    logger.warning("Username is already taken")
                    self.root.after(0, lambda: self.status_var.set("This username is already taken"))
                elif flag:
                    logger.warning("Username or password is incorrect")
                    self.root.after(0, lambda: self.status_var.set("Username or password is incorrect"))
                flag = True

                username = self.username_var.get()
                password = self.password_var.get()

                user_data = username + ',' + password
                logger.debug("Sending credentials to server")

                auth_protocol.send_message(tcp_socket, user_data)

                response_data = auth_protocol.receive_message(tcp_socket)
                if not response_data:
                    logger.error("Connection closed by server during authentication")
                    self.root.after(0, lambda: self.status_var.set("Connection closed by server"))
                    return False, None, None

                response = response_data.decode()
                logger.debug(f"Received authentication response: {response}")

                if response == 'f':
                    logger.info("Account not found, asking user about registration")
                    answer = messagebox.askyesno("Registration", "Account not found. Do you want to register?")
                    if answer:
                        logger.info("User chose to register new account")
                        auth_protocol.send_message(tcp_socket, 'r')
                        response_data = auth_protocol.receive_message(tcp_socket)
                        if not response_data:
                            logger.error("Connection closed during registration")
                            self.root.after(0, lambda: self.status_var.set("Connection closed during registration"))
                            return False, None, None
                        response = response_data.decode()
                        logger.debug(f"Registration response: {response}")
                    else:
                        logger.info("User declined registration")
                        return False, None, None

            if response == 't':
                logger.info("Authentication successful, receiving UDP port")
                port_data = auth_protocol.receive_message(tcp_socket)
                if port_data:
                    udp_port = int(port_data.decode())
                    logger.info(f"Received UDP streaming port: {udp_port}")
                    self.root.after(0, lambda: self.status_var.set(
                        f"Authentication successful. Streaming port: {udp_port}"))
                    return True, username, udp_port

            return False, None, None

        except Exception as e:
            logger.error(f"Authentication error: {e}", exc_info=True)
            self.root.after(0, lambda: self.status_var.set(f"Authentication error: {e}"))
            return False, None, None
        finally:
            tcp_socket.close()
            logger.debug("Authentication socket closed")

    def start_streaming(self):
        if not self.authenticated:
            logger.error("Attempted to start streaming without authentication")
            messagebox.showerror("Error", "You need to authenticate first")
            return

        try:
            logger.info("Starting streaming process")

            self.stream_status_var.set(f"Connected as: {self.username} | Port: {self.udp_port}")
            self.show_stream_frame()

            self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            logger.debug("UDP socket created for streaming")

            if platform.system() == "Windows":
                logger.info("Windows detected, using DirectShow backend for camera")
                self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            else:
                logger.info("Non-Windows system detected, using default backend for camera")
                self.cap = cv2.VideoCapture(0)

            if not self.cap.isOpened():
                logger.error("Camera failed to open")
                messagebox.showerror("Error", "Failed to open camera")
                return

            logger.info("Camera opened successfully")

            logger.debug("Setting camera properties")
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
            self.cap.set(cv2.CAP_PROP_FPS, 30)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            actual_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            actual_fps = int(self.cap.get(cv2.CAP_PROP_FPS))
            logger.info(f"Camera configured: {actual_width}x{actual_height} @ {actual_fps}fps")

            logger.debug("Testing initial frame capture")
            ret, test_frame = self.cap.read()
            if not ret:
                logger.error("Cannot read frame from camera")
                messagebox.showerror("Error", "Camera opened but cannot read frames")
                return

            if test_frame is None:
                logger.error("Camera returned empty frame")
                messagebox.showerror("Error", "Camera returned empty frame")
                return

            logger.debug(f"Test frame captured successfully: {test_frame.shape}")
            logger.debug(f"Frame mean brightness: {test_frame.mean()}")

            if test_frame.mean() < 1.0:
                logger.warning("Frame appears to be completely black - camera may be covered or malfunctioning")

            self.running = True
            logger.info("Streaming state set to active")

            logger.info("Starting audio streaming thread")
            audio_thread = threading.Thread(target=self.send_audio, daemon=True)
            audio_thread.start()

            logger.info("Starting video streaming loop")
            self.update_video()

        except Exception as e:
            logger.error(f"Failed to start streaming: {str(e)}", exc_info=True)
            messagebox.showerror("Error", f"Failed to start streaming: {str(e)}")
            self.stop_streaming()

    def update_video(self):
        logger.debug(f"Video update called - running: {self.running}, camera available: {self.cap is not None}")

        if not self.running or not self.cap:
            logger.debug("Exiting video update - streaming stopped or camera unavailable")
            return

        try:
            ret, frame = self.cap.read()

            if not ret or frame is None:
                logger.warning("Failed to read video frame, retrying")
                if self.running:
                    self.root.after(100, self.update_video)
                return

            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = PIL.Image.fromarray(frame_rgb)
            imgtk = PIL.ImageTk.PhotoImage(image=img)

            self.current_image = imgtk
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor=tk.NW, image=imgtk)

            try:
                frame_bytes = stream_protocol.encode_video_frame(frame, quality=80)
                video_fragments = stream_protocol.fragment_data("V", frame_bytes)
                server_ip = self.server_ip_var.get()
                fragments_sent = stream_protocol.send_fragments(
                    self.client_socket,
                    video_fragments,
                    (server_ip, self.udp_port)
                )

                logger.debug(f"Video frame sent: {len(frame_bytes)} bytes in {len(video_fragments)} fragments")

                current_time = time.time()
                if not hasattr(self, 'last_status_update'):
                    self.last_status_update = 0

                if current_time - self.last_status_update > 5:
                    self.stream_status_var.set(f"Connected as: {self.username} | Sending: {len(frame_bytes)} bytes")
                    self.last_status_update = current_time

            except Exception as e:
                logger.error(f"Error sending video frame: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error in video update loop: {e}", exc_info=True)

        if self.running:
            self.root.after(33, self.update_video)

    def send_audio(self):
        logger.info("Audio streaming thread started")
        try:
            logger.debug("Opening audio input stream")
            with sd.InputStream(samplerate=AUDIO_RATE, channels=AUDIO_CHANNELS, dtype=AUDIO_DTYPE) as stream:
                logger.info(f"Audio stream opened: {AUDIO_RATE}Hz, {AUDIO_CHANNELS} channels")
                audio_frame_count = 0
                while self.running:
                    audio_data, overflowed = stream.read(AUDIO_CHUNK)
                    if overflowed:
                        logger.warning("Audio buffer overflow detected")
                        continue

                    audio_frame_count += 1
                    if audio_frame_count % 1000 == 0:
                        logger.debug(f"Audio frames processed: {audio_frame_count}")

                    server_ip = self.server_ip_var.get()
                    audio_fragments = stream_protocol.fragment_data("A", audio_data.tobytes())
                    stream_protocol.send_fragments(
                        self.client_socket,
                        audio_fragments,
                        (server_ip, self.udp_port)
                    )

                    time.sleep(0.01)

        except Exception as e:
            logger.error(f"Audio streaming error: {str(e)}", exc_info=True)
            if self.running:
                self.root.after(0, lambda: messagebox.showerror("Audio Error", f"Audio stream error: {str(e)}"))
                self.root.after(0, self.stop_streaming)

        logger.info("Audio streaming thread ended")

    def stop_streaming(self):
        logger.info("Stopping streaming session")
        self.running = False

        if self.cap:
            logger.debug("Releasing camera resources")
            self.cap.release()
            self.cap = None

        if self.client_socket:
            logger.debug("Closing UDP socket")
            self.client_socket.close()
            self.client_socket = None

        self.authenticated = False
        self.username = None
        self.udp_port = None

        self.show_login_frame()
        self.login_button.config(state=tk.NORMAL)
        self.status_var.set("Enter your credentials to start streaming")
        logger.info("Streaming stopped, returned to login screen")

    def on_closing(self):
        logger.info("Application closing")
        self.running = False
        if self.cap:
            logger.debug("Releasing camera on application close")
            self.cap.release()
        if self.client_socket:
            logger.debug("Closing socket on application close")
            self.client_socket.close()
        self.root.destroy()
        logger.info("Application closed successfully")


if __name__ == "__main__":
    logger.info("Starting Video Streamer application")
    root = tk.Tk()
    app = StreamerApp(root)
    root.mainloop()
    logger.info("Video Streamer application terminated")
