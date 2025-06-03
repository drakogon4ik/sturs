"""
Author: Oleg Shkolnik
Class: יב9
Description: protocol of streaming and authentication between streamer and server
Date: 3/06/25
"""


import socket
import struct
import numpy as np
import cv2
import logging
import datetime
import os

# Protocol constants
MAX_UDP_PACKET_SIZE = 1024  # Standard UDP packet size
FRAGMENT_HEADER_SIZE = 20  # Header size
DATA_TYPE_VIDEO = "V"  # Data type - video
DATA_TYPE_AUDIO = "A"  # Data type - audio


def setup_logging():
    """
    Sets up logging configuration for the application
    """
    # Create logs directory if it doesn't exist
    if not os.path.exists('logs'):
        os.makedirs('logs')

    # Configure logging format
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'

    # Set up file handler (logs to file)
    file_handler = logging.FileHandler(
        f'logs/protocol_{datetime.datetime.now().strftime("%Y%m%d")}.log'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format))

    # Set up console handler (logs to terminal)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format))

    # Create logger
    log = logging.getLogger('StreamerApp')
    log.setLevel(logging.DEBUG)

    # Add handlers to logger
    log.addHandler(file_handler)
    log.addHandler(console_handler)

    return log


# Initialize logger at module level
logger = setup_logging()


class StreamProtocol:
    def __init__(self):
        self.packet_sequence = 0
        self.video_reassembly_buffer = {}
        self.audio_reassembly_buffer = {}
        logger.info("StreamProtocol initialized")

    def fragment_data(self, data_type, data):
        """
        Splits large data into fragments
        :param data_type: Data type ('V' for video or 'A' for audio)
        :param data: Data to be fragmented
        :return: List of fragments with headers
        """
        # Calculate maximum payload size
        max_payload_size = MAX_UDP_PACKET_SIZE - FRAGMENT_HEADER_SIZE

        # Split into fragments
        fragments = []
        total_fragments = (len(data) + max_payload_size - 1) // max_payload_size

        logger.debug(f"Fragmenting {data_type} data: {len(data)} bytes into {total_fragments} fragments")

        for i in range(total_fragments):
            start = i * max_payload_size
            end = min(start + max_payload_size, len(data))
            fragment_data = data[start:end]

            # Create fragment header
            try:
                fragment_header = struct.pack(
                    "!5I",  # 5 integers
                    ord(data_type[0]),  # Data type
                    self.packet_sequence,  # Packet sequence number
                    total_fragments,  # Total fragments count
                    i,  # Current fragment number
                    0  # Add fifth element (CRC or reserve)
                )
            except Exception as e:
                logger.error(f"Header packing error: {e}")
                raise

            fragment = fragment_header + fragment_data
            fragments.append(fragment)

        self.packet_sequence += 1
        logger.debug(f"Data fragmentation completed, packet sequence: {self.packet_sequence}")
        return fragments

    def send_fragments(self, socket, fragments, address):
        """
        Sends fragments to the specified address
        :param socket: UDP socket
        :param fragments: List of fragments
        :param address: Destination address (IP, port)
        :return: Number of sent fragments
        """
        logger.debug(f"Sending {len(fragments)} fragments to {address}")
        for fragment in fragments:
            socket.sendto(fragment, address)
        logger.debug(f"Successfully sent {len(fragments)} fragments")
        return len(fragments)

    def process_fragment(self, data):
        """
        Processes received data fragment
        :param data: Received data with header
        :return: Tuple (data type, packet number, total fragments, fragment number, payload)
                or None in case of error
        """
        # Check minimum length for header unpacking
        if len(data) < FRAGMENT_HEADER_SIZE:
            logger.warning(f"Packet too short: {len(data)} bytes, expected at least {FRAGMENT_HEADER_SIZE}")
            return None

        # Safe unpacking
        try:
            # Unpack 5 integers
            data_type_byte, packet_seq, total_fragments, fragment_num, _ = struct.unpack(
                "!5I", data[:FRAGMENT_HEADER_SIZE]
            )

            # Convert byte to character
            data_type = chr(data_type_byte)

            # Get payload
            payload = data[FRAGMENT_HEADER_SIZE:]

            logger.debug(f"Processed fragment: type={data_type}, seq={packet_seq}, "
                         f"fragment={fragment_num}/{total_fragments}, payload_size={len(payload)}")

            return data_type, packet_seq, total_fragments, fragment_num, payload

        except struct.error as e:
            logger.error(f"Error unpacking header: {e}")
            logger.error(f"Header bytes: {data[:FRAGMENT_HEADER_SIZE].hex()}")
            return None

    def reassemble_fragment(self, data_type, packet_seq, total_fragments, fragment_num, payload):
        """
        Adds fragment to reassembly buffer and checks if a complete packet can be assembled
        :param data_type: Data type ('V' or 'A')
        :param packet_seq: Packet number
        :param total_fragments: Total number of fragments
        :param fragment_num: Current fragment number
        :param payload: Fragment payload
        :return: Fully assembled data if all fragments received, otherwise None
        """
        # Choose appropriate buffer based on data type
        reassembly_buffer = self.video_reassembly_buffer if data_type == DATA_TYPE_VIDEO else self.audio_reassembly_buffer

        # Create new entry in buffer if packet doesn't exist yet
        if packet_seq not in reassembly_buffer:
            reassembly_buffer[packet_seq] = [None] * total_fragments
            logger.debug(
                f"Created new reassembly buffer entry for packet {packet_seq} with {total_fragments} fragments")

        # Save fragment
        reassembly_buffer[packet_seq][fragment_num] = payload
        logger.debug(f"Stored fragment {fragment_num} for packet {packet_seq}")

        # Check if all fragments received
        if None not in reassembly_buffer[packet_seq]:
            # Assemble full packet
            full_data = b''.join(reassembly_buffer[packet_seq])

            # Remove assembled packet from buffer
            del reassembly_buffer[packet_seq]
            return full_data

        fragments_received = sum(1 for f in reassembly_buffer[packet_seq] if f is not None)
        logger.debug(f"Packet {packet_seq} incomplete: {fragments_received}/{total_fragments} fragments received")
        return None

    def encode_video_frame(self, frame, quality=80):
        """
        Encodes video frame into compressed format
        :param frame: Image frame
        :param quality: JPEG quality (0-100)
        :return: Encoded frame data
        """
        if frame.size == 0:
            logger.error("Attempted to encode empty frame")
            raise ValueError("Empty frame!")

        logger.debug(f"Encoding video frame with quality {quality}")
        _, encoded_frame = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])

        if encoded_frame is None or len(encoded_frame) == 0:
            logger.error("Failed to encode video frame")
            raise ValueError("Failed to encode frame")

        logger.debug(f"Frame encoded successfully, size: {len(encoded_frame)} bytes")
        return encoded_frame.tobytes()

    def decode_video_frame(self, data):
        """
        Decodes data into video frame
        :param data: Encoded frame data
        :return: Decoded frame or None on error
        """
        logger.debug(f"Decoding video frame from {len(data)} bytes")
        np_data = np.frombuffer(data, dtype=np.uint8)
        decoded_frame = cv2.imdecode(np_data, cv2.IMREAD_COLOR)

        if decoded_frame is None:
            logger.error("Failed to decode video frame")
        else:
            logger.debug(f"Frame decoded successfully, shape: {decoded_frame.shape}")

        return decoded_frame

    def clear_buffers(self):
        """
        Clears video and audio reassembly buffers
        :return: None
        """
        video_count = len(self.video_reassembly_buffer)
        audio_count = len(self.audio_reassembly_buffer)

        self.video_reassembly_buffer.clear()
        self.audio_reassembly_buffer.clear()

        logger.info(f"Cleared reassembly buffers: {video_count} video packets, {audio_count} audio packets")


