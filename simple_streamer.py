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

# Server settings
SERVER_IP = "127.0.0.1"
SERVER_AUTH_PORT = 5050  # TCP port for authentication

# Video settings
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480

# Audio settings
AUDIO_RATE = 44100
AUDIO_CHANNELS = 1
AUDIO_CHUNK = 1024
AUDIO_DTYPE = np.int16

# Create protocol instances
stream_protocol = StreamProtocol()
auth_protocol = AuthProtocol()


class StreamerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Video Streamer")
        self.root.geometry("800x600")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # Application state
        self.running = False
        self.authenticated = False
        self.username = None
        self.udp_port = None
        self.client_socket = None
        self.cap = None

        # Create frames for different app states
        self.create_login_frame()
        self.create_stream_frame()

        # Start with login frame
        self.show_login_frame()

    def create_login_frame(self):
        """Create the login UI components"""
        self.login_frame = ttk.Frame(self.root, padding=20)

        # Server connection settings
        ttk.Label(self.login_frame, text="Server Settings", font=("Arial", 14, "bold")).grid(row=0, column=0,
                                                                                             columnspan=2, pady=10,
                                                                                             sticky='w')

        ttk.Label(self.login_frame, text="Server IP:").grid(row=1, column=0, sticky='w', pady=5)
        self.server_ip_var = tk.StringVar(value=SERVER_IP)
        ttk.Entry(self.login_frame, textvariable=self.server_ip_var, width=30).grid(row=1, column=1, sticky='w', padx=5)

        ttk.Label(self.login_frame, text="Auth Port:").grid(row=2, column=0, sticky='w', pady=5)
        self.auth_port_var = tk.StringVar(value=str(SERVER_AUTH_PORT))
        ttk.Entry(self.login_frame, textvariable=self.auth_port_var, width=30).grid(row=2, column=1, sticky='w', padx=5)

        # Login details
        ttk.Label(self.login_frame, text="Login", font=("Arial", 14, "bold")).grid(row=3, column=0, columnspan=2,
                                                                                   pady=(20, 10), sticky='w')

        ttk.Label(self.login_frame, text="Username:").grid(row=4, column=0, sticky='w', pady=5)
        self.username_var = tk.StringVar()
        ttk.Entry(self.login_frame, textvariable=self.username_var, width=30).grid(row=4, column=1, sticky='w', padx=5)

        ttk.Label(self.login_frame, text="Password:").grid(row=5, column=0, sticky='w', pady=5)
        self.password_var = tk.StringVar()
        ttk.Entry(self.login_frame, textvariable=self.password_var, width=30, show="*").grid(row=5, column=1,
                                                                                             sticky='w', padx=5)

        # Status message
        self.status_var = tk.StringVar(value="Enter your credentials to start streaming")
        ttk.Label(self.login_frame, textvariable=self.status_var, foreground='blue').grid(row=6, column=0, columnspan=2,
                                                                                          pady=10)

        # Login button
        self.login_button = ttk.Button(self.login_frame, text="Login", command=self.authenticate)
        self.login_button.grid(row=7, column=0, columnspan=2, pady=10)

    def create_stream_frame(self):
        """Create the streaming UI components"""
        self.stream_frame = ttk.Frame(self.root, padding=10)

        # Video frame (top)
        self.video_frame = ttk.Frame(self.stream_frame)
        self.video_frame.pack(fill=tk.BOTH, expand=True, pady=10)

        # Video canvas
        self.canvas = tk.Canvas(self.video_frame, width=CAMERA_WIDTH, height=CAMERA_HEIGHT, bg="black")
        self.canvas.pack()

        # Control frame (bottom)
        control_frame = ttk.Frame(self.stream_frame)
        control_frame.pack(fill=tk.X, pady=10)

        # Status label
        self.stream_status_var = tk.StringVar(value="Connected as: Not logged in")
        ttk.Label(control_frame, textvariable=self.stream_status_var).pack(side=tk.LEFT, padx=10)

        # Stop button
        self.stop_button = ttk.Button(control_frame, text="Disconnect", command=self.stop_streaming)
        self.stop_button.pack(side=tk.RIGHT, padx=10)

    def show_login_frame(self):
        """Switch to login view"""
        self.stream_frame.pack_forget()
        self.login_frame.pack(fill=tk.BOTH, expand=True)

    def show_stream_frame(self):
        """Switch to streaming view"""
        self.login_frame.pack_forget()
        self.stream_frame.pack(fill=tk.BOTH, expand=True)

    def authenticate(self):
        """Handle authentication with the server"""
        # Get values from input fields
        server_ip = self.server_ip_var.get()
        auth_port = int(self.auth_port_var.get())

        username = self.username_var.get()
        password = self.password_var.get()

        # Validate inputs
        if not username or not password:
            messagebox.showerror("Error", "Username and password cannot be empty")
            return

        # Disable login button during authentication
        self.login_button.config(state=tk.DISABLED)
        self.status_var.set("Authenticating...")
        self.root.update()

        # Start authentication in a separate thread to keep UI responsive
        auth_thread = threading.Thread(target=self.authentication_thread,
                                       args=(server_ip, auth_port),
                                       daemon=True)
        auth_thread.start()

    def authentication_thread(self, server_ip, auth_port):
        """Run authentication process in a separate thread"""
        try:
            # Custom authentication handler to integrate with GUI
            auth_result, username, udp_port = self.gui_authenticate(server_ip, auth_port)

            if auth_result:
                self.authenticated = True
                self.username = username
                self.udp_port = udp_port

                # Update UI in the main thread
                self.root.after(0, lambda: self.start_streaming())
            else:
                # Update UI in the main thread
                self.root.after(0, lambda: self.status_var.set("Authentication failed. Try again."))
                self.root.after(0, lambda: self.login_button.config(state=tk.NORMAL))
        except Exception as e:
            error_msg = f"Authentication error: {str(e)}"
            self.root.after(0, lambda: messagebox.showerror("Error", error_msg))
            self.root.after(0, lambda: self.login_button.config(state=tk.NORMAL))

    def gui_authenticate(self, server_host, server_port):
        """
        GUI-friendly implementation of client authentication logic
        Based on AuthProtocol.client_authenticate but adapted for GUI
        """
        # Create TCP socket for auth
        tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        # Setup SSL context for client
        try:
            ssl_context = ssl.create_default_context()
            # For testing with self-signed certificate
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            # Wrap socket in SSL
            tcp_socket = ssl_context.wrap_socket(tcp_socket, server_hostname=server_host)
        except Exception as e:
            self.root.after(0, lambda: self.status_var.set(f"SSL setup error: {e}. Trying without SSL..."))
            # Continue without SSL if error occurs

        try:
            # Connect to server
            tcp_socket.connect((server_host, server_port))

            response = 'f'
            flag = False

            while response != 't':
                if response == 'fr':
                    self.root.after(0, lambda: self.status_var.set("This username is already taken"))
                elif flag:
                    self.root.after(0, lambda: self.status_var.set("Username or password is incorrect"))
                flag = True

                # For GUI, we already have username and password from entry fields
                username = self.username_var.get()
                password = self.password_var.get()

                user_data = username + ',' + password

                # Send credentials via TCP
                auth_protocol.send_message(tcp_socket, user_data)

                # Get response
                response_data = auth_protocol.receive_message(tcp_socket)
                if not response_data:
                    self.root.after(0, lambda: self.status_var.set("Connection closed by server"))
                    return False, None, None

                response = response_data.decode()

                if response == 'f':
                    # Ask about registration using a message box
                    answer = messagebox.askyesno("Registration", "Account not found. Do you want to register?")
                    if answer:
                        auth_protocol.send_message(tcp_socket, 'r')
                        response_data = auth_protocol.receive_message(tcp_socket)
                        if not response_data:
                            self.root.after(0, lambda: self.status_var.set("Connection closed during registration"))
                            return False, None, None
                        response = response_data.decode()
                    else:
                        # If user says no to registration, we need to break and try again
                        return False, None, None

            # Get UDP port for streaming from server response if authentication successful
            if response == 't':
                port_data = auth_protocol.receive_message(tcp_socket)
                if port_data:
                    udp_port = int(port_data.decode())
                    self.root.after(0, lambda: self.status_var.set(
                        f"Authentication successful. Streaming port: {udp_port}"))
                    return True, username, udp_port

            return False, None, None

        except Exception as e:
            self.root.after(0, lambda: self.status_var.set(f"Authentication error: {e}"))
            return False, None, None
        finally:
            tcp_socket.close()

    def start_streaming(self):
        """Start video and audio streaming after successful authentication"""
        if not self.authenticated:
            messagebox.showerror("Error", "You need to authenticate first")
            return

        try:
            # Update UI
            self.stream_status_var.set(f"Connected as: {self.username} | Port: {self.udp_port}")
            self.show_stream_frame()

            # Create UDP socket for streaming
            self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

            # Initialize video capture
            self.cap = cv2.VideoCapture(0)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)

            # Set running state
            self.running = True

            # Start audio streaming thread
            audio_thread = threading.Thread(target=self.send_audio, daemon=True)
            audio_thread.start()

            # Start video streaming
            self.update_video()

        except Exception as e:
            messagebox.showerror("Error", f"Failed to start streaming: {str(e)}")
            self.stop_streaming()

    def update_video(self):
        """Update video frame and send to server"""
        if not self.running or not self.cap:
            return

        ret, frame = self.cap.read()
        if ret:
            # Display the frame locally
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = PIL.Image.fromarray(frame_rgb)
            imgtk = PIL.ImageTk.PhotoImage(image=img)

            # Keep a reference to prevent garbage collection
            self.current_image = imgtk
            self.canvas.create_image(0, 0, anchor=tk.NW, image=imgtk)

            # Send frame to server
            try:
                frame_bytes = stream_protocol.encode_video_frame(frame, quality=80)
                video_fragments = stream_protocol.fragment_data("V", frame_bytes)
                server_ip = self.server_ip_var.get()
                fragments_sent = stream_protocol.send_fragments(
                    self.client_socket,
                    video_fragments,
                    (server_ip, self.udp_port)
                )

                # Update status periodically (not every frame to avoid UI lag)
                if self.running and time.time() % 5 < 0.1:
                    self.stream_status_var.set(f"Connected as: {self.username} | Sending: {len(frame_bytes)} bytes")

            except Exception as e:
                print(f"Error sending video: {e}")

        # Schedule next frame update (30 fps = ~33ms)
        if self.running:
            self.root.after(33, self.update_video)

    def send_audio(self):
        """Send audio stream to server"""
        try:
            with sd.InputStream(samplerate=AUDIO_RATE, channels=AUDIO_CHANNELS, dtype=AUDIO_DTYPE) as stream:
                while self.running:
                    audio_data, overflowed = stream.read(AUDIO_CHUNK)
                    if overflowed:
                        continue

                    # Use protocol for fragmentation and sending audio
                    server_ip = self.server_ip_var.get()
                    audio_fragments = stream_protocol.fragment_data("A", audio_data.tobytes())
                    stream_protocol.send_fragments(
                        self.client_socket,
                        audio_fragments,
                        (server_ip, self.udp_port)
                    )

                    # Sleep to prevent CPU overuse
                    time.sleep(0.01)
        except Exception as e:
            if self.running:  # Only show error if we're still supposed to be running
                self.root.after(0, lambda: messagebox.showerror("Audio Error", f"Audio stream error: {str(e)}"))
                self.root.after(0, self.stop_streaming)

    def stop_streaming(self):
        """Stop streaming and return to login screen"""
        self.running = False

        # Clean up resources
        if self.cap:
            self.cap.release()
            self.cap = None

        if self.client_socket:
            self.client_socket.close()
            self.client_socket = None

        # Reset authentication state
        self.authenticated = False
        self.username = None
        self.udp_port = None

        # Return to login screen
        self.show_login_frame()
        self.login_button.config(state=tk.NORMAL)
        self.status_var.set("Enter your credentials to start streaming")

    def on_closing(self):
        """Handle window close event"""
        self.running = False
        if self.cap:
            self.cap.release()
        if self.client_socket:
            self.client_socket.close()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = StreamerApp(root)
    root.mainloop()