class AuthProtocol:
    """
    Protocol for authentication over TCP
    """

    @staticmethod
    def send_message(socket, message):
        """
        Send message with length prefix
        """
        # Convert message to bytes if it's a string
        if isinstance(message, str):
            message = message.encode()

        # Send message length as 4-byte integer
        message_len = len(message)
        socket.sendall(struct.pack('!I', message_len))

        # Send actual message
        socket.sendall(message)
        logger.debug(f"Sent message of {message_len} bytes")

    @staticmethod
    def receive_message(socket):
        """
        Receive message with length prefix
        """
        # Get message length (4 bytes)
        header = socket.recv(4)
        if not header or len(header) < 4:
            logger.warning("Failed to receive message header or incomplete header")
            return None

        message_len = struct.unpack('!I', header)[0]
        logger.debug(f"Expecting message of {message_len} bytes")

        # Get actual message
        chunks = []
        bytes_received = 0

        while bytes_received < message_len:
            chunk = socket.recv(min(message_len - bytes_received, 4096))
            if not chunk:
                logger.error("Connection closed while receiving message")
                return None
            chunks.append(chunk)
            bytes_received += len(chunk)

        logger.debug(f"Successfully received message of {bytes_received} bytes")
        return b''.join(chunks)

    def server_authenticate(self, client_socket, auth_function, register_function=None):
        """
        Server-side authentication via TCP
        :param client_socket: Accepted TCP client socket
        :param auth_function: Function to verify credentials
        :param register_function: Function to register user
        :return: Username or None on error
        """
        try:
            answer = 'f'
            username = None

            logger.info("Starting server authentication process")

            while True:
                # Receive credentials
                data = self.receive_message(client_socket)
                if not data:
                    logger.warning("Client disconnected during authentication")
                    return None

                # Check if it's a registration request
                if data.decode() == 'r' and register_function:
                    logger.info(f"Processing registration request for user: {username}")
                    if register_function(username, password):
                        logger.info(f"User {username} registered successfully")
                        break
                    else:
                        answer = 'fr'
                        logger.warning(f"Registration failed for user: {username}")
                        self.send_message(client_socket, answer)
                        continue

                # Process credentials
                user_data = data.decode().split(',')
                username = user_data[0]
                password = user_data[1]

                logger.info(f"Authentication attempt for user: {username}")

                # Authenticate
                if auth_function(username, password):
                    answer = 't'
                    logger.info(f"User {username} authenticated successfully")
                    self.send_message(client_socket, answer)
                    break
                else:
                    logger.warning(f"Authentication failed for user: {username}")
                    self.send_message(client_socket, answer)

            return username

        except Exception as e:
            logger.error(f"Server authentication error: {e}")
            return None

    @staticmethod
    def create_socket(host='0.0.0.0', port=0, tcp=False):
        """
        Creates socket and binds it to the specified address
        :param host: IP address
        :param port: Port number
        :param tcp: If True creates TCP socket, otherwise UDP
        :return: Created socket
        """
        socket_type = "TCP" if tcp else "UDP"
        logger.info(f"Creating {socket_type} socket on {host}:{port}")

        if tcp:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        sock.bind((host, port))
        actual_port = sock.getsockname()[1]
        logger.info(f"{socket_type} socket created and bound to {host}:{actual_port}")

        return sock
